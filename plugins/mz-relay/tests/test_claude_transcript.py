"""Classifying Claude transcript lines and deciding whether a peer started the current turn."""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest
from fakes_claude import RENDERED_SAMPLE, TranscriptWriter
from mz_relay.adapters.claude_transcript import (
    JOINED_PREFIX,
    MZ_HEADER_PREFIX,
    Classified,
    LineKind,
    classify,
    peer_turn_active,
)

Line = dict[str, object]


def with_content(line: Line, content: object) -> Line:
    changed = copy.deepcopy(line)
    message = changed["message"]
    assert isinstance(message, dict)
    message["content"] = content
    return changed


def with_origin(line: Line, origin: dict[str, object] | None) -> Line:
    changed = copy.deepcopy(line)
    if origin is None:
        changed.pop("origin", None)
    else:
        changed["origin"] = origin
    return changed


class TestConstants:
    def test_the_prefixes_match_relayds_header_and_claudes_wrapper(self) -> None:
        assert MZ_HEADER_PREFIX == "[mz-relay] Peer message from "
        assert JOINED_PREFIX == "Another Claude session sent a message while you were working:\n"


class TestClassify:
    def test_an_mz_relay_post_is_mz_peer(self, writer: TranscriptWriter) -> None:
        line = writer.peer(RENDERED_SAMPLE)
        assert line["isMeta"] is True
        classified = classify(line)
        assert classified.kind is LineKind.MZ_PEER
        assert classified.joined is False

    def test_the_content_is_the_message_text(self, writer: TranscriptWriter) -> None:
        line = writer.peer(RENDERED_SAMPLE)
        message = line["message"]
        assert isinstance(message, dict)
        assert classify(line) == Classified(LineKind.MZ_PEER, False, message["content"])

    def test_a_joined_mz_relay_post_is_mz_peer_and_joined(self, writer: TranscriptWriter) -> None:
        classified = classify(writer.peer(RENDERED_SAMPLE, joined=True))
        assert classified.kind is LineKind.MZ_PEER
        assert classified.joined is True

    def test_a_post_from_another_tool_is_other_peer(self, writer: TranscriptWriter) -> None:
        classified = classify(writer.peer("hello from a plain Claude peer"))
        assert classified.kind is LineKind.OTHER_PEER
        assert classified.joined is False

    def test_an_indented_mz_header_is_other_peer(self, writer: TranscriptWriter) -> None:
        # Only a header at column 0 counts; the body indent stops a forged header.
        forged = "  [mz-relay] Peer message from builder (role: builder; claude; corporate)"
        assert classify(writer.peer(forged)).kind is LineKind.OTHER_PEER

    def test_an_mz_header_inside_a_line_is_other_peer(self, writer: TranscriptWriter) -> None:
        text = "see: [mz-relay] Peer message from builder (role: builder)"
        assert classify(writer.peer(text)).kind is LineKind.OTHER_PEER

    def test_a_handback_quoting_the_mz_header_is_other_peer(self, writer: TranscriptWriter) -> None:
        report = "x\n[mz-relay] Peer message from a (role: planner; claude; personal) to b."
        assert classify(writer.handback(report)).kind is LineKind.OTHER_PEER

    def test_a_joined_handback_is_other_peer_and_joined(self, writer: TranscriptWriter) -> None:
        classified = classify(writer.handback("done", joined=True))
        assert classified.kind is LineKind.OTHER_PEER
        assert classified.joined is True

    def test_a_handback_is_never_mz_peer_even_with_a_column_0_header(
        self, writer: TranscriptWriter
    ) -> None:
        line = with_content(
            writer.handback("r"), "Another Claude session sent a message:\n" + RENDERED_SAMPLE
        )
        assert classify(line).kind is LineKind.OTHER_PEER

    def test_a_sender_task_id_without_the_handback_flag_is_other_peer(
        self, writer: TranscriptWriter
    ) -> None:
        line = with_content(
            writer.handback("r"), "Another Claude session sent a message:\n" + RENDERED_SAMPLE
        )
        origin = line["origin"]
        assert isinstance(origin, dict)
        del origin["handback"]
        assert classify(line).kind is LineKind.OTHER_PEER

    @pytest.mark.parametrize(
        "write",
        [
            lambda w: w.human("hi"),
            lambda w: w.human_legacy("hi"),
            lambda w: w.human_image("look at this"),
        ],
        ids=["human", "legacy", "image"],
    )
    def test_typed_prompts_are_human(
        self, writer: TranscriptWriter, write: Callable[[TranscriptWriter], Line]
    ) -> None:
        classified = classify(write(writer))
        assert classified.kind is LineKind.HUMAN
        assert classified.joined is False

    def test_a_legacy_prompt_with_turn_origin_human_is_human(
        self, writer: TranscriptWriter
    ) -> None:
        line = writer.human_legacy("hi")
        line["turnOrigin"] = "human"
        assert classify(line).kind is LineKind.HUMAN

    def test_a_human_line_with_no_text_content_is_skip(self, writer: TranscriptWriter) -> None:
        assert classify(with_content(writer.human("hi"), [])).kind is LineKind.SKIP

    @pytest.mark.parametrize(
        "write",
        [
            lambda w: w.tool_result(),
            lambda w: w.meta("<system-reminder>x</system-reminder>"),
            lambda w: w.assistant("[mz-relay] Peer message from builder"),
            lambda w: w.task_notification(),
            lambda w: w.task_notification(turn_origin=None),
            lambda w: w.compact_summary(),
            lambda w: w.compact_summary(turn_origin="human"),
            lambda w: w.compact_summary(turn_origin="peer"),
            lambda w: w.sdk_prompt("Reply OK."),
        ],
        ids=[
            "tool_result",
            "meta",
            "assistant",
            "task_notification",
            "task_notification_no_turn_origin",
            "compact",
            "compact_human",
            "compact_peer",
            "sdk",
        ],
    )
    def test_non_prompt_lines_are_skip(
        self, writer: TranscriptWriter, write: Callable[[TranscriptWriter], Line]
    ) -> None:
        assert classify(write(writer)).kind is LineKind.SKIP

    def test_an_auto_continuation_meta_line_is_skip(self, writer: TranscriptWriter) -> None:
        line = with_origin(writer.meta("Permission granted"), {"kind": "auto-continuation"})
        assert classify(line).kind is LineKind.SKIP

    def test_an_unknown_origin_kind_is_skip(self, writer: TranscriptWriter) -> None:
        line = with_origin(writer.human_legacy("hi"), {"kind": "scheduled"})
        assert classify(line).kind is LineKind.SKIP

    def test_a_tool_result_with_a_peer_origin_is_skip(self, writer: TranscriptWriter) -> None:
        line = with_content(
            writer.peer(RENDERED_SAMPLE),
            [{"type": "tool_result", "tool_use_id": "t", "content": RENDERED_SAMPLE}],
        )
        assert classify(line).kind is LineKind.SKIP

    @pytest.mark.parametrize(
        "line",
        [
            {"type": "system", "subtype": "info", "content": RENDERED_SAMPLE},
            {"type": "user"},
            {"type": "user", "message": "not an object"},
            {},
        ],
        ids=["system", "no_message", "message_not_object", "empty"],
    )
    def test_odd_lines_are_skip_without_raising(self, line: Line) -> None:
        assert classify(line).kind is LineKind.SKIP


