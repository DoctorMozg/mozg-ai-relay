"""`mz-relay hook wait` end to end: relayd with the Claude adapter, a fake session, waiters."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from fakes_claude import CWD, FakeClaudeSession, HookEvent, claude_roster
from mz_relay.adapters.claude import ClaudeAdapter, ClaudeTiming
from mz_relay.client import RelayClient
from mz_relay.clock import SystemClock
from mz_relay.envelope import Status
from mz_relay.ids import new_id
from mz_relay.paths import Paths
from mz_relay.roster import Harness
from mz_relay.store import Store, StoredMessage
from relay_rig import BIN, reap, running_daemon, stop_daemon, wait_until

# A ceiling for a waiter to exit or a registration to appear; costs nothing when it is quick.
EXIT_S = 5.0
# Longer than the waiter's 1 s wait slice, so a wait started before a status change has ended.
SLICE_S = 1.5
BODY = "please review src/app.py"
HEADER = "[mz-relay] Peer message from sender"


class WaiterProc:
    """One `bin/mz-relay hook wait <event>` child fed a hook JSON on stdin."""

    def __init__(
        self, event: HookEvent, stdin_text: str, env: Mapping[str, str], err: Path
    ) -> None:
        self.err = err
        with err.open("wb") as sink:
            self.proc = subprocess.Popen(
                [str(BIN), "hook", "wait", event],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=sink,
                env=dict(env),
                cwd=err.parent,
            )
        assert self.proc.stdin is not None
        # A child that exits before reading its input is checked by its exit code instead.
        with contextlib.suppress(BrokenPipeError):
            self.proc.stdin.write(stdin_text.encode())
        with contextlib.suppress(BrokenPipeError):
            self.proc.stdin.close()

    def exit_code(self, timeout_s: float = EXIT_S) -> int | None:
        """The exit code, or None when the waiter is still running after `timeout_s`."""
        try:
            return self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None

    def running(self) -> bool:
        return self.proc.poll() is None

    def stderr(self) -> str:
        return self.err.read_text(encoding="utf-8")

    def kill(self) -> None:
        reap(self.proc, timeout_s=0)


class HookRig:
    """relayd in-process with a real ClaudeAdapter; `cc` lives in a FakeClaudeSession."""

    def __init__(
        self, stack: contextlib.ExitStack, paths: Paths, env: Mapping[str, str], tools: str
    ) -> None:
        self.stack = stack
        self.paths = paths
        self.home = paths.config_dir
        self.env = dict(env)
        # The session pid is a child we can kill; reaped at once so pid checks see it gone.
        self.sleeper = subprocess.Popen(["sleep", "60"])
        stack.callback(self.kill_session_pid)
        self.fake = stack.enter_context(FakeClaudeSession(self.home / "c", pid=self.sleeper.pid))
        # A live session has a transcript, so the marker and turn end have a size to record.
        self.fake.writer.human("start the review")
        adapter = ClaudeAdapter(
            clock=SystemClock(), timing=ClaudeTiming(confirm_s=0.3, poll_s=0.02)
        )
        roster = claude_roster(self.fake.config_dir, tools=tools)
        stack.enter_context(running_daemon(paths, {Harness.CLAUDE: adapter}, roster_text=roster))
        self.store = Store(paths.db)
        stack.callback(self.store.close)
        self.sender, self.sender_token = self.client("sender", "opencode")
        self._waiters = 0

    def kill_session_pid(self) -> None:
        reap(self.sleeper, timeout_s=0)

    def hook_input(self, event: HookEvent) -> str:
        fields: dict[str, object] = {
            "session_id": self.fake.session_id,
            "cwd": CWD,
            "transcript_path": str(self.fake.transcript_path),
            "hook_event_name": event,
        }
        if event == "SessionStart":
            fields["source"] = "startup"
        return json.dumps(fields)

    def waiter(
        self,
        event: HookEvent = "SessionStart",
        *,
        stdin_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> WaiterProc:
        self._waiters += 1
        if env is None:
            env = {"CLAUDE_CONFIG_DIR": str(self.fake.config_dir), "MZ_RELAY_NAME": "cc"}
        proc = WaiterProc(
            event,
            self.hook_input(event) if stdin_text is None else stdin_text,
            {**self.env, **env},
            self.home / f"waiter-{self._waiters}.err",
        )
        # Waiters go before relayd, so none of them sees relayd vanish and spawns another.
        self.stack.callback(proc.kill)
        return proc

    def waiter_registered(self) -> bool:
        reg = self.store.latest_delivery_registration("cc")
        return reg is not None and reg.delivery_mode == "fallback"

    def ready_waiter(self, env: Mapping[str, str] | None = None) -> WaiterProc:
        """A SessionStart waiter that has registered its rewake-only binding."""
        proc = self.waiter(env=env)
        assert wait_until(lambda: self.waiter_registered() or not proc.running(), EXIT_S)
        assert proc.running(), f"waiter exited {proc.proc.returncode}: {proc.stderr()}"
        assert self.waiter_registered(), "waiter did not register a rewake binding"
        return proc

    def client(self, name: str, harness: str, **extra: object) -> tuple[RelayClient, str]:
        """A registration kept online until the test ends."""
        relay = RelayClient(self.paths.socket)
        relay.connect(client="cli")
        self.stack.callback(relay.close)
        args: dict[str, object] = {"name": name, "harness": harness, "cwd": CWD, "pid": os.getpid()}
        args.update(extra)
        token = relay.call("register", args)["token"]
        assert isinstance(token, str)
        return relay, token

    def cc_client(self, delivery: Mapping[str, object] | None = None) -> tuple[RelayClient, str]:
        extra: dict[str, object] = {"config_dir": str(self.fake.config_dir)}
        if delivery is not None:
            extra["delivery"] = dict(delivery)
        return self.client("cc", "claude", **extra)

    def send(self, body: str = BODY) -> str:
        msg_id = new_id()
        args = {"token": self.sender_token, "id": msg_id, "to": "cc", "kind": "request"}
        result = self.sender.call("send", {**args, "body": body})
        assert result["status"] == "queued", result
        return msg_id

    def row(self, msg_id: str) -> StoredMessage:
        got = self.store.get("sender", msg_id)
        assert got is not None, f"row {msg_id} missing"
        return got

    @property
    def marker(self) -> Path:
        return self.paths.marker_dir / self.fake.session_id

    @property
    def turn_end(self) -> Path:
        return self.paths.turn_end_dir / self.fake.session_id


MakeHooks = Callable[..., HookRig]


@pytest.fixture
def make_hooks(relay_paths: Paths, relay_env: dict[str, str]) -> Iterator[MakeHooks]:
    with contextlib.ExitStack() as stack:
        # Runs last: a relayd some waiter spawned after the in-process one stopped.
        stack.callback(stop_daemon, relay_paths.socket)

        def build(*, tools: str = "read_only") -> HookRig:
            return HookRig(stack, relay_paths, relay_env, tools)

        yield build


class TestRewakeDelivery:
    @pytest.mark.parametrize(("tools", "marked"), [("read_only", True), ("full", False)])
    def test_rewake_only_session_is_woken_with_the_message(
        self, make_hooks: MakeHooks, tools: str, marked: bool
    ) -> None:
        hooks = make_hooks(tools=tools)
        waiter = hooks.ready_waiter()

        msg_id = hooks.send()

        assert waiter.exit_code() == 2
        err = waiter.stderr()
        assert err.startswith(HEADER)
        assert f"  {BODY}" in err
        assert msg_id in err
        row = hooks.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "rewake")
        # Only a read_only agent needs the marker: it tells the gate a peer woke the session.
        assert hooks.marker.exists() is marked
        if marked:
            assert hooks.marker.read_text(encoding="ascii").isdigit()

    def test_with_hooks_log_unopenable_stderr_carries_only_the_message(
        self, make_hooks: MakeHooks
    ) -> None:
        hooks = make_hooks()
        # A folder where hooks.log should be: opening it for append fails.
        hooks.paths.hooks_log.mkdir()
        waiter = hooks.ready_waiter()

        msg_id = hooks.send()

        assert waiter.exit_code() == 2
        err = waiter.stderr()
        # On exit 2 stderr is Claude's prompt, so a log line there would read as the peer's text.
        assert err.startswith(HEADER), err
        assert msg_id in err
        assert "mz-relay-hook[" not in err
        assert "cannot open" not in err

    def test_tools_only_registration_still_gets_the_rewake(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()
        waiter = hooks.ready_waiter()
        hooks.cc_client()

        msg_id = hooks.send()

        assert waiter.exit_code() == 2
        assert waiter.stderr().startswith(HEADER)
        assert (hooks.row(msg_id).status, hooks.row(msg_id).via) == (Status.DELIVERED, "rewake")

    def test_native_binding_wins_and_the_waiter_keeps_waiting(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()
        waiter = hooks.ready_waiter()
        hooks.cc_client(hooks.fake.native_binding())

        msg_id = hooks.send()

        assert wait_until(lambda: hooks.row(msg_id).status is Status.DELIVERED, EXIT_S)
        assert hooks.row(msg_id).via == "claude"
        assert len(hooks.fake.frames) == 1
        assert waiter.exit_code(1.0) is None

    @pytest.mark.parametrize(
        "env",
        [
            {"CLAUDE_PLUGIN_OPTION_RELAY_NAME": "cc"},
            {"MZ_RELAY_NAME": "", "CLAUDE_PLUGIN_OPTION_RELAY_NAME": ""},
        ],
        ids=["plugin-option-name", "config-dir-match"],
    )
    def test_waiter_identity_without_mz_relay_name(
        self, make_hooks: MakeHooks, env: dict[str, str]
    ) -> None:
        hooks = make_hooks()

        hooks.ready_waiter(env={"CLAUDE_CONFIG_DIR": str(hooks.fake.config_dir), **env})


class TestSingleton:
    def test_second_waiter_for_the_session_exits_quietly(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()
        first = hooks.ready_waiter()

        second = hooks.waiter()

        assert second.exit_code() == 0
        assert second.stderr() == ""
        assert first.running()

    def test_stop_records_the_turn_end_while_session_start_holds_the_lock(
        self, make_hooks: MakeHooks
    ) -> None:
        hooks = make_hooks()
        first = hooks.ready_waiter()
        hooks.marker.parent.mkdir(parents=True, exist_ok=True)
        hooks.marker.write_text("0", encoding="ascii")
        hooks.fake.writer.human("review the plan")
        hooks.fake.writer.assistant("done")

        stop = hooks.waiter("Stop")

        assert stop.exit_code() == 0
        size = hooks.fake.transcript_path.stat().st_size
        assert hooks.turn_end.read_text(encoding="ascii") == str(size)
        assert not hooks.marker.exists()
        assert first.running()


class TestWaiterExits:
    def test_session_start_with_native_messaging_exits_at_once(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()
        env = {"CLAUDE_CONFIG_DIR": str(hooks.fake.config_dir), "MZ_RELAY_NAME": "cc"}

        waiter = hooks.waiter(env={**env, "CLAUDE_CODE_MESSAGING_SOCKET": "/nonexistent.sock"})

        assert waiter.exit_code() == 0
        assert waiter.stderr() == ""
        assert not hooks.waiter_registered()
        # The lock is free again, so the Stop waiter of the first turn can serve FALLBACK.
        hooks.ready_waiter()

    def test_waiter_exits_when_the_session_pid_dies(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()
        waiter = hooks.ready_waiter()

        hooks.kill_session_pid()

        assert waiter.exit_code() == 0
        assert waiter.stderr() == ""

    @pytest.mark.parametrize("tools", ["read_only", "full"])
    def test_waiter_exits_when_the_registry_entry_disappears(
        self, make_hooks: MakeHooks, tools: str
    ) -> None:
        hooks = make_hooks(tools=tools)
        waiter = hooks.ready_waiter()

        hooks.fake.registry_path.unlink()

        assert waiter.exit_code() == 0

    def test_message_after_the_waiter_left_waits_for_inbox(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()
        waiter = hooks.ready_waiter()
        hooks.fake.registry_path.unlink()
        assert waiter.exit_code() == 0

        msg_id = hooks.send()
        time.sleep(SLICE_S)

        row = hooks.row(msg_id)
        assert row.status is Status.QUEUED
        assert row.via is None
        cc, cc_token = hooks.cc_client()
        assert msg_id in str(cc.call("inbox", {"token": cc_token})["text"])

    def test_a_message_sent_as_the_registry_entry_vanishes_wakes_nothing(
        self, make_hooks: MakeHooks
    ) -> None:
        hooks = make_hooks()
        waiter = hooks.ready_waiter()
        hooks.fake.registry_path.unlink()

        msg_id = hooks.send()
        seen: set[Status] = set()

        def waiter_gone() -> bool:
            seen.add(hooks.row(msg_id).status)
            return not waiter.running()

        assert wait_until(waiter_gone, EXIT_S)
        # Keeps sampling one more slice, in case the row moves just as the waiter exits.
        wait_until(lambda: waiter_gone() and False, SLICE_S)

        # A wait already in flight may lease the row; the waiter must not print or confirm it.
        assert waiter.exit_code() == 0
        assert waiter.stderr() == ""
        assert seen.isdisjoint({Status.DELIVERED, Status.ACKED}), seen

    def test_bad_hook_input_exits_zero_and_logs_a_warning(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()

        waiter = hooks.waiter(stdin_text="not json")

        assert waiter.exit_code() == 0
        assert "WARNING" in hooks.paths.hooks_log.read_text(encoding="utf-8")

    def test_unmatched_identity_exits_zero(self, make_hooks: MakeHooks) -> None:
        hooks = make_hooks()
        other = hooks.home / "other-cfg"
        other.mkdir()

        waiter = hooks.waiter(env={"CLAUDE_CONFIG_DIR": str(other)})

        assert waiter.exit_code() == 0
        assert not hooks.waiter_registered()


class TestReadOnlyBusy:
    # An unknown status counts as busy, the same as in the adapter.
    @pytest.mark.parametrize("busy", ["busy", "waiting"])
    def test_read_only_waiter_holds_the_message_while_busy_then_wakes_when_idle(
        self, make_hooks: MakeHooks, busy: str
    ) -> None:
        hooks = make_hooks(tools="read_only")
        waiter = hooks.ready_waiter()
        hooks.fake.set_status(busy)
        time.sleep(SLICE_S)

        msg_id = hooks.send()
        time.sleep(SLICE_S)
        held = hooks.row(msg_id)
        still_waiting = waiter.running()
        hooks.fake.set_status("idle")

        assert held.status is Status.FALLBACK
        assert still_waiting
        assert waiter.exit_code() == 2
        assert msg_id in waiter.stderr()
        assert (hooks.row(msg_id).status, hooks.row(msg_id).via) == (Status.DELIVERED, "rewake")
