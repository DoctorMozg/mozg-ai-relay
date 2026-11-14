"""Live smoke: a real OpenCode loads the installed plugin, gets peer messages in its main session,
and runs read_only peer turns under the mz-relay-peer agent.
Run by hand: MZ_RELAY_LIVE_OPENCODE="opencode -m <provider/model>" uv run make live-opencode
OpenCode runs with the real HOME from the test environment, so its sessions land in the user's
own data dir; `session list` is per project, so the test's project dir keeps them apart."""

from __future__ import annotations

import contextlib
import json
import secrets
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from live_rig import POLL_S, SETTLE_S, START_S, TURN_S, Architect, RelayRows, TmuxPane
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

ROSTER = """\
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


class OpencodeCli:
    """`opencode session list` and `opencode export` run in the project.
    UNTESTED: the JSON shapes (session `id`/`parentID`; messages with `info` and `parts`)."""

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
        return json.loads(done.stdout)

    def sessions(self) -> list[Message]:
        listed = self._json("session", "list", "--format", "json")
        assert isinstance(listed, list), f"session list is not a JSON list: {listed!r}"
        return [s for s in listed if isinstance(s, dict)]

    def main_session(self) -> str:
        top = [s for s in self.sessions() if not s.get("parentID")]
        assert len(top) == 1, f"expected one top-level session, got {top}"
        return str(top[0]["id"])

    def child_sessions(self) -> list[str]:
        return [str(s["id"]) for s in self.sessions() if s.get("parentID")]

    def agent_of_message_with(self, session_id: str, needle: str) -> str | None:
        """The agent of the first message in the session whose text holds needle; None if absent."""
        exported = self._json("export", session_id)
        assert isinstance(exported, dict), f"export is not a JSON object: {exported!r}"
        for message in exported.get("messages", []):
            texts = [str(part.get("text", "")) for part in message.get("parts", [])]
            if any(needle in text for text in texts):
                info = message.get("info", {})
                return str(info.get("agent") or info.get("mode") or "")
        return None


@pytest.mark.live_opencode
def test_opencode_gets_peer_messages_in_its_main_session_under_the_peer_agent(
    live_opencode_argv: list[str], tmux_bin: str, relay_home: Path, relay_env: dict[str, str]
) -> None:
    """UNTESTED: whether OpenCode applies the plugin's `config` hook (mcp entry, peer agent) and
    passes MZ_RELAY_HOME on to the local MCP server it starts."""
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
    cli = OpencodeCli(live_opencode_argv[0], project, env)
    ctx = AdapterContext(paths=paths, clock=SystemClock(), pw_home=paths.pw_home, base_env={})
    # The stack unwinds before the daemon stops: install.json names a bin the plugin could use
    # to start a detached relayd in a home the fixture is about to delete.
    with (
        running_daemon(paths, build_adapters(ctx), roster_text=ROSTER),
        contextlib.ExitStack() as stack,
    ):
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
        assert wait_until(lambda: bool(cli.child_sessions()), TURN_S, POLL_S), pane.screen()
        after = architect.send(f"AFTER-{nonce}: reply with the word OK.", kind="inform")
        assert wait_until(lambda: rows.arrived_via("architect", after, "opencode"), TURN_S, POLL_S)
        assert cli.agent_of_message_with(main, f"AFTER-{nonce}") is not None
        for child in cli.child_sessions():
            assert cli.agent_of_message_with(child, f"AFTER-{nonce}") is None
