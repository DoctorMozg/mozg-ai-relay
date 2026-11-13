"""RelayToolBackend against an in-process relayd, UnavailableBackend and `mz-relay mcp`."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mz_relay import __version__
from mz_relay.client import RelayClient
from mz_relay.clock import SystemClock
from mz_relay.envelope import Kind
from mz_relay.errors import RelayError
from mz_relay.launcher import DaemonLauncher
from mz_relay.mcp_server import McpServer, SendArgs
from mz_relay.paths import Paths
from mz_relay.presence import PresenceSession, RegisterArgs
from mz_relay.roster import Harness
from mz_relay.tool_backend import RelayToolBackend, UnavailableBackend
from relay_rig import PROC_WAIT_S, McpProc, NoSpawn, launcher_for, run_in_thread

HELD_TEXT = "held {id} for user approval — ask your user to run: mz-relay approve {id}"


def request(to: str = "bob", body: str = "please review src/app.py") -> SendArgs:
    return SendArgs(to=to, body=body, kind=Kind.REQUEST, context_id=None, in_reply_to=None)


def tool_call(server: McpServer, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    reply: dict[str, Any] = json.loads(server.handle_line(json.dumps(frame)) or "")
    return reply


class IdSource:
    """Hands out predictable message ids and remembers each one issued."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.issued: list[str] = []

    def __call__(self) -> str:
        msg_id = f"{self.prefix}-msg-{len(self.issued) + 1:04d}"
        self.issued.append(msg_id)
        return msg_id


class FailingLauncher(DaemonLauncher):
    """A launcher whose first `failures` connects fail the way a vanished relayd does."""

    def __init__(self, paths: Paths, failures: int) -> None:
        super().__init__(paths, clock=SystemClock(), spawn=NoSpawn(), command=["/bin/true"], env={})
        self.failures = failures
        self.connects = 0
        self._missing = paths.runtime_dir / "gone.sock"

    def connect(self, *, client: str, wait_s: float = 3.0) -> RelayClient:
        self.connects += 1
        if self.connects <= self.failures:
            # A client on a missing socket raises the real DaemonUnavailable.
            RelayClient(self._missing, timeout_s=0.5).connect(client=client)
        return super().connect(client=client, wait_s=wait_s)


@dataclass
class Agent:
    backend: RelayToolBackend
    presence: PresenceSession
    ids: IdSource


@pytest.fixture
def agents(daemon_paths: Paths) -> Iterator[dict[str, Agent]]:
    spawn = NoSpawn()
    started: dict[str, Agent] = {}
    try:
        for name in ("alice", "bob", "carol"):
            launcher = launcher_for(daemon_paths, spawn)
            args = RegisterArgs(
                name=name,
                harness=Harness.OPENCODE,
                cwd=daemon_paths.config_dir,
                pid=os.getpid(),
                client="mcp",
            )
            presence = PresenceSession(launcher, args, clock=SystemClock())
            presence.start()
            ids = IdSource(name)
            backend = RelayToolBackend(presence, launcher, new_id=ids)
            started[name] = Agent(backend=backend, presence=presence, ids=ids)
        yield started
    finally:
        for agent in started.values():
            agent.presence.close()
    # relayd runs in this process, so a spawn would mean the launcher missed it.
    assert spawn.calls == []