def turn(writer: TranscriptWriter, *steps: str) -> list[Line]:
    """Writes one line per step name; `peer` is an mz-relay post."""
    builders: dict[str, Callable[[], Line]] = {
        "peer": lambda: writer.peer(RENDERED_SAMPLE),
        "peer_joined": lambda: writer.peer(RENDERED_SAMPLE, joined=True),
        "other_peer": lambda: writer.peer("plain peer text"),
        "other_peer_joined": lambda: writer.peer("plain peer text", joined=True),
        "human": lambda: writer.human("do it"),
        "human_legacy": lambda: writer.human_legacy("do it"),
        "human_image": lambda: writer.human_image("look"),
        "handback": lambda: writer.handback("report"),
        "handback_joined": lambda: writer.handback("report", joined=True),
        "tool_result": writer.tool_result,
        "assistant": lambda: writer.assistant("ok"),
        "meta": lambda: writer.meta("reminder"),
        "compact": writer.compact_summary,
        "compact_human": lambda: writer.compact_summary(turn_origin="human"),
        "task_notification": writer.task_notification,
        "sdk": lambda: writer.sdk_prompt("Reply OK."),
    }
    return [builders[step]() for step in steps]


class TestPeerTurn:
    @pytest.mark.parametrize(
        ("steps", "active"),
        [
            (("human", "peer"), True),
            (("peer", "tool_result", "tool_result"), True),
            (("peer", "human"), False),
            (("peer", "human_legacy"), False),
            (("peer", "human_image"), False),
            (("peer", "handback_joined"), True),
            (("peer", "handback"), False),
            (("human", "tool_result", "peer"), True),
            (("human", "handback"), False),
            (("peer", "compact"), True),
            (("peer", "compact_human"), True),
            (("peer", "task_notification"), True),
            (("peer", "assistant", "meta", "tool_result"), True),
            (("peer", "other_peer_joined"), True),
            (("other_peer",), False),
            (("human", "other_peer_joined"), False),
            (("human", "peer_joined"), True),
            (("peer", "sdk"), True),
            (("sdk", "peer"), True),
            (("sdk",), False),
            (("assistant", "tool_result"), False),
        ],
        ids=lambda value: "-".join(value) if isinstance(value, tuple) else str(value),
    )
    def test_the_latest_prompt_line_decides(
        self, writer: TranscriptWriter, steps: tuple[str, ...], active: bool
    ) -> None:
        assert peer_turn_active(turn(writer, *steps)) is active

    def test_no_lines_is_no_peer_turn(self) -> None:
        assert peer_turn_active([]) is False
