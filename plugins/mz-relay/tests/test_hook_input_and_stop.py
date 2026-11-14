"""Hook input parsing and the Stop waiter's turn-end record, which comes before the session lock."""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes_claude import CWD, HookEvent, TranscriptWriter
from mz_relay.clock import SystemClock
from mz_relay.errors import EnvelopeError
from mz_relay.hooks import HookInput, Waiter, parse_hook_input, run_hook
from mz_relay.paths import Paths
from relay_rig import InThread, NoSpawn, launcher_for, waiter_lock_held

SID = "0b3f6c1e-5a52-4d0e-9a51-6f1d2c3b4a59"


def hook_json(**fields: object) -> str:
    base: dict[str, object] = {
        "session_id": SID,
        "cwd": CWD,
        "transcript_path": f"{CWD}/t.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    base.update(fields)
    return json.dumps({key: value for key, value in base.items() if value is not None})


class TestParseHookInput:
    def test_session_start_input(self) -> None:
        got = parse_hook_input(hook_json())

        assert got == HookInput(
            session_id=SID,
            cwd=Path(CWD),
            transcript_path=Path(CWD) / "t.jsonl",
            tool_name=None,
            event="SessionStart",
            source="startup",
        )

    def test_pre_tool_use_input_carries_the_tool(self) -> None:
        raw = hook_json(hook_event_name="PreToolUse", source=None, tool_name="Write")

        got = parse_hook_input(raw)

        assert (got.event, got.tool_name, got.source) == ("PreToolUse", "Write", None)

    def test_stop_input_carries_the_prompt_id(self) -> None:
        raw = hook_json(hook_event_name="Stop", source=None, prompt_id="6dfb3ef7")

        assert parse_hook_input(raw).prompt_id == "6dfb3ef7"

    def test_missing_transcript_path_is_none(self) -> None:
        assert parse_hook_input(hook_json(transcript_path=None)).transcript_path is None

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            hook_json(session_id=None),
            hook_json(session_id=""),
            hook_json(session_id=7),
            # The id names lock and marker files, so a path in it must not escape their folders.
            hook_json(session_id="../s-1"),
            hook_json(prompt_id=""),
            hook_json(prompt_id=7),
        ],
    )
    def test_bad_input_is_invalid_args(self, raw: str) -> None:
        with pytest.raises(EnvelopeError) as caught:
            parse_hook_input(raw)
        assert caught.value.code == "invalid_args"


class StopRig:
    """A transcript, a held session lock and a launcher that must never be used."""

    def __init__(self, paths: Paths, home: Path) -> None:
        self.paths = paths
        self.writer = TranscriptWriter(home / "proj" / f"{SID}.jsonl", session_id=SID)
        self.spawn = NoSpawn()
        self.waiter = Waiter(
            paths,
            launcher_for(paths, self.spawn),
            env={"CLAUDE_CONFIG_DIR": str(home / "cfg"), "MZ_RELAY_NAME": "cc"},
            clock=SystemClock(),
        )
        self.turn_end = paths.turn_end_dir / SID
        self.marker = paths.marker_dir / SID
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(waiter_lock_held(paths, SID))

    def write_marker(self, offset: int) -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(str(offset), encoding="ascii")

    def run(
        self, event: HookEvent, *, transcript: Path | None = None, prompt_id: str | None = None
    ) -> tuple[int, str]:
        path = self.writer.path if transcript is None else transcript
        raw = hook_json(hook_event_name=event, transcript_path=str(path), prompt_id=prompt_id)
        hook = parse_hook_input(raw)
        stderr = io.StringIO()
        code = InThread(lambda: self.waiter.run(event, hook, stderr)).result(5.0)
        return code, stderr.getvalue()

    def close(self) -> None:
        self._stack.close()


class TestRelativeConfigDir:
    def test_a_waiter_with_a_relative_claude_config_dir_exits_0_quietly(
        self, relay_paths: Paths, relay_env: dict[str, str]
    ) -> None:
        stderr = io.StringIO()
        env = {**relay_env, "CLAUDE_CONFIG_DIR": "relative/cfg", "MZ_RELAY_NAME": "cc"}
        stdin = io.StringIO(hook_json(transcript_path=None))

        code = run_hook(["wait", "SessionStart"], stdin, io.StringIO(), stderr, env)

        assert (code, stderr.getvalue()) == (0, "")
        log = relay_paths.hooks_log.read_text(encoding="utf-8")
        assert "Traceback" not in log
        assert "CLAUDE_CONFIG_DIR" in log


@pytest.fixture
def stop_rig(relay_paths: Paths, relay_home: Path) -> Iterator[StopRig]:
    built = StopRig(relay_paths, relay_home)
    yield built
    built.close()


