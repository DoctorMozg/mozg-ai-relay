"""Live smoke: a real OpenCode loads the installed plugin, gets peer messages in its main session,
and runs read_only peer turns under the mz-relay-peer agent.
Run by hand: MZ_RELAY_LIVE_OPENCODE=opencode OPENCODE_CONFIG=<config> uv run make live-opencode
The TUI ignores `-m` (1.19.5), so the config file sets "model".
OpenCode runs with the real HOME, so its sessions land in the user's own data dir. A non-git dir
shares OpenCode's global project, so sessions are told apart by directory and deleted afterwards."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from live_rig import (
    BOARD_ASK,
    BOARD_EXTENSION,
    POLL_S,
    SETTLE_S,
    START_S,
    TURN_S,
    Architect,
    RelayRows,
    TmuxPane,
)
from relay_rig import (
    PLUGIN,
    Message,
    online_names,
    paths_in,
    relayd_status,
    running_daemon,
    wait_until,
)

CLI_TIMEOUT_S = 60.0
# The TUI counts as ready once its screen stops changing for this long.
TUI_QUIET_S = 3.0
PEER_AGENT = "mz-relay-peer"

ROSTER = (
    """\
version = 1

[policy]
hold_corporate_to_personal = false

[agents.architect]
harness = "pi"
label = "personal"
role = "architect"
description = "Plans the work; a test client with tools only."

[agents.helper]
harness = "opencode"
label = "personal"
role = "helper"
description = "Answers questions about the project."
tools = "read_only"
"""
    + BOARD_EXTENSION
)


class OpencodeCli:
    """`opencode session list` (top-level sessions only) and `opencode export`, run in project."""

    def __init__(self, argv0: str, project: Path, env: Mapping[str, str]) -> None:
        self._argv0 = argv0
        self._project = project
        self._env = dict(env)

    def _json(self, *args: str) -> object:
        done = subprocess.run(
            [self._argv0, *args],
            cwd=self._project,
            env=self._env,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
            check=True,
        )
        # `session list` prints nothing at all, not [], when there are no sessions.
        return json.loads(done.stdout) if done.stdout.strip() else []

    def sessions(self) -> list[str]:
        """Top-level sessions started in this project dir."""
        listed = self._json("session", "list", "--format", "json")
        assert isinstance(listed, list), f"session list is not a JSON list: {listed!r}"
        here = str(self._project)
        return [str(s["id"]) for s in listed if isinstance(s, dict) and s.get("directory") == here]

    def main_session(self) -> str:
        top = self.sessions()
        assert len(top) == 1, f"expected one top-level session in {self._project}, got {top}"
        return top[0]

    def messages(self, session_id: str) -> list[Message]:
        exported = self._json("export", session_id)
        assert isinstance(exported, dict), f"export is not a JSON object: {exported!r}"
        return [m for m in exported.get("messages", []) if isinstance(m, dict)]

    def child_sessions(self, session_id: str) -> list[str]:
        """Sessions the task tool started from session_id, read from its tool parts' metadata."""
        children = []
        for message in self.messages(session_id):
            for part in message.get("parts", []):
                if part.get("type") == "tool" and part.get("tool") == "task":
                    child = part.get("state", {}).get("metadata", {}).get("sessionId")
                    if child:
                        children.append(str(child))
        return children

    def delete_sessions(self) -> None:
        for session_id in self.sessions():
            subprocess.run(
                [self._argv0, "session", "delete", session_id],
                cwd=self._project,
                env=self._env,
                capture_output=True,
                timeout=CLI_TIMEOUT_S,
                check=False,
            )

    def agent_of_message_with(self, session_id: str, needle: str) -> str | None:
        """The agent of the first message in the session whose text holds needle; None if absent."""
        for message in self.messages(session_id):
            texts = [str(part.get("text", "")) for part in message.get("parts", [])]
            if any(needle in text for text in texts):
                info = message.get("info", {})
                return str(info.get("agent") or info.get("mode") or "")
        return None


