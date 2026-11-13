"""`mz-relay mcp --harness claude`: native binding from the session env, tools-only without it."""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from fakes_claude import CWD, FakeClaudeSession, claude_roster, mz_text
from mz_relay.adapters import AdapterContext, build_adapters
from mz_relay.adapters.claude import ClaudeAdapter
from mz_relay.clock import SystemClock
from mz_relay.envelope import Status
from mz_relay.gate import Gate
from mz_relay.hooks import HookInput
from mz_relay.paths import Paths
from mz_relay.roster import Harness, RosterCache
from mz_relay.store import Registration, Store
from relay_rig import PROC_WAIT_S, McpProc, paths_in, relayd_status, stop_daemon, wait_until

# Delivery waits on relayd's worker and the adapter's transcript poll (confirm_s 5 s).
DELIVERY_WAIT_S = 15.0
QUEUED = re.compile(r"queued (\S+) for cc")


class ClaudeRig:
    """A fake Claude session owned by this test process, a roster naming it `cc`, MCP children."""

    def __init__(self, home: Path, env: Mapping[str, str], fake: FakeClaudeSession) -> None:
        self.home = home
        self.paths: Paths = paths_in(home)
        self.base_env = dict(env)
        self.fake = fake
        self._stack = contextlib.ExitStack()
        # Registered first, so it runs after every MCP child has been reaped.
        self._stack.callback(stop_daemon, self.paths.socket)
        self.paths.roster.write_text(claude_roster(fake.config_dir), encoding="utf-8")

    def session_env(self, *, socket: bool = True) -> dict[str, str]:
        """What Claude Code puts in its MCP child's env (and an outer CLAUDE_PID that must lose)."""
        env = {
            **self.base_env,
            "CLAUDE_CONFIG_DIR": str(self.fake.config_dir),
            "CLAUDE_CODE_SESSION_ID": self.fake.session_id,
            "CLAUDECODE": "1",
            "CLAUDE_PID": "1",
        }
        if socket:
            env["CLAUDE_CODE_MESSAGING_SOCKET"] = str(self.fake.socket)
        return env

    def mcp(self, name: str | None, env: Mapping[str, str], *, harness: str) -> McpProc:
        log = self.home / f"mcp-{harness}-{name or 'unnamed'}.log"
        proc = self._stack.enter_context(
            contextlib.closing(McpProc(name, env, cwd=self.home, log=log, harness=harness))
        )
        proc.initialize()
        return proc

    def claude_mcp(self, *, socket: bool = True, name: str | None = "cc") -> McpProc:
        return self.mcp(name, self.session_env(socket=socket), harness="claude")

    def sender(self, extra_env: Mapping[str, str] | None = None) -> McpProc:
        return self.mcp("sender", {**self.base_env, **(extra_env or {})}, harness="opencode")

    def delivery_registration(self, name: str) -> Registration | None:
        store = Store(self.paths.db)
        try:
            return store.latest_delivery_registration(name)
        finally:
            store.close()

    def message_status(self, msg_id: str) -> tuple[Status, str | None] | None:
        store = Store(self.paths.db)
        try:
            row = store.get("sender", msg_id)
        finally:
            store.close()
        return None if row is None else (row.status, row.via)

    def online(self, name: str) -> bool:
        agents = relayd_status(self.paths.socket).get("agents", [])
        return any(agent["name"] == name and agent["online"] for agent in agents)

    def close(self) -> None:
        self._stack.close()


@pytest.fixture
def claude(relay_home: Path, relay_env: dict[str, str]) -> Iterator[ClaudeRig]:
    # pid = this process: the MCP child's parent, as Claude Code is the parent of its MCP child.
    with FakeClaudeSession(relay_home / "c", pid=os.getpid()) as fake:
        built = ClaudeRig(relay_home, relay_env, fake)
        try:
            yield built
        finally:
            built.close()


class TestBuildAdapters:
    def test_relayd_gets_a_claude_adapter(self, relay_paths: Paths, relay_home: Path) -> None:
        ctx = AdapterContext(
            paths=relay_paths, clock=SystemClock(), pw_home=relay_home, base_env={}
        )
        assert isinstance(build_adapters(ctx)[Harness.CLAUDE], ClaudeAdapter)


