"""Claude wiring files: .mcp.json, hooks/hooks.json and plugin.json userConfig."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fakes_claude import CWD, SESSION_ID, TranscriptWriter, claude_roster, mz_text, transcript_in
from relay_rig import PLUGIN, PROC_WAIT_S, Message, paths_in

ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
LAUNCHER = f'"{ROOT_VAR}/bin/mz-relay"'


def load(relative: str) -> Message:
    loaded: Message = json.loads((PLUGIN / relative).read_text(encoding="utf-8"))
    return loaded


# A day: an idle session keeps its waiter; a probe saw 86400 honoured past 600 s.
WAITER_TIMEOUT_S = 86_400


@pytest.fixture
def hooks() -> Message:
    hooks: Message = load("hooks/hooks.json")["hooks"]
    return hooks


def only_command(groups: list[Message]) -> Message:
    """The single command hook of an event that must have exactly one."""
    assert len(groups) == 1, groups
    entries = groups[0]["hooks"]
    assert len(entries) == 1, entries
    entry: Message = entries[0]
    assert entry["type"] == "command"
    return entry


class TestMcpJson:
    def test_the_server_runs_the_launcher_as_a_claude_mcp_with_the_configured_name(self) -> None:
        assert load(".mcp.json") == {
            "mcpServers": {
                "mz-relay": {
                    "command": f"{ROOT_VAR}/bin/mz-relay",
                    "args": ["mcp", "--harness", "claude"],
                    # The option's own name: a shell MZ_RELAY_NAME is left as it is.
                    "env": {"CLAUDE_PLUGIN_OPTION_RELAY_NAME": "${user_config.relay_name}"},
                }
            }
        }

    def test_the_name_placeholder_refers_to_a_declared_user_config_key(self) -> None:
        raw = load(".mcp.json")["mcpServers"]["mz-relay"]["env"]["CLAUDE_PLUGIN_OPTION_RELAY_NAME"]
        match = re.fullmatch(r"\$\{user_config\.([a-z_]+)\}", raw)
        assert match is not None, raw
        assert match.group(1) in load(".claude-plugin/plugin.json")["userConfig"]


class TestHooksJson:
    def test_session_start_runs_one_rewake_waiter_on_every_session_source(
        self, hooks: Message
    ) -> None:
        groups = hooks["SessionStart"]
        entry = only_command(groups)
        assert groups[0]["matcher"] == "startup|resume|clear|compact"
        assert entry["command"] == f"{LAUNCHER} hook wait SessionStart"
        assert entry["asyncRewake"] is True

    def test_stop_runs_one_rewake_waiter(self, hooks: Message) -> None:
        entry = only_command(hooks["Stop"])
        assert entry["command"] == f"{LAUNCHER} hook wait Stop"
        assert entry["asyncRewake"] is True

    @pytest.mark.parametrize("event", ["SessionStart", "Stop"])
    def test_a_waiter_outlives_the_600_s_default_timeout(self, hooks: Message, event: str) -> None:
        # Claude Code 2.1.325 sends SIGTERM to an asyncRewake hook at its timeout, 600 s if unset.
        assert only_command(hooks[event])["timeout"] == WAITER_TIMEOUT_S

    def test_pre_tool_use_runs_the_gate_synchronously_on_every_tool(self, hooks: Message) -> None:
        groups = hooks["PreToolUse"]
        entry = only_command(groups)
        assert groups[0]["matcher"] == "*"
        assert entry["command"] == f"{LAUNCHER} hook gate"
        assert entry["timeout"] == 10
        # A background gate could not deny anything.
        assert "async" not in entry
        assert "asyncRewake" not in entry

    def test_only_the_three_wired_events_have_hooks(self, hooks: Message) -> None:
        assert set(hooks) == {"SessionStart", "Stop", "PreToolUse"}


class TestUserConfig:
    def test_relay_name_is_an_optional_string_defaulting_to_empty(self) -> None:
        relay_name = load(".claude-plugin/plugin.json")["userConfig"]["relay_name"]
        assert relay_name == {
            "type": "string",
            "title": "Relay name",
            "description": (
                "Roster name for sessions of this Claude account. "
                "Leave empty to match by config dir and folder."
            ),
            "required": False,
            "default": "",
        }


class TestGateCommandAsClaudeRunsIt:
    def test_the_pre_tool_use_command_denies_write_in_a_read_only_peer_turn(
        self, relay_home: Path, relay_env: dict[str, str], hooks: Message
    ) -> None:
        cfg = relay_home / "cfg"
        paths_in(relay_home).roster.write_text(
            claude_roster(cfg, tools="read_only"), encoding="utf-8"
        )
        writer = TranscriptWriter(transcript_in(cfg), session_id=SESSION_ID)
        writer.human("tidy the docs")
        writer.peer(mz_text())
        payload = {
            "session_id": SESSION_ID,
            "transcript_path": str(writer.path),
            "cwd": CWD,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": f"{CWD}/a.txt", "content": "x"},
        }
        command = only_command(hooks["PreToolUse"])["command"]
        env = {**relay_env, "CLAUDE_PLUGIN_ROOT": str(PLUGIN), "CLAUDE_CONFIG_DIR": str(cfg)}

        done = subprocess.run(
            ["/bin/bash", "-c", command],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=PROC_WAIT_S,
            check=False,
        )

        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
