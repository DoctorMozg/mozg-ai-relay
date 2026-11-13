"""Waiter.run against a fake relayd: re-registering after a hang-up, a lease taken while busy."""

from __future__ import annotations

import io
import itertools
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fakes_claude import CWD, FakeClaudeSession
from mz_relay.clock import SystemClock
from mz_relay.hooks import HookInput, Waiter
from mz_relay.paths import Paths
from relay_rig import (
    PROC_WAIT_S,
    WAIT_S,
    FakeRelayd,
    InThread,
    Message,
    NoSpawn,
    Reply,
    envelope,
    fail,
    hello_then,
    launcher_for,
    mid,
    ok,
    token,
    wait_until,
)

FIRST = mid(8001)
SECOND = mid(8002)
# Longer than the waiter's 1 s wait slice, so it has acted on the registry status by then.
SLICE_S = 1.5


def card(tools: str) -> Message:
    return {
        "name": "cc",
        "role": "reviewer",
        "harness": "claude",
        "label": "personal",
        "description": "Reviews changes in a Claude session.",
        "tools": tools,
    }


def leased(msg_id: str) -> Message:
    """A `wait` result carrying one leased message, as relayd renders it."""
    env = envelope(msg_id, source="sender", to="cc", body=f"body of {msg_id}")
    text = f"[mz-relay] Peer message from sender to cc.\nid: {msg_id}\n  body of {msg_id}"
    return {"message": {"envelope": env.to_json(), "text": text}}


class LoopRig:
    """A live fake session for `cc` (this process's pid), a scripted relayd and a Waiter."""

    def __init__(
        self,
        paths: Paths,
        fake: FakeClaudeSession,
        start_relayd: Callable[[Reply], FakeRelayd],
        *,
        tools: str,
        on_wait: Callable[[int, Message], Message | None],
    ) -> None:
        self.fake = fake
        self.spawn = NoSpawn()
        tokens = itertools.count(1)
        waits = itertools.count(1)

        def answer(req: Message) -> Message | None:
            op = req["op"]
            if op == "register":
                result = {
                    "token": token(next(tokens)),
                    "agent": card(tools),
                    "delivery_mode": "fallback",
                    "brief": 'You are "cc" on mz-relay.',
                }
                return ok(req, result)
            if op == "wait":
                return on_wait(next(waits), req)
            if op == "confirm":
                return ok(req, {"id": req["args"]["id"], "status": "delivered"})
            return fail(req, "unknown_op", f"unknown op {op!r}")

        self.relayd = start_relayd(hello_then(answer))
        self.fake.writer.human("start the review")
        env = {"CLAUDE_CONFIG_DIR": str(fake.config_dir), "MZ_RELAY_NAME": "cc"}
        self.waiter = Waiter(paths, launcher_for(paths, self.spawn), env=env, clock=SystemClock())
        self.stderr = io.StringIO()

    def start(self) -> InThread[int]:
        hook = HookInput(
            session_id=self.fake.session_id,
            cwd=Path(CWD),
            transcript_path=self.fake.transcript_path,
            tool_name=None,
            event="SessionStart",
            source="startup",
        )
        return InThread(lambda: self.waiter.run("SessionStart", hook, self.stderr))

    def confirmed_ids(self) -> list[object]:
        return [args["id"] for args in self.relayd.args_of("confirm")]


MakeLoop = Callable[..., LoopRig]


@pytest.fixture
def session(relay_home: Path) -> Iterator[FakeClaudeSession]:
    with FakeClaudeSession(relay_home / "c", pid=os.getpid()) as fake:
        yield fake


@pytest.fixture
def make_loop(
    relay_paths: Paths, session: FakeClaudeSession, fake_relayd: Callable[[Reply], FakeRelayd]
) -> MakeLoop:
    def build(*, tools: str, on_wait: Callable[[int, Message], Message | None]) -> LoopRig:
        return LoopRig(relay_paths, session, fake_relayd, tools=tools, on_wait=on_wait)

    return build


def idle_timeout(req: Message) -> Message:
    # A real wait parks for its slice; answering at once would make the waiter spin.
    time.sleep(0.05)
    return ok(req, {"timeout": True})


class TestReconnect:
    def test_waiter_registers_again_after_relayd_hangs_up_and_still_delivers(
        self, make_loop: MakeLoop
    ) -> None:
        def on_wait(n: int, req: Message) -> Message | None:
            # None closes the connection: the waiter sees EOF in the middle of its wait call.
            if n == 1:
                return None
            return ok(req, leased(FIRST)) if n == 2 else idle_timeout(req)

        loop = make_loop(tools="full", on_wait=on_wait)

        code = loop.start().result(PROC_WAIT_S)

        assert code == 2
        assert f"  body of {FIRST}" in loop.stderr.getvalue()
        registers = loop.relayd.args_of("register")
        assert len(registers) == 2
        assert all(args["delivery"] == loop.fake.rewake_binding() for args in registers)
        # A second hello means a second connection; the confirm uses the new registration.
        assert loop.relayd.ops().count("hello") == 2
        assert loop.relayd.args_of("confirm") == [{"token": token(2), "id": FIRST}]
        # relayd kept listening, so the waiter reconnected without spawning another.
        assert loop.spawn.calls == []


class TestLeasedWhileBusy:
    def test_a_lease_taken_as_the_session_turns_busy_is_neither_printed_nor_confirmed(
        self, make_loop: MakeLoop, session: FakeClaudeSession
    ) -> None:
        def on_wait(n: int, req: Message) -> Message | None:
            if n == 1:
                # The session starts a turn between the waiter's idle check and the lease.
                session.set_status("busy")
                return ok(req, leased(FIRST))
            return ok(req, leased(SECOND)) if n == 2 else idle_timeout(req)

        loop = make_loop(tools="read_only", on_wait=on_wait)

        running = loop.start()
        assert wait_until(lambda: len(loop.relayd.args_of("wait")) >= 1, WAIT_S)
        time.sleep(SLICE_S)
        held_back = (running.done(), loop.stderr.getvalue(), loop.confirmed_ids())
        session.set_status("idle")
        code = running.result(PROC_WAIT_S)

        # While busy: still looping, nothing printed, FIRST left to its lease running out.
        assert held_back == (False, "", [])
        assert code == 2
        err = loop.stderr.getvalue()
        assert f"body of {SECOND}" in err
        assert FIRST not in err
        assert loop.confirmed_ids() == [SECOND]
