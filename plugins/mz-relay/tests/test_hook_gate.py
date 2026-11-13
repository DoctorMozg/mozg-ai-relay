"""The read-only PreToolUse gate: which tools a read_only Claude agent keeps during a peer turn."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from fakes_claude import (
    CWD,
    SESSION_ID,
    TranscriptWriter,
    claude_roster,
    mz_text,
    transcript_in,
)
from mz_relay.adapters.claude_transcript import DEFAULT_TAIL_BYTES
from mz_relay.clock import SystemClock
from mz_relay.gate import Gate
from mz_relay.hooks import HookInput, Waiter, run_hook
from mz_relay.paths import Paths
from mz_relay.roster import RosterCache
from relay_rig import BIN, PROC_WAIT_S, Message, NoSpawn, launcher_for, waiter_lock_held

REASON = (
    "mz-relay: {tool} is blocked because a peer message is part of this turn and your roster "
    "role is read_only. The limit lifts when the turn ends or your user types."
)
OWN_TOOLS = [f"mcp__plugin_mz-relay_mz-relay__{tool}" for tool in ("send", "inbox", "ack", "peers")]
READ_ONLY = ["Read", "Glob", "Grep", "LS", "WebSearch", "TodoWrite", "ToolSearch", *OWN_TOOLS]
BLOCKED = [
    "Write",
    "Edit",
    "Bash",
    "NotebookEdit",
    "WebFetch",
    "Task",
    "Skill",
    "mcp__other__x",
    # Another server that merely starts with our name is not ours.
    "mcp__plugin_mz-relay_mz-relay-evil__send",
    "read",
]

Step = Callable[[TranscriptWriter], object]
# One tool result's text; enough of them push a peer line out of the gate's tail window.
CHUNK = "x" * 65_536


def denial(tool: str) -> Message:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON.format(tool=tool),
        }
    }


def pre_tool_use(tool: str, transcript: Path) -> str:
    """A PreToolUse hook payload in the shape Claude Code writes to stdin."""
    payload = {
        "session_id": SESSION_ID,
        "transcript_path": str(transcript),
        "cwd": CWD,
        "permission_mode": "acceptEdits",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": f"{CWD}/gate.txt", "content": "x"},
        "tool_use_id": "toolu_0001",
    }
    return json.dumps(payload)


class GateRig:
    """A roster, a Claude config dir with the session's transcript, and the gate's run files."""

    def __init__(self, paths: Paths, home: Path) -> None:
        self.paths = paths
        self.home = home
        self.cfg = home / "cfg"
        self.writer = TranscriptWriter(transcript_in(self.cfg), session_id=SESSION_ID)
        # What Claude passes as transcript_path; None leaves the gate to find the transcript.
        self.hook_transcript: Path | None = self.writer.path
        self.env: dict[str, str] = {"CLAUDE_CONFIG_DIR": str(self.cfg), "HOME": str(home)}
        self.write_roster(claude_roster(self.cfg, tools="read_only"))

    def write_roster(self, text: str) -> None:
        self.paths.roster.write_text(text, encoding="utf-8")

    def write(self, steps: list[Step]) -> None:
        for step in steps:
            step(self.writer)

    def decide(self, tool: str, env: dict[str, str] | None = None) -> dict[str, object] | None:
        gate = Gate(self.paths, RosterCache(self.paths.roster, self.home), env=env or self.env)
        hook = HookInput(
            session_id=SESSION_ID,
            cwd=Path(CWD),
            transcript_path=self.hook_transcript,
            tool_name=tool,
            event="PreToolUse",
            source=None,
        )
        decision: dict[str, object] | None = gate.decide(hook)
        return decision

    def stop(self) -> None:
        """Runs the real Stop hook while a SessionStart waiter holds the session lock."""
        hook = HookInput(
            session_id=SESSION_ID,
            cwd=Path(CWD),
            transcript_path=self.writer.path,
            tool_name=None,
            event="Stop",
            source=None,
        )
        launcher = launcher_for(self.paths, NoSpawn())
        waiter = Waiter(self.paths, launcher, env=self.env, clock=SystemClock())
        with waiter_lock_held(self.paths, SESSION_ID):
            assert waiter.run("Stop", hook, io.StringIO()) == 0

    def transcript_size(self) -> int:
        return self.writer.path.stat().st_size if self.writer.path.exists() else 0

    def mark_turn_end(self) -> None:
        """What the Stop hook records: the transcript size at the end of a turn."""
        self._offset_file(self.paths.turn_end_dir).write_text(
            str(self.transcript_size()), encoding="ascii"
        )

    def mark_rewake(self) -> None:
        """What the rewake waiter records when it wakes the session with a peer message."""
        self._offset_file(self.paths.marker_dir).write_text(
            str(self.transcript_size()), encoding="ascii"
        )

    def _offset_file(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        return directory / SESSION_ID


@pytest.fixture
def rig(relay_paths: Paths, relay_home: Path) -> GateRig:
    return GateRig(relay_paths, relay_home)


def human(w: TranscriptWriter) -> object:
    return w.human("please tidy the docs")


def human_legacy(w: TranscriptWriter) -> object:
    return w.human_legacy("please tidy the docs")


def human_image(w: TranscriptWriter) -> object:
    return w.human_image("what is in this screenshot?")


def peer(w: TranscriptWriter) -> object:
    return w.peer(mz_text())


def peer_joined(w: TranscriptWriter) -> object:
    return w.peer(mz_text(), joined=True)


def foreign_peer(w: TranscriptWriter) -> object:
    return w.peer("hi from a session that does not use mz-relay")


def handback(w: TranscriptWriter) -> object:
    return w.handback("all tests pass")


def handback_joined(w: TranscriptWriter) -> object:
    return w.handback("all tests pass", joined=True)


def tool_result(w: TranscriptWriter) -> object:
    return w.tool_result()


def assistant(w: TranscriptWriter) -> object:
    return w.assistant("on it")


def meta(w: TranscriptWriter) -> object:
    return w.meta("Stop hook feedback")


def compact(w: TranscriptWriter) -> object:
    return w.compact_summary()


def compact_human(w: TranscriptWriter) -> object:
    return w.compact_summary(turn_origin="human")


def task_notification(w: TranscriptWriter) -> object:
    return w.task_notification()


def sdk_prompt(w: TranscriptWriter) -> object:
    return w.sdk_prompt("continue")


def long_output(w: TranscriptWriter) -> object:
    """More tool output than the gate's tail window holds, all of it allowed read tools."""
    return [w.tool_result(CHUNK) for _ in range(DEFAULT_TAIL_BYTES // len(CHUNK) + 2)]


class TestToolAllowlist:
    @pytest.mark.parametrize("tool", READ_ONLY)
    def test_read_only_and_own_relay_tools_stay_allowed_in_a_peer_turn(
        self, rig: GateRig, tool: str
    ) -> None:
        rig.write([human, peer])
        assert rig.decide(tool) is None

    @pytest.mark.parametrize("tool", BLOCKED)
    def test_every_other_tool_is_denied_in_a_peer_turn(self, rig: GateRig, tool: str) -> None:
        rig.write([human, peer])
        assert rig.decide(tool) == denial(tool)


class TestPeerTurnWindow:
    @pytest.mark.parametrize(
        "steps",
        [
            pytest.param([human, peer], id="peer-after-human"),
            pytest.param([peer, tool_result, assistant, tool_result], id="peer-then-tool-calls"),
            pytest.param([peer_joined], id="joined-peer"),
            pytest.param([human, peer, compact], id="compaction-keeps-the-turn"),
            pytest.param([human, peer, compact_human], id="compaction-with-human-origin"),
            pytest.param([human, peer, task_notification], id="task-notification-keeps-the-turn"),
            pytest.param([human, peer, meta], id="meta-keeps-the-turn"),
            pytest.param([peer, handback_joined], id="joined-handback-keeps-the-turn"),
            pytest.param([human, tool_result, peer], id="peer-joined-a-user-turn"),
            # SDK prompts are skipped by the classifier, so only Stop ends an SDK-driven peer turn.
            pytest.param([peer, sdk_prompt], id="sdk-prompt-keeps-the-turn"),
        ],
    )
    def test_write_is_denied_while_a_peer_message_leads_the_turn(
        self, rig: GateRig, steps: list[Step]
    ) -> None:
        rig.write(steps)
        assert rig.decide("Write") == denial("Write")

    @pytest.mark.parametrize(
        "steps",
        [
            pytest.param([], id="empty-transcript"),
            pytest.param([human, assistant, tool_result], id="user-turn"),
            pytest.param([peer, human], id="user-typed-after-the-peer"),
            pytest.param([peer, human_image], id="user-image-prompt"),
            pytest.param([peer, human_legacy], id="legacy-user-prompt"),
            pytest.param([human, handback], id="handback-in-user-turn"),
            pytest.param([peer, handback], id="handback-started-a-turn"),
            pytest.param([foreign_peer], id="non-mz-peer-message"),
            pytest.param([sdk_prompt, tool_result], id="sdk-turn"),
        ],
    )
    def test_write_is_allowed_outside_a_peer_turn(self, rig: GateRig, steps: list[Step]) -> None:
        rig.write(steps)
        assert rig.decide("Write") is None


class TestTurnEndAndRewakeMarker:
    def test_a_turn_end_after_the_peer_line_lifts_the_gate(self, rig: GateRig) -> None:
        rig.write([human, peer, assistant])
        rig.mark_turn_end()
        rig.write([tool_result])
        assert rig.decide("Write") is None

    def test_a_peer_line_after_the_turn_end_is_gated(self, rig: GateRig) -> None:
        rig.write([human, assistant])
        rig.mark_turn_end()
        rig.write([peer])
        assert rig.decide("Write") == denial("Write")

    def test_a_rewake_marker_without_a_later_human_line_is_a_peer_turn(self, rig: GateRig) -> None:
        # The rewake path leaves no peer line; only the marker says a peer woke the session.
        rig.write([human, assistant])
        rig.mark_turn_end()
        rig.mark_rewake()
        rig.write([meta, assistant, tool_result])
        assert rig.decide("Bash") == denial("Bash")

    def test_a_human_line_after_the_rewake_marker_lifts_the_gate(self, rig: GateRig) -> None:
        rig.write([human, assistant])
        rig.mark_rewake()
        rig.write([meta, assistant, human])
        assert rig.decide("Bash") is None


class TestLongTurns:
    """Tool output past the 256 KiB tail must not hide the prompt line that started the turn."""

    @pytest.mark.parametrize("tool", ["Bash", "Write", "Edit"])
    def test_a_peer_turn_stays_gated_after_long_tool_output(self, rig: GateRig, tool: str) -> None:
        rig.write([human, peer, assistant, long_output])
        assert rig.decide(tool) == denial(tool)

    def test_a_peer_turn_after_a_turn_end_stays_gated_after_long_tool_output(
        self, rig: GateRig
    ) -> None:
        rig.write([human, assistant])
        rig.mark_turn_end()
        rig.write([peer, long_output])
        assert rig.decide("Bash") == denial("Bash")

    def test_a_human_line_after_a_long_peer_turn_lifts_the_gate(self, rig: GateRig) -> None:
        rig.write([peer, long_output, human])
        assert rig.decide("Bash") is None

    def test_a_human_line_pushed_out_of_the_tail_still_lifts_the_gate(self, rig: GateRig) -> None:
        rig.write([peer, long_output, human, long_output])
        assert rig.decide("Bash") is None

    def test_a_human_line_after_the_rewake_marker_lifts_the_gate_after_long_output(
        self, rig: GateRig
    ) -> None:
        rig.write([human, assistant])
        rig.mark_turn_end()
        rig.mark_rewake()
        rig.write([meta, assistant, human, long_output])
        assert rig.decide("Bash") is None


class TestStopHookBookkeeping:
    """The Stop hook runs async and late; what it records must come from the transcript."""

    def test_a_peer_line_written_before_a_late_stop_hook_stays_gated(self, rig: GateRig) -> None:
        rig.write([human, assistant, peer])
        rig.stop()
        assert rig.decide("Bash") == denial("Bash")

    def test_a_rewake_marker_written_before_a_late_stop_hook_stays_gated(
        self, rig: GateRig
    ) -> None:
        rig.write([human, assistant])
        rig.mark_rewake()
        rig.write([meta])
        rig.stop()
        assert rig.decide("Bash") == denial("Bash")

    def test_a_stop_after_the_peer_turns_last_reply_lifts_the_gate(self, rig: GateRig) -> None:
        rig.write([human, peer, tool_result, assistant])
        rig.stop()
        assert rig.decide("Bash") is None

    def test_a_stop_after_the_rewake_turns_reply_lifts_the_gate(self, rig: GateRig) -> None:
        rig.write([human, assistant])
        rig.mark_rewake()
        rig.write([meta, assistant])
        rig.stop()
        assert rig.decide("Bash") is None


class TestTranscriptPath:
    def test_the_gate_reads_the_transcript_path_claude_passes(
        self, rig: GateRig, relay_home: Path
    ) -> None:
        # Not under <config dir>/projects, so only transcript_path leads to it.
        elsewhere = TranscriptWriter(
            relay_home / "elsewhere" / f"{SESSION_ID}.jsonl", session_id=SESSION_ID
        )
        elsewhere.human("please tidy the docs")
        elsewhere.peer(mz_text())
        rig.hook_transcript = elsewhere.path
        assert rig.decide("Write") == denial("Write")

    def test_without_a_transcript_path_the_gate_finds_it_under_the_config_dir(
        self, rig: GateRig
    ) -> None:
        rig.write([human, peer])
        rig.hook_transcript = None
        assert rig.decide("Write") == denial("Write")


class TestNotGated:
    def test_a_full_tools_agent_is_never_gated(self, rig: GateRig) -> None:
        rig.write_roster(claude_roster(rig.cfg, tools="full"))
        rig.write([human, peer])
        assert rig.decide("Write") is None

    def test_a_missing_transcript_allows(self, rig: GateRig) -> None:
        assert not rig.writer.path.exists()
        assert rig.decide("Write") is None

    def test_an_ambiguous_identity_allows(self, rig: GateRig) -> None:
        rig.write_roster(claude_roster(rig.cfg, tools="read_only", twin=True))
        rig.write([human, peer])
        assert rig.decide("Write") is None

    @pytest.mark.parametrize("name", ["ghost", "sender"])
    def test_a_name_that_matches_no_claude_agent_allows(self, rig: GateRig, name: str) -> None:
        rig.write([human, peer])
        assert rig.decide("Write", {**rig.env, "MZ_RELAY_NAME": name}) is None


class TestIdentity:
    @pytest.mark.parametrize("variable", ["MZ_RELAY_NAME", "CLAUDE_PLUGIN_OPTION_RELAY_NAME"])
    def test_a_configured_name_picks_the_agent_on_a_shared_config_dir(
        self, rig: GateRig, variable: str
    ) -> None:
        rig.write_roster(claude_roster(rig.cfg, tools="read_only", twin=True))
        rig.write([human, peer])
        assert rig.decide("Write", {**rig.env, variable: "cc"}) == denial("Write")
        assert rig.decide("Write", {**rig.env, variable: "cf"}) is None

    @pytest.mark.parametrize("raw", ["", "${user_config.relay_name}"])
    def test_an_unset_looking_plugin_option_falls_back_to_the_config_dir(
        self, rig: GateRig, raw: str
    ) -> None:
        rig.write([human, peer])
        env = {**rig.env, "CLAUDE_PLUGIN_OPTION_RELAY_NAME": raw}
        assert rig.decide("Write", env) == denial("Write")


class TestRunHookGate:
    def run(self, rig: GateRig, stdin: str, env: dict[str, str] | None = None) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        full_env = {**rig.env, "MZ_RELAY_HOME": str(rig.home), **(env or {})}
        code = run_hook(["gate"], io.StringIO(stdin), out, err, full_env)
        assert err.getvalue() == ""
        return code, out.getvalue()

    def hooks_log(self, rig: GateRig) -> str:
        log = rig.paths.hooks_log
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def test_a_denial_is_one_json_object_on_stdout_and_exit_0(self, rig: GateRig) -> None:
        rig.write([human, peer])
        code, out = self.run(rig, pre_tool_use("Edit", rig.writer.path))
        assert code == 0
        assert json.loads(out) == denial("Edit")
        assert out.strip().count("\n") == 0

    def test_an_allowed_tool_prints_nothing(self, rig: GateRig) -> None:
        rig.write([human, peer])
        assert self.run(rig, pre_tool_use("Read", rig.writer.path)) == (0, "")

    def test_malformed_stdin_prints_nothing_and_is_logged(self, rig: GateRig) -> None:
        rig.write([human, peer])
        assert self.run(rig, "not json") == (0, "")
        assert rig.paths.hooks_log.read_text(encoding="utf-8").strip()

    @pytest.mark.parametrize("roster", [None, "version = [broken"])
    def test_a_missing_or_broken_roster_never_blocks_tools(
        self, rig: GateRig, roster: str | None
    ) -> None:
        if roster is None:
            rig.paths.roster.unlink()
        else:
            rig.write_roster(roster)
        rig.write([human, peer])
        assert self.run(rig, pre_tool_use("Write", rig.writer.path)) == (0, "")

    @pytest.mark.parametrize(
        ("tools", "steps"),
        [("full", [human, peer]), ("read_only", [human, assistant])],
        ids=["full-tools-in-a-peer-turn", "read-only-outside-a-peer-turn"],
    )
    def test_an_allowed_call_writes_no_info_line_to_hooks_log(
        self, rig: GateRig, tools: str, steps: list[Step]
    ) -> None:
        # The gate runs before every tool call and hooks.log is never rotated.
        rig.write_roster(claude_roster(rig.cfg, tools=tools))
        rig.write(steps)

        assert self.run(rig, pre_tool_use("Write", rig.writer.path)) == (0, "")

        info = [line for line in self.hooks_log(rig).splitlines() if " INFO " in line]
        assert info == []

    def test_a_denial_is_still_logged(self, rig: GateRig) -> None:
        rig.write([human, peer])

        self.run(rig, pre_tool_use("Edit", rig.writer.path))

        assert "denied Edit to cc" in self.hooks_log(rig)

    @pytest.mark.parametrize("directory", ["turn_end_dir", "marker_dir"])
    def test_a_corrupt_offset_file_is_dropped_with_one_warning(
        self, rig: GateRig, directory: str
    ) -> None:
        # Left in place, a corrupt file would write a traceback on every tool call.
        offset_dir: Path = getattr(rig.paths, directory)
        offset_dir.mkdir(parents=True, exist_ok=True)
        (offset_dir / SESSION_ID).write_text("12x", encoding="ascii")
        rig.write([human, assistant])

        first = self.run(rig, pre_tool_use("Write", rig.writer.path))
        second = self.run(rig, pre_tool_use("Write", rig.writer.path))

        assert (first, second) == ((0, ""), (0, ""))
        assert not (offset_dir / SESSION_ID).exists()
        log = self.hooks_log(rig)
        assert "Traceback" not in log
        assert sum(SESSION_ID in line for line in log.splitlines() if " WARNING " in line) == 1

    def test_a_relative_claude_config_dir_allows_quietly(self, rig: GateRig) -> None:
        rig.write([human, peer])

        got = self.run(rig, pre_tool_use("Write", rig.writer.path), {"CLAUDE_CONFIG_DIR": "cfg"})

        assert got == (0, "")
        assert "Traceback" not in self.hooks_log(rig)


class TestHookGateCommand:
    def test_bin_hook_gate_denies_write_in_a_peer_turn_without_starting_relayd(
        self, rig: GateRig, relay_env: dict[str, str]
    ) -> None:
        rig.write([human, peer])
        done = subprocess.run(
            [str(BIN), "hook", "gate"],
            input=pre_tool_use("Write", rig.writer.path),
            capture_output=True,
            text=True,
            env={**relay_env, "CLAUDE_CONFIG_DIR": str(rig.cfg)},
            timeout=PROC_WAIT_S,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        decision = json.loads(done.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        # The gate runs before every tool call; it reads local files and never spawns relayd.
        assert not rig.paths.socket.exists()
