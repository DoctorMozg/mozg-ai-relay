"""Codex thread lookup by /rename name, and finding a message's nonce in the thread's rollout."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from codex_rig import write_index, write_rollout
from mz_relay.adapters.codex_threads import (
    CodexThread,
    CodexThreadMissing,
    find_rollout,
    resolve_thread,
    rollout_has_nonce,
)

OLD = {"id": "t-old", "thread_name": "reviewer", "updated_at": "2026-11-10T09:00:00.123456789Z"}
NEW = {"id": "t-new", "thread_name": "reviewer", "updated_at": "2026-11-12T09:00:00.123456789Z"}
OTHER = {"id": "t-other", "thread_name": "other", "updated_at": "2026-11-13T09:00:00.000000000Z"}
RENAME_HINT = "run /rename reviewer in the Codex TUI"


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex"
    home.mkdir()
    return home


class TestResolveThread:
    def test_a_single_match_is_that_thread(self, codex_home: Path) -> None:
        write_index(codex_home, OLD, OTHER)

        assert resolve_thread(codex_home, "reviewer") == CodexThread(
            id="t-old", name="reviewer", updated_at=OLD["updated_at"]
        )

    def test_the_newest_of_several_matches_wins_wherever_it_sits(self, codex_home: Path) -> None:
        write_index(codex_home, NEW, OTHER, OLD)

        assert resolve_thread(codex_home, "reviewer").id == "t-new"

    def test_several_matches_log_one_warning_with_the_count_and_the_choice(
        self, codex_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_index(codex_home, OLD, NEW, OTHER)

        with caplog.at_level(logging.WARNING):
            resolve_thread(codex_home, "reviewer")

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == ["2 Codex threads named reviewer; using t-new"]

    def test_a_single_match_logs_no_warning(
        self, codex_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_index(codex_home, NEW, OTHER)

        with caplog.at_level(logging.WARNING):
            resolve_thread(codex_home, "reviewer")

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_times_compare_as_instants_not_as_strings(self, codex_home: Path) -> None:
        # As text "...00Z" sorts after "...00.100000000Z", though it is the older time.
        whole = {"id": "t-whole", "thread_name": "reviewer", "updated_at": "2026-11-12T10:00:00Z"}
        later = {**whole, "id": "t-later", "updated_at": "2026-11-12T10:00:00.100000000Z"}
        write_index(codex_home, later, whole)

        assert resolve_thread(codex_home, "reviewer").id == "t-later"

    def test_of_equal_times_the_later_line_wins(self, codex_home: Path) -> None:
        # Codex appends to the index, so the later line is the later write.
        write_index(codex_home, {**NEW, "id": "t-first"}, OTHER, {**NEW, "id": "t-second"})

        assert resolve_thread(codex_home, "reviewer").id == "t-second"

    def test_malformed_lines_are_skipped(self, codex_home: Path) -> None:
        write_index(
            codex_home,
            "{not json",
            ["t-list", "reviewer"],
            {"id": "t-no-name", "updated_at": "2026-11-13T00:00:00Z"},
            {"thread_name": "reviewer", "updated_at": "2026-11-13T00:00:00Z"},
            "",
            OLD,
        )

        assert resolve_thread(codex_home, "reviewer").id == "t-old"

    def test_matching_lines_with_bad_fields_are_skipped(self, codex_home: Path) -> None:
        bad_time = {**NEW, "id": "t-bad-time", "updated_at": "yesterday"}
        no_time = {"id": "t-no-time", "thread_name": "reviewer"}
        int_time = {**NEW, "id": "t-int-time", "updated_at": 5}
        int_id = {**NEW, "id": 7}
        list_name = {**NEW, "id": "t-list-name", "thread_name": ["reviewer"]}
        write_index(codex_home, bad_time, no_time, int_time, int_id, list_name, OLD)

        assert resolve_thread(codex_home, "reviewer").id == "t-old"

    def test_the_name_must_match_exactly(self, codex_home: Path) -> None:
        write_index(
            codex_home, {**OLD, "thread_name": "reviewer-2"}, {**NEW, "thread_name": "Reviewer"}
        )

        with pytest.raises(CodexThreadMissing):
            resolve_thread(codex_home, "reviewer")

    def test_no_match_raises_with_the_rename_hint(self, codex_home: Path) -> None:
        write_index(codex_home, OTHER)

        with pytest.raises(CodexThreadMissing) as caught:
            resolve_thread(codex_home, "reviewer")

        assert RENAME_HINT in str(caught.value)
        assert caught.value.code == "codex_thread_missing"

    def test_a_missing_index_raises_with_the_rename_hint(self, codex_home: Path) -> None:
        with pytest.raises(CodexThreadMissing) as caught:
            resolve_thread(codex_home, "reviewer")

        assert RENAME_HINT in str(caught.value)


class TestFindRollout:
    def test_the_rollout_of_the_thread_is_found(self, codex_home: Path) -> None:
        path = write_rollout(codex_home, "2026/11/12", "t-new", mtime=1_000.0)
        write_rollout(codex_home, "2026/11/12", "t-other", mtime=2_000.0)

        assert find_rollout(codex_home, "t-new") == path

    def test_the_newest_mtime_wins_across_days(self, codex_home: Path) -> None:
        write_rollout(codex_home, "2026/11/13", "t-new", mtime=1_000.0)
        newest = write_rollout(codex_home, "2026/11/10", "t-new", mtime=5_000.0)

        assert find_rollout(codex_home, "t-new") == newest

    @pytest.mark.parametrize("folder", ["", "2026/11/13/extra"], ids=["too-high", "too-deep"])
    def test_a_rollout_outside_the_day_folders_is_ignored(
        self, codex_home: Path, folder: str
    ) -> None:
        stray = codex_home / "sessions" / folder / "rollout-2026-11-13T10-00-00-t-new.jsonl"
        stray.parent.mkdir(parents=True)
        stray.write_text("", encoding="utf-8")

        assert find_rollout(codex_home, "t-new") is None

    def test_no_rollout_is_none(self, codex_home: Path) -> None:
        assert find_rollout(codex_home, "t-new") is None


class TestRolloutHasNonce:
    NONCE = "01JC0000000000000000009001"

    def test_a_line_holding_the_nonce_is_found(self, codex_home: Path) -> None:
        line = json.dumps({"type": "response_item", "payload": {"text": f"id: {self.NONCE}"}})
        path = write_rollout(codex_home, "2026/11/13", "t-new", mtime=1.0, text=f"{{}}\n{line}\n")

        assert rollout_has_nonce(path, self.NONCE)

    def test_a_rollout_without_the_nonce_is_false(self, codex_home: Path) -> None:
        text = json.dumps({"payload": {"text": "id: 01JC0000000000000000009002"}}) + "\n"
        path = write_rollout(codex_home, "2026/11/13", "t-new", mtime=1.0, text=text)

        assert not rollout_has_nonce(path, self.NONCE)

    def test_a_last_line_without_a_newline_is_read(self, codex_home: Path) -> None:
        path = write_rollout(codex_home, "2026/11/13", "t-new", text=f"{{}}\nid: {self.NONCE}")

        assert rollout_has_nonce(path, self.NONCE)

    def test_a_missing_rollout_is_false(self, codex_home: Path) -> None:
        assert not rollout_has_nonce(codex_home / "gone.jsonl", self.NONCE)