class TestRelayToolBackend:
    def test_send_returns_relayd_queued_text(self, agents: dict[str, Agent]) -> None:
        alice = agents["alice"]

        text = alice.backend.send(request())

        assert text == f"queued {alice.ids.issued[0]} for bob"

    def test_corporate_send_returns_the_exact_held_text(self, agents: dict[str, Agent]) -> None:
        carol = agents["carol"]

        text = carol.backend.send(request(to="alice"))

        assert text == HELD_TEXT.format(id=carol.ids.issued[0])

    def test_inbox_shows_the_rendered_peer_message(self, agents: dict[str, Agent]) -> None:
        agents["alice"].backend.send(request(body="see src/app.py line 40"))

        text = agents["bob"].backend.inbox(20)

        assert text.startswith("[mz-relay] Peer message from alice")
        assert "  see src/app.py line 40" in text

    def test_ack_returns_acked_text(self, agents: dict[str, Agent]) -> None:
        agents["alice"].backend.send(request())
        agents["bob"].backend.inbox(20)
        msg_id = agents["alice"].ids.issued[0]

        assert agents["bob"].backend.ack(msg_id) == f"acked {msg_id}"

    def test_peers_lists_the_two_other_agents(self, agents: dict[str, Agent]) -> None:
        lines = agents["alice"].backend.peers().splitlines()

        assert [line.split(" : ")[0] for line in lines] == ["bob", "carol"]
        assert all(line.endswith("[online]") for line in lines)

    def test_business_error_propagates_as_relay_error(self, agents: dict[str, Agent]) -> None:
        with pytest.raises(RelayError) as err:
            agents["carol"].backend.send(request(to="bob"))

        assert err.value.code == "not_allowed"

    def test_instructions_are_the_registration_brief(self, agents: dict[str, Agent]) -> None:
        brief = agents["alice"].backend.instructions()

        assert brief.startswith('You are "alice" on mz-relay, role planner')
        assert "- bob : " in brief

    def test_unavailable_daemon_is_retried_once_under_the_same_id(
        self, daemon_paths: Paths, agents: dict[str, Agent]
    ) -> None:
        flaky = FailingLauncher(daemon_paths, failures=1)
        ids = IdSource("retry")
        backend = RelayToolBackend(agents["alice"].presence, flaky, new_id=ids)

        text = backend.send(request())

        assert flaky.connects == 2
        assert len(ids.issued) == 1
        assert text == f"queued {ids.issued[0]} for bob"
        inbox = agents["bob"].backend.inbox(20)
        assert inbox.count("[mz-relay] Peer message from alice") == 1

    def test_a_retry_that_fails_again_is_a_tool_error_without_a_third_try(
        self, daemon_paths: Paths, agents: dict[str, Agent]
    ) -> None:
        dead = FailingLauncher(daemon_paths, failures=2)
        ids = IdSource("dead")
        server = McpServer(
            RelayToolBackend(agents["alice"].presence, dead, new_id=ids), version=__version__
        )

        reply = run_in_thread(
            lambda: tool_call(server, "send", {"to": "bob", "body": "hi"}), PROC_WAIT_S
        )

        tool = reply["result"]
        assert tool["isError"] is True
        assert "daemon_unavailable" in tool["content"][0]["text"]
        assert dead.connects == 2
        assert len(ids.issued) <= 1, f"the retry drew a second id: {ids.issued}"


class TestUnavailableBackend:
    def test_every_tool_and_the_instructions_report_unavailable(self) -> None:
        backend = UnavailableBackend(RelayError("daemon_unavailable", "relayd did not start"))
        server = McpServer(backend, version=__version__)
        expected = "mz-relay is unavailable: daemon_unavailable: relayd did not start"
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
        calls: list[tuple[str, dict[str, object]]] = [
            ("send", {"to": "bob", "body": "hi"}),
            ("inbox", {}),
            ("ack", {"id": "m1"}),
            ("peers", {}),
        ]

        instructions = json.loads(server.handle_line(json.dumps(init)) or "")["result"]
        results = [tool_call(server, name, arguments)["result"] for name, arguments in calls]

        assert expected in instructions["instructions"]
        for result in results:
            assert result["isError"] is True
            assert expected in result["content"][0]["text"]


class TestRunMcp:
    @pytest.mark.parametrize("raw_name", ["", "${user_config.relay_name}"])
    def test_unset_looking_name_falls_back_to_cwd_resolution(
        self, daemon_paths: Paths, relay_env: dict[str, str], relay_home: Path, raw_name: str
    ) -> None:
        workdir = relay_home / "alice" / "work"
        workdir.mkdir(parents=True)
        mcp = McpProc(raw_name, relay_env, cwd=workdir, log=relay_home / "mcp.log")
        try:
            mcp.initialize()
            peers = mcp.tool("peers")
            exit_code = mcp.finish()
        finally:
            mcp.close()

        assert exit_code == 0
        assert [line.split(" : ")[0] for line in peers.splitlines()] == ["bob", "carol"]

    def test_unknown_name_serves_the_unavailable_backend_and_exits_0_at_eof(
        self, daemon_paths: Paths, relay_env: dict[str, str], relay_home: Path
    ) -> None:
        expected = "mz-relay is unavailable: unknown_agent"
        mcp = McpProc("ghost", relay_env, cwd=relay_home, log=relay_home / "mcp.log")
        try:
            init = mcp.initialize()
            peers = mcp.tool_result("peers")
            exit_code = mcp.finish()
        finally:
            mcp.close()

        assert expected in init["instructions"]
        assert peers["isError"] is True
        assert expected in peers["content"][0]["text"]
        assert exit_code == 0

    def test_a_state_dir_that_cannot_be_created_serves_the_unavailable_backend(
        self, relay_env: dict[str, str], relay_home: Path
    ) -> None:
        # MZ_RELAY_HOME is a file, so the launcher cannot create its state dir inside it.
        blocker = relay_home / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        env = {**relay_env, "MZ_RELAY_HOME": str(blocker)}
        mcp = McpProc("alice", env, cwd=relay_home, log=relay_home / "mcp.log")
        try:
            init = mcp.initialize()
            peers = mcp.tool_result("peers")
            exit_code = mcp.finish()
        finally:
            mcp.close()

        assert "mz-relay is unavailable: path_invalid" in init["instructions"]
        assert peers["isError"] is True
        assert exit_code == 0
