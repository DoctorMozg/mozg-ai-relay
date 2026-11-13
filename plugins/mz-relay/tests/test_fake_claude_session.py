"""The Claude test doubles: transcript line shapes and the fake session's socket and registry."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fakes_claude import (
    CWD,
    JOINED_PEER_PREFIX,
    PEER_PREFIX,
    PINNED_VERSION,
    RENDERED_SAMPLE,
    FakeClaudeSession,
    TranscriptWriter,
    mz_text,
    project_dir_name,
)
from relay_rig import WAIT_S, wait_until

FRAME = b'{"type":"user","message":{"role":"user","content":"hello"}}\n'

# Every key of a real cross-session peer line (CC 2.1.323) except the optional `slug`.
PEER_KEYS = {
    "parentUuid",
    "isSidechain",
    "promptId",
    "type",
    "message",
    "isMeta",
    "uuid",
    "timestamp",
    "permissionMode",
    "origin",
    "promptSource",
    "queueSkipSlashCommands",
    "turnOrigin",
    "turnPosition",
    "queueSkipAttachments",
    "userType",
    "entrypoint",
    "cwd",
    "sessionId",
    "version",
    "gitBranch",
}


def post(path: Path, data: bytes) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(path))
        conn.sendall(data)


def read_lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def content_of(line: dict[str, object]) -> object:
    return cast(dict[str, object], line["message"])["content"]


def registry(session: FakeClaudeSession) -> dict[str, object]:
    return cast(dict[str, object], json.loads(session.registry_path.read_text(encoding="utf-8")))


@pytest.fixture
def session(relay_home: Path) -> Iterator[FakeClaudeSession]:
    with FakeClaudeSession(relay_home) as fake:
        yield fake


class TestTranscriptWriter:
    def test_each_method_appends_the_line_it_returns(self, writer: TranscriptWriter) -> None:
        written = [
            writer.human("a"),
            writer.peer("b"),
            writer.handback("c"),
            writer.tool_result(),
            writer.assistant("d"),
            writer.meta("e"),
            writer.task_notification(),
            writer.compact_summary(),
            writer.sdk_prompt("f"),
            writer.human_legacy("g"),
            writer.human_image("h"),
        ]
        assert read_lines(writer.path) == written

    def test_lines_chain_through_parent_uuid(self, writer: TranscriptWriter) -> None:
        first = writer.human("a")
        second = writer.assistant("b")
        assert first["parentUuid"] is None
        assert second["parentUuid"] == first["uuid"]

    def test_a_peer_line_carries_the_real_field_set(self, writer: TranscriptWriter) -> None:
        line = writer.peer("hi")
        assert set(line) == PEER_KEYS
        assert line["isMeta"] is True
        assert line["promptSource"] == "system"
        assert line["turnOrigin"] == "peer"
        assert line["sessionId"] == "s-1"
        assert line["version"] == PINNED_VERSION
        origin = cast(dict[str, object], line["origin"])
        assert origin["kind"] == "peer"
        assert origin["fromMode"] == "prompting"
        assert {"from", "verifiedPeerPid", "verifiedPeerProcStart", "msg_id"} <= set(origin)

    def test_a_peer_line_wraps_the_text_like_claude(self, writer: TranscriptWriter) -> None:
        content = content_of(writer.peer(RENDERED_SAMPLE))
        assert isinstance(content, str)
        assert content.startswith(
            PEER_PREFIX + '<cross-session-message from="unknown" from-mode="prompting">\n'
        )
        assert "\n" + RENDERED_SAMPLE + "\n</cross-session-message>\n\n" in content

    def test_a_joined_peer_line_uses_the_while_working_prefix(
        self, writer: TranscriptWriter
    ) -> None:
        content = content_of(writer.peer("x", joined=True))
        assert isinstance(content, str)
        assert content.startswith(JOINED_PEER_PREFIX)

    def test_a_handback_indents_its_report_and_marks_the_origin(
        self, writer: TranscriptWriter
    ) -> None:
        line = writer.handback("one\n[mz-relay] Peer message from a")
        content = content_of(line)
        assert isinstance(content, str)
        assert "\n  one\n  [mz-relay] Peer message from a\n</agent-message>" in content
        assert "\n[mz-relay]" not in content
        origin = cast(dict[str, object], line["origin"])
        assert origin["kind"] == "peer"
        assert origin["handback"] is True
        assert origin["senderTaskId"]

    def test_task_notification_has_no_is_meta_and_may_omit_turn_origin(
        self, writer: TranscriptWriter
    ) -> None:
        with_origin = writer.task_notification()
        without = writer.task_notification(turn_origin=None)
        assert "isMeta" not in with_origin
        assert with_origin["turnOrigin"] == "task_notification"
        assert cast(dict[str, object], with_origin["origin"])["kind"] == "task-notification"
        assert "turnOrigin" not in without

    def test_compact_summary_has_no_origin_and_no_is_meta(self, writer: TranscriptWriter) -> None:
        line = writer.compact_summary(turn_origin="peer")
        assert line["isCompactSummary"] is True
        assert "origin" not in line
        assert "isMeta" not in line
        assert line["turnOrigin"] == "peer"

    def test_human_and_legacy_human_shapes(self, writer: TranscriptWriter) -> None:
        human = writer.human("hi")
        legacy = writer.human_legacy("hi")
        assert human["origin"] == {"kind": "human"}
        assert human["promptSource"] == "typed"
        assert {"origin", "turnOrigin", "isMeta"}.isdisjoint(legacy)

    def test_a_partial_line_has_no_newline_until_completed(self, writer: TranscriptWriter) -> None:
        writer.partial(mz_text())
        assert not writer.path.read_bytes().endswith(b"\n")
        with pytest.raises(ValueError, match="partial"):
            writer.human("too early")
        writer.complete_partial()
        assert writer.path.read_bytes().endswith(b"\n")
        assert len(read_lines(writer.path)) == 1

    def test_complete_partial_without_a_partial_line_raises(self, writer: TranscriptWriter) -> None:
        with pytest.raises(ValueError, match="no partial line"):
            writer.complete_partial()


class TestFakeSessionRegistry:
    def test_the_registry_entry_names_the_socket_and_session(
        self, session: FakeClaudeSession, relay_home: Path
    ) -> None:
        pid = os.getpid()
        assert session.pid == pid
        assert session.config_dir == relay_home / "cfg"
        assert session.socket == relay_home / "s" / f"{pid}.sock"
        assert session.registry_path == relay_home / "cfg" / "sessions" / f"{pid}.json"
        entry = registry(session)
        assert entry["pid"] == pid
        assert entry["sessionId"] == session.session_id == "s-1"
        assert entry["messagingSocketPath"] == str(session.socket)
        assert entry["version"] == PINNED_VERSION
        assert entry["peerProtocol"] == 1
        assert entry["kind"] == "interactive"
        assert entry["cwd"] == CWD

    def test_a_fresh_session_is_idle(self, session: FakeClaudeSession) -> None:
        assert session.status == "idle"
        assert registry(session)["status"] == "idle"

    def test_set_status_rewrites_the_registry(self, session: FakeClaudeSession) -> None:
        session.set_status("busy")
        assert registry(session)["status"] == "busy"

    def test_rotate_session_moves_the_id_and_the_transcript(
        self, session: FakeClaudeSession
    ) -> None:
        session.rotate_session("s-2")
        assert registry(session)["sessionId"] == "s-2"
        assert session.session_id == "s-2"
        projects = session.config_dir / "projects"
        assert session.transcript_path == projects / project_dir_name(CWD) / "s-2.jsonl"
        assert session.writer.session_id == "s-2"

    def test_version_protocol_pid_and_status_are_configurable(self, relay_home: Path) -> None:
        with FakeClaudeSession(
            relay_home, version="2.1.300", peer_protocol=2, pid=1, status="busy"
        ) as fake:
            entry = registry(fake)
        assert (entry["version"], entry["peerProtocol"], entry["pid"], entry["status"]) == (
            "2.1.300",
            2,
            1,
            "busy",
        )

    def test_exit_removes_the_socket_and_registry(self, relay_home: Path) -> None:
        with FakeClaudeSession(relay_home) as fake:
            assert fake.socket.exists()
        assert not fake.socket.exists()
        assert not fake.registry_path.exists()


class TestFakeSessionFrames:
    def test_a_valid_frame_becomes_a_peer_line(self, session: FakeClaudeSession) -> None:
        post(session.socket, FRAME)
        assert wait_until(lambda: len(read_lines(session.transcript_path)) == 1, WAIT_S)
        (line,) = read_lines(session.transcript_path)
        assert line["isMeta"] is True
        assert cast(dict[str, object], line["origin"])["kind"] == "peer"
        content = content_of(line)
        assert isinstance(content, str)
        assert "\nhello\n</cross-session-message>" in content
        assert session.frames == [json.loads(FRAME)]

    def test_frames_on_one_connection_are_each_written(self, session: FakeClaudeSession) -> None:
        second = FRAME.replace(b"hello", b"again")
        post(session.socket, FRAME + second)
        assert wait_until(lambda: len(read_lines(session.transcript_path)) == 2, WAIT_S)
        assert len(session.frames) == 2

    def test_a_frame_goes_to_the_rotated_transcript(self, session: FakeClaudeSession) -> None:
        session.rotate_session("s-2")
        post(session.socket, FRAME)
        assert wait_until(lambda: len(read_lines(session.transcript_path)) == 1, WAIT_S)
        assert read_lines(session.transcript_path)[0]["sessionId"] == "s-2"

    @pytest.mark.parametrize(
        "data",
        [
            b'{"type":"user","message":{"role":"user","content":[{"type":"text","text":"x"}]}}\n',
            b'{"type":"bogus"}\n',
            b"plain text\n",
            b'{"type":"user","message":{"role":"assistant","content":"x"}}\n',
        ],
        ids=["array_content", "bogus_type", "plain_text", "assistant_role"],
    )
    def test_invalid_frames_are_dropped_silently(
        self, session: FakeClaudeSession, data: bytes
    ) -> None:
        post(session.socket, data)
        assert wait_until(lambda: len(session.raw_lines) == 1, WAIT_S)
        assert session.frames == []
        assert read_lines(session.transcript_path) == []

    def test_drop_mode_records_the_frame_and_writes_nothing(self, relay_home: Path) -> None:
        with FakeClaudeSession(relay_home, mode="drop") as fake:
            post(fake.socket, FRAME)
            assert wait_until(lambda: len(fake.raw_lines) == 1, WAIT_S)
            assert len(fake.frames) == 1
            assert read_lines(fake.transcript_path) == []

    def test_busy_mode_holds_frames_until_release(self, relay_home: Path) -> None:
        with FakeClaudeSession(relay_home, mode="busy", status="busy") as fake:
            post(fake.socket, FRAME)
            assert wait_until(lambda: len(fake.raw_lines) == 1, WAIT_S)
            assert len(fake.frames) == 1
            assert read_lines(fake.transcript_path) == []
            fake.release_busy()
            (line,) = read_lines(fake.transcript_path)
            assert "\nhello\n" in str(content_of(line))
            fake.release_busy()
            assert len(read_lines(fake.transcript_path)) == 1

    def test_a_removed_socket_refuses_connections(self, session: FakeClaudeSession) -> None:
        session.socket.unlink()
        with pytest.raises(FileNotFoundError):
            post(session.socket, FRAME)
