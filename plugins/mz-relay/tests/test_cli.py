"""User CLI: roster check, status, peers, held, approve, deny, context extend and log outputs."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import PLUGIN
from mz_relay import __version__
from mz_relay.cli import AGENT_ENV_MARKERS, Terminal, main
from mz_relay.client import RelayClient
from mz_relay.ids import new_id
from mz_relay.paths import Paths
from mz_relay.protocol import PROTOCOL_VERSION
from relay_rig import (
    ERIN_CORPORATE,
    FakeRelayd,
    Message,
    RawConn,
    Reply,
    fail,
    hello_then,
    ok,
    paths_in,
    pull_roster,
    running_daemon,
)

EXAMPLE_ROSTER = PLUGIN / "data" / "roster.example.toml"
REFUSAL = (
    "mz-relay: approve, deny and context extend must be run by the user in a terminal, "
    "not by an agent"
)
ISO_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
NO_SPAWN_ARGV = [
    ["held"],
    ["status"],
    ["stop"],
    ["approve", "m-12345678"],
    ["deny", "m-12345678"],
    ["log"],
]

Serve = Callable[[Reply], FakeRelayd]


@dataclass
class FakeTerminal:
    """The user's terminal: a tty flag, a scripted answer and the environment it runs in."""

    answer: str = "y"
    tty: bool = True
    env: dict[str, str] = field(default_factory=dict)
    prompts: list[str] = field(default_factory=list)

    def build(self) -> Terminal:
        return Terminal(isatty=lambda: self.tty, ask=self._ask, env=self.env)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