class TestStopWithLockHeld:
    def test_stop_records_the_turn_end_and_exits_quietly(self, stop_rig: StopRig) -> None:
        stop_rig.writer.human("review the plan")
        stop_rig.writer.assistant("done")

        code, err = stop_rig.run("Stop")

        assert (code, err) == (0, "")
        size = stop_rig.writer.path.stat().st_size
        assert stop_rig.turn_end.read_text(encoding="ascii") == str(size)
        assert stop_rig.spawn.calls == []

    def test_stop_removes_the_rewake_marker(self, stop_rig: StopRig) -> None:
        stop_rig.writer.human("review the plan")
        stop_rig.writer.assistant("done")
        stop_rig.write_marker(0)

        stop_rig.run("Stop")

        assert not stop_rig.marker.exists()

    def test_later_stop_moves_the_turn_end_forward(self, stop_rig: StopRig) -> None:
        stop_rig.writer.human("first")
        stop_rig.run("Stop")
        stop_rig.writer.human("second")
        stop_rig.writer.assistant("ok")

        stop_rig.run("Stop")

        size = stop_rig.writer.path.stat().st_size
        assert stop_rig.turn_end.read_text(encoding="ascii") == str(size)

    def test_a_late_stop_records_the_turn_end_past_the_last_assistant_line(
        self, stop_rig: StopRig
    ) -> None:
        stop_rig.writer.human("review the plan")
        stop_rig.writer.assistant("done")
        turn_end = stop_rig.writer.path.stat().st_size
        # The next turn's prompt lands before the async Stop hook gets to run.
        stop_rig.writer.peer("[mz-relay] Peer message from sender to cc.")

        stop_rig.run("Stop")

        assert stop_rig.turn_end.read_text(encoding="ascii") == str(turn_end)

    def test_a_late_stop_with_its_prompt_id_records_its_own_turn_end(
        self, stop_rig: StopRig
    ) -> None:
        stop_rig.writer.human("review the plan")
        own_turn = stop_rig.writer.prompt_id
        stop_rig.writer.assistant("done")
        turn_end = stop_rig.writer.path.stat().st_size
        # The next peer turn has already replied once when the async Stop hook runs.
        stop_rig.writer.peer("[mz-relay] Peer message from sender to cc.")
        stop_rig.writer.assistant("checking")
        stop_rig.writer.tool_result()

        stop_rig.run("Stop", prompt_id=own_turn)

        assert stop_rig.turn_end.read_text(encoding="ascii") == str(turn_end)

    def test_a_post_that_joined_the_turn_does_not_cut_its_end_short(
        self, stop_rig: StopRig
    ) -> None:
        stop_rig.writer.human("review the plan")
        own_turn = stop_rig.writer.prompt_id
        stop_rig.writer.assistant("reading")
        stop_rig.writer.peer("[mz-relay] Peer message from sender to cc.", joined=True)
        stop_rig.writer.assistant("done")

        stop_rig.run("Stop", prompt_id=own_turn)

        size = stop_rig.writer.path.stat().st_size
        assert stop_rig.turn_end.read_text(encoding="ascii") == str(size)

    def test_a_prompt_id_missing_from_the_transcript_uses_the_newest_turn_end(
        self, stop_rig: StopRig
    ) -> None:
        stop_rig.writer.human("review the plan")
        stop_rig.writer.assistant("done")

        stop_rig.run("Stop", prompt_id="not-in-the-transcript")

        size = stop_rig.writer.path.stat().st_size
        assert stop_rig.turn_end.read_text(encoding="ascii") == str(size)

    def test_a_late_stop_keeps_a_marker_no_assistant_line_follows(self, stop_rig: StopRig) -> None:
        stop_rig.writer.human("review the plan")
        stop_rig.writer.assistant("done")
        stop_rig.write_marker(stop_rig.writer.path.stat().st_size)
        stop_rig.writer.meta("Stop hook feedback")

        stop_rig.run("Stop")

        assert stop_rig.marker.exists()

    def test_stop_without_a_transcript_writes_no_turn_end_but_drops_the_marker(
        self, stop_rig: StopRig, relay_home: Path
    ) -> None:
        stop_rig.write_marker(0)

        code, _ = stop_rig.run("Stop", transcript=relay_home / "missing.jsonl")

        assert code == 0
        assert not stop_rig.turn_end.exists()
        assert not stop_rig.marker.exists()

    def test_duplicate_session_start_records_nothing_and_exits_quietly(
        self, stop_rig: StopRig
    ) -> None:
        stop_rig.writer.human("hello")
        stop_rig.write_marker(0)

        code, err = stop_rig.run("SessionStart")

        assert (code, err) == (0, "")
        assert not stop_rig.turn_end.exists()
        assert stop_rig.marker.exists()
        assert stop_rig.spawn.calls == []