@pytest.mark.live_opencode
def test_opencode_gets_peer_messages_in_its_main_session_under_the_peer_agent(
    live_opencode_argv: list[str], tmux_bin: str, relay_home: Path, relay_env: dict[str, str]
) -> None:
    """OpenCode applies the plugin's `config` hook (mcp entry, peer agent) and passes
    MZ_RELAY_HOME on to the local MCP server it starts."""
    from mz_relay.adapters import AdapterContext, build_adapters
    from mz_relay.clock import SystemClock
    from mz_relay.install import Installer

    nonce = secrets.token_hex(4)
    paths = paths_in(relay_home)
    project = relay_home / "project"
    project.mkdir()
    installer = Installer(paths, plugin_root=PLUGIN, env={})
    installer.opencode(project / ".opencode" / "plugins" / "mz-relay.ts")
    env = {**relay_env, "MZ_RELAY_NAME": "helper"}
    # relay_env drops every OPENCODE* variable; the config file is the user's, not a session marker.
    if "OPENCODE_CONFIG" in os.environ:
        env["OPENCODE_CONFIG"] = os.environ["OPENCODE_CONFIG"]
    cli = OpencodeCli(live_opencode_argv[0], project, env)
    ctx = AdapterContext(paths=paths, clock=SystemClock(), pw_home=paths.pw_home, base_env={})
    # The stack unwinds before the daemon stops: install.json names a bin the plugin could use
    # to start a detached relayd in a home the fixture is about to delete.
    with (
        running_daemon(paths, build_adapters(ctx), roster_text=ROSTER),
        contextlib.ExitStack() as stack,
    ):
        # Registered first, so it runs last: after the pane has closed OpenCode.
        stack.callback(cli.delete_sessions)
        pane = stack.enter_context(TmuxPane(tmux_bin, live_opencode_argv, cwd=project, env=env))
        rows = stack.enter_context(RelayRows(paths.db))
        assert pane.wait_quiet(TUI_QUIET_S, START_S), f"the TUI never settled:\n{pane.screen()}"

        # (a) the injected MCP server lists mz-relay_send, and the first chat registers helper
        pane.send_line(
            f"Use the mz-relay_send tool to send architect the text HELLO-{nonce} as inform."
        )
        sent = wait_until(lambda: rows.sent_with("helper", f"HELLO-{nonce}"), START_S, POLL_S)
        assert sent, pane.screen()
        online = wait_until(
            lambda: "helper" in online_names(relayd_status(paths.socket)), START_S, POLL_S
        )
        assert online, f"helper never came online: {relayd_status(paths.socket)}"
        main = cli.main_session()
        architect = stack.enter_context(
            Architect(paths.socket, project, harness="pi", target="helper")
        )

        # (b) a read_only peer message runs under mz-relay-peer and cannot write
        gate = project / f"gate-{nonce}.txt"
        ask = architect.send(f"PING-{nonce}: create the file {gate.name} in this project.")
        assert wait_until(lambda: rows.arrived_via("architect", ask, "opencode"), TURN_S, POLL_S)
        assert not wait_until(gate.exists, SETTLE_S, POLL_S), pane.screen()
        assert cli.agent_of_message_with(main, f"PING-{nonce}") == PEER_AGENT

        # (c) after a task subagent ran, the next peer message still lands in the main session
        pane.send_line("Use the task tool to have a general subagent list the files here.")
        assert wait_until(lambda: bool(cli.child_sessions(main)), TURN_S, POLL_S), pane.screen()
        after = architect.send(f"AFTER-{nonce}: reply with the word OK.", kind="inform")
        assert wait_until(lambda: rows.arrived_via("architect", after, "opencode"), TURN_S, POLL_S)
        assert cli.agent_of_message_with(main, f"AFTER-{nonce}") is not None
        for child in cli.child_sessions(main):
            assert cli.agent_of_message_with(child, f"AFTER-{nonce}") is None

        # (d) a read_only peer turn may use the read-only board tools relayd gave it
        marker = architect.seed_board()
        architect.send(BOARD_ASK)
        assert wait_until(lambda: rows.sent_with("helper", marker), TURN_S, POLL_S), pane.screen()
