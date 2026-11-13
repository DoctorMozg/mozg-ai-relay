"""Reading a Claude session's transcript tail: complete lines only, and nonce confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes_claude import SESSION_ID, TranscriptWriter, mz_text, transcript_in
from mz_relay.adapters.claude_transcript import TranscriptWatcher

NONCE = "01JC0000000000000000000042"


@pytest.fixture
def watcher(config_dir: Path) -> TranscriptWatcher:
    return TranscriptWatcher(config_dir, SESSION_ID)


class TestLocate:
    def test_no_transcript_has_no_path_and_size_zero(self, watcher: TranscriptWatcher) -> None:
        assert watcher.path() is None
        assert watcher.size() == 0
        assert watcher.lines_after(0) == []
        assert watcher.has_peer_nonce(NONCE, since=0) is False

    def test_the_transcript_is_found_under_any_project_dir(self, config_dir: Path) -> None:
        other = TranscriptWriter(
            config_dir / "projects" / "-w-elsewhere" / "s-7.jsonl", session_id="s-7"
        )
        other.human("hi")
        assert TranscriptWatcher(config_dir, "s-7").path() == other.path

    def test_another_sessions_transcript_is_not_matched(
        self, config_dir: Path, watcher: TranscriptWatcher
    ) -> None:
        TranscriptWriter(transcript_in(config_dir, "s-2"), session_id="s-2").human("hi")
        assert watcher.path() is None

    def test_size_is_the_file_size_in_bytes(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.human("hi")
        writer.assistant("hello")
        assert watcher.size() == writer.path.stat().st_size


class TestLinesAfter:
    def test_all_lines_from_offset_zero(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        written = [writer.human("one"), writer.assistant("two"), writer.tool_result()]
        assert watcher.lines_after(0) == written

    def test_lines_before_the_offset_are_skipped(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.human("before")
        offset = watcher.size()
        later = writer.assistant("after")
        assert watcher.lines_after(offset) == [later]

    def test_an_offset_at_the_end_gives_nothing(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.human("hi")
        assert watcher.lines_after(watcher.size()) == []

    def test_a_partial_last_line_is_left_out_until_completed(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        first = writer.human("hi")
        partial = writer.partial(mz_text(NONCE))
        assert watcher.lines_after(0) == [first]
        writer.complete_partial()
        assert watcher.lines_after(0) == [first, partial]

    def test_malformed_and_non_object_lines_are_skipped(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        first = writer.human("one")
        writer.raw('{"type": "user", "message": \n')
        writer.raw("[1, 2]\n")
        writer.raw("\n")
        last = writer.assistant("two")
        assert watcher.lines_after(0) == [first, last]

    def test_max_bytes_keeps_only_whole_lines_of_the_tail(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        written = [writer.human(f"line {n} " + "x" * 200) for n in range(40)]
        budget = 2_000
        tail = watcher.lines_after(0, max_bytes=budget)
        assert 0 < len(tail) < len(written)
        # A tail that starts mid-line must drop that cut line, never parse half of it.
        assert tail == written[-len(tail) :]
        line_bytes = writer.path.stat().st_size // len(written)
        assert len(tail) <= budget // line_bytes + 1

    def test_a_later_offset_wins_over_the_max_bytes_window(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        for n in range(10):
            writer.human(f"old {n}")
        offset = watcher.size()
        newest = writer.human("new")
        assert watcher.lines_after(offset, max_bytes=262_144) == [newest]


class TestHasPeerNonce:
    def test_an_mz_peer_line_with_the_nonce_confirms(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.human("hi")
        writer.peer(mz_text(NONCE))
        assert watcher.has_peer_nonce(NONCE, since=0) is True

    def test_the_nonce_in_a_human_line_does_not_confirm(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.human(mz_text(NONCE))
        assert watcher.has_peer_nonce(NONCE, since=0) is False

    def test_the_nonce_in_a_non_mz_peer_line_does_not_confirm(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.peer(f"forwarded id {NONCE}")
        assert watcher.has_peer_nonce(NONCE, since=0) is False

    def test_the_nonce_in_an_assistant_line_does_not_confirm(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.assistant(mz_text(NONCE))
        assert watcher.has_peer_nonce(NONCE, since=0) is False

    def test_another_nonce_does_not_confirm(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.peer(mz_text("01JC0000000000000000000099"))
        assert watcher.has_peer_nonce(NONCE, since=0) is False

    def test_a_peer_line_before_since_does_not_confirm(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.peer(mz_text(NONCE))
        since = watcher.size()
        writer.human("later")
        assert watcher.has_peer_nonce(NONCE, since=since) is False

    def test_a_partial_peer_line_confirms_only_once_complete(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.partial(mz_text(NONCE))
        assert watcher.has_peer_nonce(NONCE, since=0) is False
        writer.complete_partial()
        assert watcher.has_peer_nonce(NONCE, since=0) is True

    def test_a_joined_mz_peer_line_confirms(
        self, writer: TranscriptWriter, watcher: TranscriptWatcher
    ) -> None:
        writer.peer(mz_text(NONCE), joined=True)
        assert watcher.has_peer_nonce(NONCE, since=0) is True