@dataclass
class Relay:
    """An in-process relayd plus client connections that keep agents online."""

    paths: Paths
    clients: list[RelayClient] = field(default_factory=list)

    def agent(self, name: str) -> tuple[RelayClient, str]:
        client = RelayClient(self.paths.socket)
        client.connect(client="mcp")
        self.clients.append(client)
        reg = client.call(
            "register",
            {
                "name": name,
                "harness": "opencode",
                "cwd": str(self.paths.config_dir),
                "pid": os.getpid(),
            },
        )
        return client, str(reg["token"])

    def send(
        self,
        client: RelayClient,
        token: str,
        to: str,
        body: str = "please review",
        msg_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> str:
        """A request, or a reply when in_reply_to is set; only a reply may reuse an existing id."""
        args = {"token": token, "id": msg_id or new_id(), "to": to, "kind": "request", "body": body}
        if in_reply_to is not None:
            args.update(kind="reply", in_reply_to=in_reply_to)
        return str(client.call("send", args)["id"])

    def sent_status(self, client: RelayClient, token: str, msg_id: str) -> str:
        """The status the sender's inbox `sent` list shows for msg_id."""
        sent = client.call("inbox", {"token": token})["sent"]
        assert isinstance(sent, list)
        [status] = [entry["status"] for entry in sent if entry["id"] == msg_id]
        return str(status)

    def add_agents(self, extra: str) -> None:
        """Rewrites the roster with extra agents; the mtime moves on so relayd reloads it."""
        later = self.paths.roster.stat().st_mtime_ns + 1_000_000_000
        self.paths.roster.write_text(pull_roster(self.paths.config_dir) + extra, encoding="utf-8")
        os.utime(self.paths.roster, ns=(later, later))

    def query(self, op: str, args: dict[str, object] | None = None) -> dict[str, object]:
        with closing(RelayClient(self.paths.socket)) as client:
            client.connect(client="cli")
            return client.call(op, args or {})

    def counts(self) -> dict[str, object]:
        counts = self.query("status")["counts"]
        assert isinstance(counts, dict)
        return {k: v for k, v in counts.items() if v}


@pytest.fixture
def cli_paths(relay_home: Path, agent_free_env: dict[str, str]) -> Paths:
    """The CLI's paths, with this process's environment free of agent-session variables."""
    return paths_in(relay_home)


@pytest.fixture
def relay(daemon_paths: Paths, agent_free_env: dict[str, str]) -> Iterator[Relay]:
    live = Relay(paths=daemon_paths)
    try:
        yield live
    finally:
        for client in live.clients:
            client.close()


class Proxy:
    """Forwards a CLI connection to an upstream relayd; `after_held` runs once, after `held`."""

    def __init__(self, upstream: Path, after_held: Callable[[], object]) -> None:
        self._upstream = upstream
        self._after_held: Callable[[], object] | None = after_held
        self._conn: RawConn | None = None

    def __call__(self, req: Message) -> Message | None:
        if self._conn is None:
            self._conn = RawConn(self._upstream)
        reply = self._conn.request(str(req["op"]), dict(req.get("args") or {}), rid=req["rid"])
        if req["op"] == "held" and self._after_held is not None:
            hook, self._after_held = self._after_held, None
            hook()
        return reply

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


@pytest.fixture
def upstream(relay_home: Path, agent_free_env: dict[str, str]) -> Iterator[Relay]:
    """A real relayd on its own home, for the CLI to reach through a Proxy."""
    paths = paths_in(relay_home / "up")
    with running_daemon(paths):
        live = Relay(paths=paths)
        try:
            yield live
        finally:
            for client in live.clients:
                client.close()


@pytest.fixture
def held_id(relay: Relay) -> str:
    client, carol = relay.agent("carol")
    relay.agent("alice")
    return relay.send(client, carol, "alice", body="corporate diff summary for review")


class TestRoster:
    def test_example_roster_checks_ok(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["roster", "check", str(EXAMPLE_ROSTER)]) == 0

        assert (
            capsys.readouterr().out == "ok: 5 agents: architect, builder, helper, reviewer, scout\n"
        )

    def test_default_path_is_the_configured_roster(
        self, cli_paths: Paths, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli_paths.ensure_dirs()
        cli_paths.roster.write_text(pull_roster(cli_paths.config_dir), encoding="utf-8")

        assert main(["roster", "check"]) == 0

        assert capsys.readouterr().out == "ok: 3 agents: alice, bob, carol\n"

    def test_invalid_roster_exits_2_with_roster_invalid(
        self, relay_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken = relay_home / "broken.toml"
        broken.write_text("version = 2\n", encoding="utf-8")

        assert main(["roster", "check", str(broken)]) == 2

        err = capsys.readouterr().err
        assert err.startswith("mz-relay: roster_invalid: ")
        assert "unsupported roster version 2" in err

    def test_missing_roster_file_exits_2(
        self, relay_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["roster", "check", str(relay_home / "absent.toml")]) == 2

        assert capsys.readouterr().err.startswith("mz-relay: roster_invalid: ")

    def test_a_cwd_through_a_symlink_loop_exits_2_naming_the_key(
        self, relay_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        loop = relay_home / "loop"
        loop.symlink_to(loop)
        roster = relay_home / "looping.toml"
        roster.write_text(
            pull_roster(relay_home).replace(f'cwd = "{relay_home / "alice"}"', f'cwd = "{loop}"'),
            encoding="utf-8",
        )

        assert main(["roster", "check", str(roster)]) == 2

        err = capsys.readouterr().err
        assert err.startswith("mz-relay: roster_invalid: ")
        assert "agents.alice.cwd" in err


class TestApprove:
    def test_agent_markers_are_the_documented_five(self) -> None:
        assert AGENT_ENV_MARKERS == (
            "CLAUDECODE",
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CODEX_THREAD_ID",
            "OPENCODE",
            "PI_CODING_AGENT",
        )

    @pytest.mark.parametrize("command", ["approve", "deny"])
    def test_without_a_tty_the_command_is_refused(
        self, relay: Relay, held_id: str, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = FakeTerminal(tty=False)

        assert main([command, held_id], terminal=user.build()) == 2

        captured = capsys.readouterr()
        assert REFUSAL in captured.out + captured.err
        assert user.prompts == []
        assert relay.counts() == {"held": 1}

    @pytest.mark.parametrize("marker", AGENT_ENV_MARKERS)
    def test_an_agent_env_marker_refuses_even_with_a_tty(
        self, relay: Relay, held_id: str, marker: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = FakeTerminal(env={marker: "1"})

        assert main(["approve", held_id], terminal=user.build()) == 2

        captured = capsys.readouterr()
        assert REFUSAL in captured.out + captured.err
        assert user.prompts == []
        assert relay.counts() == {"held": 1}

    @pytest.mark.parametrize("answer", ["y", "yes"])
    def test_user_yes_approves_and_queues_the_message(
        self, relay: Relay, held_id: str, answer: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = FakeTerminal(answer=answer)

        assert main(["approve", held_id], terminal=user.build()) == 0

        out = capsys.readouterr().out
        assert f"{held_id}: carol (corporate) → alice (personal)" in out
        assert "corporate diff summary for review" in out
        assert out.splitlines()[-1] == f"approved {held_id}"
        assert user.prompts == ["Approve? [y/N] "]
        assert relay.counts() == {"queued": 1}

    @pytest.mark.parametrize("answer", ["n", "", "maybe"])
    def test_anything_but_yes_leaves_the_message_held(
        self, relay: Relay, held_id: str, answer: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = FakeTerminal(answer=answer)

        assert main(["approve", held_id], terminal=user.build()) == 1

        assert capsys.readouterr().out.splitlines()[-1] == "not approved"
        assert relay.counts() == {"held": 1}

    def test_deny_yes_denies_with_the_given_reason(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, carol = relay.agent("carol")
        msg_id = relay.send(client, carol, "alice")
        user = FakeTerminal()

        assert main(["deny", msg_id, "--reason", "policy"], terminal=user.build()) == 0

        assert capsys.readouterr().out.splitlines()[-1] == f"denied {msg_id}"
        assert user.prompts == ["Deny? [y/N] "]
        assert relay.counts() == {"denied": 1}
        inbox = client.call("inbox", {"token": carol})
        assert f"- {msg_id} → alice: denied (policy)" in str(inbox["text"]).splitlines()

    def test_deny_no_keeps_the_message_held(
        self, relay: Relay, held_id: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["deny", held_id], terminal=FakeTerminal(answer="n").build()) == 1

        assert capsys.readouterr().out.splitlines()[-1] == "not denied"
        assert relay.counts() == {"held": 1}

    @pytest.mark.parametrize("command", ["approve", "deny"])
    def test_an_unknown_id_is_not_found_without_asking(
        self, relay: Relay, held_id: str, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        user = FakeTerminal()

        assert main([command, new_id()], terminal=user.build()) == 1

        assert capsys.readouterr().err.startswith("mz-relay: not_found: ")
        assert user.prompts == []
        assert relay.counts() == {"held": 1}

    @pytest.mark.parametrize("command", ["approve", "deny"])
    def test_an_id_that_is_not_held_is_not_held_without_asking(
        self, relay: Relay, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, alice = relay.agent("alice")
        queued = relay.send(client, alice, "bob")
        user = FakeTerminal()

        assert main([command, queued], terminal=user.build()) == 1

        assert capsys.readouterr().err.startswith("mz-relay: not_held: ")
        assert user.prompts == []
        assert relay.counts() == {"queued": 1}

    def test_approve_shows_the_first_200_chars_of_a_long_body(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, carol = relay.agent("carol")
        msg_id = relay.send(client, carol, "alice", body="z" * 250)

        assert main(["approve", msg_id], terminal=FakeTerminal().build()) == 0

        out = capsys.readouterr().out
        assert "z" * 200 + "…" in out
        assert "z" * 201 not in out

    @pytest.mark.parametrize("argv", NO_SPAWN_ARGV)
    def test_no_spawn_commands_exit_3_when_relayd_is_down(
        self, cli_paths: Paths, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(argv, terminal=FakeTerminal().build()) == 3

        assert capsys.readouterr().err == "mz-relay: relayd is not running\n"
        assert not cli_paths.socket.exists()


class TestApproveTargetsOneRow:
    def test_approve_targets_the_shown_row_when_another_sender_reused_its_id(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        carol_client, carol = relay.agent("carol")
        alice_client, alice = relay.agent("alice")
        bob_client, bob = relay.agent("bob")
        shared = relay.send(carol_client, carol, "alice", msg_id=new_id())
        question = relay.send(bob_client, bob, "alice")
        # Ids are unique per sender only; alice's queued reply is the newer row with this id.
        relay.send(alice_client, alice, "bob", msg_id=shared, in_reply_to=question)

        assert main(["approve", shared], terminal=FakeTerminal().build()) == 0

        out = capsys.readouterr().out
        assert f"{shared}: carol (corporate) → alice (personal)" in out
        assert out.splitlines()[-1] == f"approved {shared}"
        assert relay.sent_status(carol_client, carol, shared) == "queued"
        assert relay.counts() == {"queued": 3}

    @pytest.mark.parametrize("command", ["approve", "deny"])
    def test_two_held_rows_sharing_an_id_change_at_most_the_shown_one(
        self, relay: Relay, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        relay.add_agents(ERIN_CORPORATE)
        senders = {name: relay.agent(name) for name in ("erin", "carol")}
        alice_client, alice = relay.agent("alice")
        question = relay.send(alice_client, alice, "carol")
        shared = relay.send(*senders["erin"], "alice", msg_id=new_id())
        # A new chain may not reuse a context id, so carol reuses erin's id in a reply.
        relay.send(*senders["carol"], "alice", msg_id=shared, in_reply_to=question)

        code = main([command, shared], terminal=FakeTerminal().build())

        out = capsys.readouterr().out
        statuses = {name: relay.sent_status(c, tok, shared) for name, (c, tok) in senders.items()}
        changed = [name for name, status in statuses.items() if status != "held"]
        if code == 0:
            [name] = changed
            assert f"{shared}: {name} (corporate) → alice (personal)" in out
        else:
            assert (code, changed) == (1, [])


class TestApproveNeverProbes:
    """A message held after the lookup must stay held: approve and deny only decide on a yes."""

    @pytest.mark.parametrize("command", ["approve", "deny"])
    @pytest.mark.parametrize(
        ("existing", "code"), [(None, "not_found"), ("bob", "not_held")], ids=["unknown", "queued"]
    )
    def test_a_message_held_right_after_the_lookup_is_not_decided(
        self,
        upstream: Relay,
        fake_relayd: Serve,
        command: str,
        existing: str | None,
        code: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        carol_client, carol = upstream.agent("carol")
        alice_client, alice = upstream.agent("alice")
        msg_id = new_id()
        question = upstream.send(alice_client, alice, "carol")
        # Pulled now, so the later inbox reads of carol leave the counts alone.
        carol_client.call("inbox", {"token": carol})
        if existing is not None:
            upstream.send(alice_client, alice, existing, msg_id=msg_id)
        before = upstream.counts()
        # carol answers in a reply, since a new chain may not reuse the id of alice's context.
        proxy = Proxy(
            upstream.paths.socket,
            lambda: upstream.send(
                carol_client, carol, "alice", msg_id=msg_id, in_reply_to=question
            ),
        )
        server = fake_relayd(proxy)
        user = FakeTerminal(answer="n")
        try:
            exit_code = main([command, msg_id], terminal=user.build())
        finally:
            proxy.close()

        assert exit_code == 1
        assert capsys.readouterr().err.startswith(f"mz-relay: {code}: ")
        assert user.prompts == []
        assert "decide" not in server.ops()
        assert upstream.sent_status(carol_client, carol, msg_id) == "held"
        assert upstream.counts() == {**before, "held": 1}


class TestDaemonErrors:
    @pytest.mark.parametrize("argv", NO_SPAWN_ARGV)
    def test_a_daemon_refusing_hello_is_a_relay_error_exit_1(
        self,
        cli_paths: Paths,
        fake_relayd: Serve,
        argv: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_relayd(
            lambda req: fail(req, "protocol_mismatch", "relayd speaks 2", {"daemon_protocol": 2})
        )

        assert main(argv, terminal=FakeTerminal().build()) == 1

        assert capsys.readouterr().err == "mz-relay: protocol_mismatch: relayd speaks 2\n"


class TestStop:
    def test_stop_shuts_down_an_old_protocol_daemon_without_hello(
        self, cli_paths: Paths, fake_relayd: Serve, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def old_daemon(req: Message) -> Message:
            if req["op"] == "shutdown":
                return ok(req, {})
            return fail(req, "protocol_mismatch", "relayd speaks 0", {"daemon_protocol": 0})

        server = fake_relayd(old_daemon)

        assert main(["stop"]) == 0

        # The refused hello's socket is closed, so shutdown goes out on a fresh one, unannounced.
        assert server.ops() == ["hello", "shutdown"]
        # A refused hello carries no daemon_pid, so the stop line has no pid to name.
        assert capsys.readouterr() == ("stopped relayd\n", "")


class TestOutputs:
    def test_status_lists_daemon_paths_agents_and_counts(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alice_client, alice = relay.agent("alice")
        carol_client, carol = relay.agent("carol")
        relay.send(alice_client, alice, "bob")
        relay.send(carol_client, carol, "alice")

        assert main(["status"]) == 0

        lines = capsys.readouterr().out.splitlines()
        version = re.escape(__version__)
        assert re.fullmatch(
            rf"relayd pid \d+, protocol {PROTOCOL_VERSION}, version {version}, \d+ connection\(s\)",
            lines[0],
        )
        assert lines[1] == f"db: {relay.paths.db}"
        assert lines[2] == f"roster: {relay.paths.roster}"
        assert lines[3:7] == [
            "agents:",
            "  alice  opencode  online",
            "  bob  opencode  offline",
            "  carol  opencode  online",
        ]
        assert lines[-1] == "counts: queued=1 held=1"

    def test_status_with_no_messages_says_counts_none(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["status"]) == 0

        assert capsys.readouterr().out.splitlines()[-1] == "counts: none"

    def test_peers_prints_tab_separated_columns_sorted_by_name(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        relay.agent("alice")
        relay.agent("carol")

        assert main(["peers"]) == 0

        assert capsys.readouterr().out.splitlines() == [
            "alice\topencode\tpersonal\tplanner\tonline",
            "bob\topencode\tpersonal\tbuilder\toffline",
            "carol\topencode\tcorporate\tauditor\tonline",
        ]

    def test_held_line_has_route_time_and_cut_preview(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, carol = relay.agent("carol")
        msg_id = relay.send(client, carol, "alice", body="line one\n" + "x" * 100)

        assert main(["held"]) == 0

        [line] = capsys.readouterr().out.splitlines()
        columns = line.split("\t")
        assert columns[0] == msg_id
        assert columns[1] == "carol (corporate) → alice (personal)"
        assert ISO_TIME.fullmatch(columns[2])
        assert columns[3] == "line one " + "x" * 51 + "…"

    def test_held_with_nothing_held_says_so(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["held"]) == 0

        assert capsys.readouterr().out == "no held messages\n"


def extend_as(user: FakeTerminal, *argv: str) -> int:
    return main(["context", "extend", *argv], terminal=user.build())


class TestContextExtend:
    """Run as the user at a terminal unless a test says otherwise."""

    @pytest.fixture
    def ctx(self, relay: Relay) -> str:
        client, alice = relay.agent("alice")
        return relay.send(client, alice, "bob")

    @pytest.fixture
    def idle_relayd(self, cli_paths: Paths, fake_relayd: Serve) -> FakeRelayd:
        return fake_relayd(hello_then(lambda req: ok(req, {"context_id": "c", "turn_cap": 1})))

    def test_by_default_the_cap_grows_by_20(
        self, ctx: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert extend_as(FakeTerminal(), ctx) == 0

        assert capsys.readouterr() == (f"context {ctx} turn cap is now 40\n", "")

    def test_by_sets_the_growth(self, ctx: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert extend_as(FakeTerminal(), ctx, "--by", "5") == 0

        assert capsys.readouterr().out == f"context {ctx} turn cap is now 25\n"

    def test_by_may_be_the_shared_maximum(
        self, ctx: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mz_relay.service import CONTEXT_EXTEND_MAX

        assert CONTEXT_EXTEND_MAX == 100
        assert extend_as(FakeTerminal(), ctx, "--by", str(CONTEXT_EXTEND_MAX)) == 0

        assert capsys.readouterr().out == f"context {ctx} turn cap is now 120\n"

    def test_the_default_adds_to_the_roster_cap(
        self, relay: Relay, ctx: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        relay.add_agents("\n[policy]\nturn_cap = 5\n")

        assert extend_as(FakeTerminal(), ctx) == 0

        assert capsys.readouterr().out == f"context {ctx} turn cap is now 25\n"

    def test_an_unknown_context_is_not_found(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = new_id()

        assert extend_as(FakeTerminal(), missing) == 1

        assert capsys.readouterr().err == f"mz-relay: not_found: no context {missing}\n"

    @pytest.mark.parametrize("by", ["0", "-3", "many", "101"])
    def test_by_outside_1_to_100_is_a_usage_error_and_nothing_is_sent(
        self, idle_relayd: FakeRelayd, by: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as usage:
            extend_as(FakeTerminal(), new_id(), "--by", by)

        assert usage.value.code == 2
        assert "--by" in capsys.readouterr().err
        assert idle_relayd.ops() == []

    def test_without_a_tty_it_is_refused_and_nothing_is_sent(
        self, idle_relayd: FakeRelayd, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert extend_as(FakeTerminal(tty=False), new_id()) == 2

        assert capsys.readouterr() == ("", REFUSAL + "\n")
        assert idle_relayd.ops() == []

    @pytest.mark.parametrize("marker", AGENT_ENV_MARKERS)
    def test_an_agent_env_marker_refuses_even_with_a_tty(
        self, idle_relayd: FakeRelayd, marker: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert extend_as(FakeTerminal(env={marker: "1"}), new_id()) == 2

        assert capsys.readouterr() == ("", REFUSAL + "\n")
        assert idle_relayd.ops() == []


HELD_ID = "01JC0000000000000000000001"
ROUTE = "carol (corporate) → alice (personal)"
# Peer bytes that would erase or rewrite terminal lines: ESC, CR, BEL, CSI (C1), NUL and DEL.
HOSTILE = "ok\x1b[1A\x1b[2K\rforged\x07\x9b2J\x00\x7f"
SHOWN = "ok\ufffd[1A\ufffd[2K\ufffdforged\ufffd\ufffd2J\ufffd\ufffd"
CONTROLS = [
    "\x00",
    "\x07",
    "\x08",
    "\t",
    "\x0b",
    "\x0c",
    "\r",
    "\x1b",
    "\x7f",
    "\x80",
    "\x9b",
    "\x9f",
]
# Line separators and bidi controls that could reorder or split what the terminal shows.
LINE_AND_BIDI = [
    "\u061c",
    "\u200e",
    "\u200f",
    "\u2028",
    "\u2029",
    "\u202a",
    "\u202e",
    "\u2066",
    "\u2069",
]


def holding(preview: str) -> Reply:
    """A relayd holding one carol → alice message with `preview`, for held and held {id}."""
    msg: Message = {
        "id": HELD_ID,
        "source": "carol",
        "source_label": "corporate",
        "to": "alice",
        "to_label": "personal",
        "time": "2026-11-13T10:00:00.000Z",
        "preview": preview,
    }
    return hello_then(lambda req: ok(req, {"messages": [msg]}) if req["op"] == "held" else None)


class TestPreviewControlChars:
    """Each control char in a peer's preview prints as U+FFFD; cuts and newlines work as before."""

    @pytest.fixture
    def held_line(
        self, cli_paths: Paths, fake_relayd: Serve, capsys: pytest.CaptureFixture[str]
    ) -> Callable[[str], list[str]]:
        def run(preview: str) -> list[str]:
            fake_relayd(holding(preview))
            assert main(["held"]) == 0
            [line] = capsys.readouterr().out.splitlines()
            return line.split("\t")

        return run

    @pytest.fixture
    def approve_lines(
        self, cli_paths: Paths, fake_relayd: Serve, capsys: pytest.CaptureFixture[str]
    ) -> Callable[[str], list[str]]:
        def run(preview: str) -> list[str]:
            fake_relayd(holding(preview))
            user = FakeTerminal(answer="n")
            assert main(["approve", HELD_ID], terminal=user.build()) == 1
            assert user.prompts == ["Approve? [y/N] "]
            return capsys.readouterr().out.split("\n")

        return run

    def test_held_replaces_escapes_and_keeps_the_route_column(
        self, held_line: Callable[[str], list[str]]
    ) -> None:
        columns = held_line(HOSTILE)

        assert columns[:2] == [HELD_ID, ROUTE]
        assert columns[3:] == [SHOWN]

    @pytest.mark.parametrize("char", CONTROLS, ids=[f"{ord(c):#04x}" for c in CONTROLS])
    def test_held_replaces_each_c0_del_and_c1_char(
        self, held_line: Callable[[str], list[str]], char: str
    ) -> None:
        assert held_line(f"a{char}b")[3:] == ["a\ufffdb"]

    @pytest.mark.parametrize("char", LINE_AND_BIDI, ids=[f"{ord(c):#06x}" for c in LINE_AND_BIDI])
    def test_held_replaces_each_line_separator_and_bidi_control(
        self, held_line: Callable[[str], list[str]], char: str
    ) -> None:
        assert held_line(f"a{char}b")[3:] == ["a\ufffdb"]

    def test_held_still_flattens_crlf_to_one_space(
        self, held_line: Callable[[str], list[str]]
    ) -> None:
        assert held_line("one\r\ntwo\x1b[2K")[3] == "one two\ufffd[2K"

    def test_held_cuts_at_60_chars_counting_each_replacement_as_one(
        self, held_line: Callable[[str], list[str]]
    ) -> None:
        assert held_line("\x1b" * 70)[3] == "\ufffd" * 60 + "…"

    def test_approve_keeps_the_route_line_and_body_newlines(
        self, approve_lines: Callable[[str], list[str]]
    ) -> None:
        lines = approve_lines("line one\n" + HOSTILE)

        assert lines == [f"{HELD_ID}: {ROUTE}", "line one", SHOWN, "not approved", ""]

    @pytest.mark.parametrize("char", CONTROLS, ids=[f"{ord(c):#04x}" for c in CONTROLS])
    def test_approve_replaces_each_c0_del_and_c1_char(
        self, approve_lines: Callable[[str], list[str]], char: str
    ) -> None:
        assert approve_lines(f"a{char}b")[1] == "a\ufffdb"

    @pytest.mark.parametrize("char", LINE_AND_BIDI, ids=[f"{ord(c):#06x}" for c in LINE_AND_BIDI])
    def test_approve_replaces_each_line_separator_and_bidi_control(
        self, approve_lines: Callable[[str], list[str]], char: str
    ) -> None:
        assert approve_lines(f"a{char}b")[1] == "a\ufffdb"

    def test_approve_keeps_the_200_char_preview_length(
        self, approve_lines: Callable[[str], list[str]]
    ) -> None:
        assert approve_lines("\x07" * 200 + "…")[1] == "\ufffd" * 200 + "…"


LOG_ID = "01JC0000000000000000000002"
LOG_TIME = "2026-11-13T10:00:00.000Z"
LOG_HEAD = f"{LOG_TIME}  {LOG_ID}  alice → bob  request"


def log_row(preview: str = "please review", reason: str | None = None) -> Message:
    """One alice → bob row as relayd's log op returns it."""
    return {
        "id": LOG_ID,
        "source": "alice",
        "to": "bob",
        "kind": "request",
        "status": "queued",
        "reason": reason,
        "time": LOG_TIME,
        "preview": preview,
    }


def serving_log(*rows: Message) -> Reply:
    return hello_then(lambda req: ok(req, {"messages": list(rows)}) if req["op"] == "log" else None)


class TestLog:
    """`log` lines are `<time>  <id>  <source> → <to>  <kind>  <status>[ (<reason>)]  <preview>`."""

    @pytest.fixture
    def printed(
        self, cli_paths: Paths, fake_relayd: Serve, capsys: pytest.CaptureFixture[str]
    ) -> Callable[..., str]:
        def run(*rows: Message) -> str:
            fake_relayd(serving_log(*rows))
            assert main(["log"]) == 0
            return capsys.readouterr().out

        return run

    @staticmethod
    def columns(out: str) -> list[list[str]]:
        # Safe only because the bodies and reasons these tests produce hold no double space.
        return [line.split("  ") for line in out.splitlines()]

    def test_a_row_prints_time_id_route_kind_status_and_preview(
        self, printed: Callable[..., str]
    ) -> None:
        assert printed(log_row()) == f"{LOG_HEAD}  queued  please review\n"

    def test_a_reason_follows_the_status_in_parentheses(self, printed: Callable[..., str]) -> None:
        out = printed(log_row(reason="attempt 1 failed: socket busy"))

        assert out == f"{LOG_HEAD}  queued (attempt 1 failed: socket busy)  please review\n"

    @pytest.mark.parametrize(
        ("preview", "shown"),
        [
            ("line one\n" + "x" * 191 + "…", "line one " + "x" * 51 + "…"),
            ("x" * 60, "x" * 60),
            ("\x1b" * 70, "\ufffd" * 60 + "…"),
        ],
        ids=["cut-at-60", "exactly-60", "escaped-then-cut"],
    )
    def test_preview_is_cut_to_60_chars_with_newlines_as_spaces(
        self, printed: Callable[..., str], preview: str, shown: str
    ) -> None:
        assert printed(log_row(preview=preview)) == f"{LOG_HEAD}  queued  {shown}\n"

    def test_control_chars_in_preview_and_reason_show_as_replacement_chars(
        self, printed: Callable[..., str]
    ) -> None:
        out = printed(log_row(preview=HOSTILE, reason=HOSTILE))

        assert out == f"{LOG_HEAD}  queued ({SHOWN})  {SHOWN}\n"

    def test_a_reason_spanning_lines_prints_on_one(self, printed: Callable[..., str]) -> None:
        out = printed(log_row(reason="socket gone\nretrying"))

        assert out == f"{LOG_HEAD}  queued (socket gone retrying)  please review\n"

    def test_no_rows_prints_no_messages(self, printed: Callable[..., str]) -> None:
        assert printed() == "no messages\n"

    def test_context_and_limit_are_passed_to_relayd(
        self, cli_paths: Paths, fake_relayd: Serve
    ) -> None:
        server = fake_relayd(serving_log())
        ctx = new_id()

        assert main(["log", "--context", ctx, "--limit", "2"]) == 0

        assert server.args_of("log") == [{"context_id": ctx, "limit": 2}]

    def test_without_options_no_context_is_asked_for(
        self, cli_paths: Paths, fake_relayd: Serve
    ) -> None:
        server = fake_relayd(serving_log())

        assert main(["log"]) == 0

        [args] = server.args_of("log")
        assert args.get("context_id") is None
        # Leaving the default to relayd or sending it explicitly reads the same rows.
        assert args.get("limit") in (None, 50)

    @pytest.mark.parametrize("limit", ["1", "200"])
    def test_the_limit_bounds_are_sent_as_given(
        self, cli_paths: Paths, fake_relayd: Serve, limit: str
    ) -> None:
        server = fake_relayd(serving_log())

        assert main(["log", "--limit", limit]) == 0

        [args] = server.args_of("log")
        assert args.get("limit") == int(limit)

    @pytest.mark.parametrize("limit", ["0", "-1", "201", "many"])
    def test_a_limit_outside_1_to_200_is_a_usage_error_and_nothing_is_sent(
        self,
        cli_paths: Paths,
        fake_relayd: Serve,
        limit: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        server = fake_relayd(serving_log())

        with pytest.raises(SystemExit) as usage:
            main(["log", "--limit", limit])

        assert usage.value.code == 2
        assert "--limit" in capsys.readouterr().err
        assert server.ops() == []

    def test_an_empty_context_is_a_usage_error_and_nothing_is_sent(
        self, cli_paths: Paths, fake_relayd: Serve, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = fake_relayd(serving_log())

        with pytest.raises(SystemExit) as usage:
            main(["log", "--context", ""])

        assert usage.value.code == 2
        assert "--context" in capsys.readouterr().err
        assert server.ops() == []

    def test_an_agent_session_may_read_the_log(
        self, printed: Callable[..., str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unlike approve and deny, reading the log lifts no guard.
        monkeypatch.setenv("CLAUDECODE", "1")

        assert printed(log_row()) == f"{LOG_HEAD}  queued  please review\n"

    def test_context_prints_that_contexts_rows_oldest_first(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alice_client, alice = relay.agent("alice")
        bob_client, bob = relay.agent("bob")
        question = relay.send(alice_client, alice, "bob", body="which port?")
        relay.send(alice_client, alice, "bob", body="an unrelated task")
        answer = relay.send(bob_client, bob, "alice", body="8080", in_reply_to=question)

        assert main(["log", "--context", question]) == 0

        rows = self.columns(capsys.readouterr().out)
        assert [row[1:4] for row in rows] == [
            [question, "alice → bob", "request"],
            [answer, "bob → alice", "reply"],
        ]
        assert all(ISO_TIME.fullmatch(row[0]) for row in rows)
        assert [row[-1] for row in rows] == ["which port?", "8080"]

    def test_limit_2_prints_the_two_newest_rows_oldest_first(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, alice = relay.agent("alice")
        ids = [relay.send(client, alice, "bob", body=f"task {n}") for n in range(3)]

        assert main(["log", "--limit", "2"]) == 0

        assert [row[1] for row in self.columns(capsys.readouterr().out)] == ids[1:]

    def test_a_200_char_body_previews_as_60_chars_and_an_ellipsis(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, carol = relay.agent("carol")
        msg_id = relay.send(client, carol, "alice", body="b" * 200)

        assert main(["log"]) == 0

        [row] = self.columns(capsys.readouterr().out)
        assert row[1:] == [msg_id, "carol → alice", "request", "held", "b" * 60 + "…"]

    def test_an_unknown_context_prints_no_messages(
        self, relay: Relay, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, alice = relay.agent("alice")
        relay.send(client, alice, "bob")

        assert main(["log", "--context", new_id()]) == 0

        assert capsys.readouterr() == ("no messages\n", "")