class TestNativeRegistration:
    def test_a_session_with_a_messaging_socket_registers_a_native_binding(
        self, claude: ClaudeRig
    ) -> None:
        claude.claude_mcp()

        reg = claude.delivery_registration("cc")
        assert reg is not None
        assert reg.delivery_mode == "native"
        assert reg.binding is not None
        # The binding names Claude (our parent), never this MCP child or CLAUDE_PID.
        assert reg.binding["pid"] == os.getpid()
        assert reg.binding["messaging_socket"] == str(claude.fake.socket)
        assert reg.binding["session_id"] == claude.fake.session_id
        assert Path(str(reg.binding["config_dir"])).resolve() == claude.fake.config_dir.resolve()

    def test_the_config_dir_alone_resolves_the_agent_without_a_name(
        self, claude: ClaudeRig
    ) -> None:
        mcp = claude.claude_mcp(name=None)

        reg = claude.delivery_registration("cc")
        assert reg is not None
        assert reg.delivery_mode == "native"
        assert "sender : " in mcp.tool("peers")

    def test_a_message_to_cc_arrives_as_one_frame_on_the_session_socket(
        self, claude: ClaudeRig
    ) -> None:
        claude.claude_mcp()
        sender = claude.sender()

        match = QUEUED.match(sender.tool("send", to="cc", body="please review src/app.py"))
        assert match is not None
        msg_id = match.group(1)

        delivered = wait_until(
            lambda: claude.message_status(msg_id) == (Status.DELIVERED, "claude"), DELIVERY_WAIT_S
        )
        assert delivered, claude.message_status(msg_id)
        frames = claude.fake.frames
        assert len(frames) == 1
        # The inbox frame carries no auth line: only type and message.
        assert set(frames[0]) == {"type", "message"}
        message = frames[0]["message"]
        assert isinstance(message, dict)
        content = str(message["content"])
        assert content.startswith("[mz-relay] Peer message from sender (role: builder;")
        assert f"id: {msg_id}" in content
        assert "  please review src/app.py" in content


class TestWithoutMessagingSocket:
    def test_registers_tools_only_logs_a_warning_and_the_tools_work(
        self, claude: ClaudeRig
    ) -> None:
        mcp = claude.claude_mcp(socket=False, name=None)

        peers = mcp.tool("peers")

        assert "sender : " in peers
        assert claude.online("cc")
        assert claude.delivery_registration("cc") is None
        warnings = [
            line
            for line in mcp.stderr_text().splitlines()
            if "WARNING mz_relay.tool_backend:" in line
        ]
        assert warnings, mcp.stderr_text()

    def test_an_opencode_mcp_ignores_a_leaked_claude_socket(self, claude: ClaudeRig) -> None:
        # An OpenCode started from a terminal inside Claude inherits Claude's socket variables.
        leaked = {
            "CLAUDE_CODE_MESSAGING_SOCKET": str(claude.fake.socket),
            "CLAUDE_CONFIG_DIR": str(claude.fake.config_dir),
        }
        claude.sender(leaked)

        assert wait_until(lambda: claude.online("sender"), PROC_WAIT_S)
        assert claude.delivery_registration("sender") is None


class TestRelativeConfigDir:
    def test_the_server_stays_up_and_its_tools_say_why_they_are_unavailable(
        self, claude: ClaudeRig
    ) -> None:
        env = {**claude.session_env(), "CLAUDE_CONFIG_DIR": "relative/cfg"}
        mcp = claude.mcp("cc", env, harness="claude")

        result = mcp.tool_result("peers")

        assert result.get("isError") is True
        text = str(result["content"][0]["text"])
        assert text.startswith("mz-relay is unavailable: path_invalid:"), text
        assert mcp.proc.poll() is None


class TestNamePrecedence:
    def test_the_server_and_the_gate_both_take_the_plugin_option_over_the_shell_name(
        self, claude: ClaudeRig
    ) -> None:
        # cc is read_only and cf full, on one config dir, so only a name tells them apart.
        roster = claude_roster(claude.fake.config_dir, tools="read_only", twin=True)
        claude.paths.roster.write_text(roster, encoding="utf-8")
        env = {**claude.session_env(), "CLAUDE_PLUGIN_OPTION_RELAY_NAME": "cc"}

        claude.mcp("cf", env, harness="claude")
        claude.fake.writer.human("tidy the docs")
        claude.fake.writer.peer(mz_text())
        gate = Gate(
            claude.paths,
            RosterCache(claude.paths.roster, claude.home),
            env={**env, "MZ_RELAY_NAME": "cf"},
        )
        hook = HookInput(
            session_id=claude.fake.session_id,
            cwd=Path(CWD),
            transcript_path=claude.fake.transcript_path,
            tool_name="Write",
            event="PreToolUse",
            source=None,
        )

        registered_cc = claude.delivery_registration("cc") is not None
        registered_cf = claude.delivery_registration("cf") is not None
        assert (registered_cc, registered_cf) == (True, False)
        # A denial means the gate resolved cc, the read_only agent.
        assert gate.decide(hook) is not None
