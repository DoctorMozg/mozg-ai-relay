"""The read-only PreToolUse gate: a read_only Claude agent keeps only read tools in a peer turn."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from mz_relay.adapters.claude_registry import claude_config_dir
from mz_relay.adapters.claude_transcript import Transcript, TranscriptWatcher
from mz_relay.errors import IdentityError, PathError
from mz_relay.hooks import HookInput, read_offset
from mz_relay.identity import IdentityQuery, resolve_identity
from mz_relay.paths import Paths
from mz_relay.presence import configured_name
from mz_relay.roster import Harness, RosterCache, ToolMode

log = logging.getLogger(__name__)

PRE_TOOL_USE = "PreToolUse"
# ToolSearch stays so a read_only agent can load the deferred mz-relay tools to reply.
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "WebSearch", "TodoWrite", "ToolSearch"})
OWN_TOOL_PREFIX = "mcp__plugin_mz-relay_mz-relay__"
GATE_REASON = (
    "mz-relay: {tool} is blocked because a peer message is part of this turn and your roster "
    "role is read_only. The limit lifts when the turn ends or your user types."
)


def always_allowed(tool: str) -> bool:
    """Read tools and this plugin's own MCP tools; an allowlist, so unknown tools are denied."""
    return tool in READ_ONLY_TOOLS or tool.startswith(OWN_TOOL_PREFIX)


class Gate:
    """Denies non-read tools to a read_only agent while a peer message leads the current turn."""

    def __init__(self, paths: Paths, roster: RosterCache, *, env: Mapping[str, str]) -> None:
        self._paths = paths
        self._roster = roster
        self._env = dict(env)

    def decide(self, hook: HookInput) -> dict[str, object] | None:
        """The PreToolUse deny object, or None to allow without output."""
        if hook.event != PRE_TOOL_USE:
            raise ValueError(f"the gate runs on {PRE_TOOL_USE}, got {hook.event!r}")
        tool = hook.tool_name
        if tool is None:
            raise ValueError(f"{PRE_TOOL_USE} hook input for {hook.session_id} has no tool_name")
        if always_allowed(tool):
            return None
        try:
            config_dir = claude_config_dir(self._env)
            query = IdentityQuery(
                harness=Harness.CLAUDE,
                cwd=hook.cwd,
                name=configured_name(self._env),
                config_dir=config_dir,
            )
            agent = resolve_identity(self._roster.current(), query)
        except (PathError, IdentityError) as exc:
            log.debug("gate allows %s: no agent for this session (%s)", tool, exc.code)
            return None
        if agent.tools is not ToolMode.READ_ONLY:
            return None
        if not self._peer_turn(config_dir, hook):
            return None
        log.warning(
            "denied %s to %s in a peer turn of session %s", tool, agent.name, hook.session_id
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": PRE_TOOL_USE,
                "permissionDecision": "deny",
                "permissionDecisionReason": GATE_REASON.format(tool=tool),
            }
        }

    def _peer_turn(self, config_dir: Path, hook: HookInput) -> bool:
        sid = hook.session_id
        # The file the Stop hook measured; the config-dir lookup is for input without the path.
        if hook.transcript_path is not None:
            transcript: Transcript | None = Transcript(hook.transcript_path)
        else:
            transcript = TranscriptWatcher(config_dir, sid).transcript()
        if transcript is None:
            return False
        turn_end = read_offset(self._paths.turn_end_dir / sid) or 0
        if transcript.peer_turn_since(turn_end):
            return True
        marker = read_offset(self._paths.marker_dir / sid)
        if marker is None:
            return False
        # A rewake leaves no recognizable prompt line; only a human prompt after it ends the turn.
        return not transcript.human_since(marker)
