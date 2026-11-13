"""Claude transcript lines: which prompt started the current turn, and reading a session's tail."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from mz_relay.envelope import HEADER_PREFIX

log = logging.getLogger(__name__)

# The first line render_for_delivery emits; peer lines are matched on it.
MZ_HEADER_PREFIX = HEADER_PREFIX
JOINED_PREFIX = "Another Claude session sent a message while you were working:\n"
DEFAULT_TAIL_BYTES = 262_144
# Written after a turn's last assistant line, before the next prompt (CC 2.1.3xx transcripts).
TURN_END_SUBTYPES = frozenset({"stop_hook_summary", "turn_duration"})


class LineKind(StrEnum):
    MZ_PEER = "mz_peer"
    OTHER_PEER = "other_peer"
    HUMAN = "human"
    SKIP = "skip"


@dataclass(frozen=True)
class Classified:
    kind: LineKind
    joined: bool
    content: str


def _is_tool_result(block: object) -> bool:
    return isinstance(block, Mapping) and block.get("type") == "tool_result"


def _prompt_text(content: object) -> str | None:
    """The prompt text, or None when the content holds no prompt (no blocks, only tool results)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or all(_is_tool_result(block) for block in content):
        return None
    texts = [
        block["text"]
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(texts)


def _peer_kind(origin: Mapping[str, object], text: str) -> LineKind:
    # Subagent hand-backs indent their report, but they are never relayd's frame.
    if origin.get("senderTaskId") or origin.get("handback") is True:
        return LineKind.OTHER_PEER
    if any(part.startswith(MZ_HEADER_PREFIX) for part in text.split("\n")):
        return LineKind.MZ_PEER
    return LineKind.OTHER_PEER


def classify(line: Mapping[str, object]) -> Classified:
    """Classifies one transcript line; the checks run in order and the first match wins."""
    if not isinstance(line, Mapping):
        raise ValueError(f"line must be a mapping, got {type(line).__name__}")
    skip = Classified(LineKind.SKIP, False, "")
    message = line.get("message")
    if line.get("type") != "user" or not isinstance(message, Mapping):
        return skip
    text = _prompt_text(message.get("content"))
    if text is None:
        return skip
    joined = text.startswith(JOINED_PREFIX)
    origin = line.get("origin")
    kind = origin.get("kind") if isinstance(origin, Mapping) else None
    if isinstance(origin, Mapping) and kind == "peer":
        return Classified(_peer_kind(origin, text), joined, text)
    if line.get("isCompactSummary") is True:
        return skip
    if kind == "human":
        return Classified(LineKind.HUMAN, joined, text)
    if origin is not None:
        return skip
    if line.get("isMeta") is True:
        return skip
    # Origin-less SDK prompts (turnOrigin "sdk") stay SKIP; a Stop between turns lifts the gate.
    if line.get("turnOrigin", "human") == "human":
        return Classified(LineKind.HUMAN, joined, text)
    return skip


def peer_turn_active(lines: Sequence[Mapping[str, object]]) -> bool:
    """True when the latest prompt line that started a turn is an mz-relay peer post."""
    return _peer_turn_newest_first(reversed(lines))


def _ends_turn(line: Mapping[str, object]) -> bool:
    """An assistant line, or one of the system lines Claude writes once a turn is over."""
    kind = line.get("type")
    return kind == "assistant" or (kind == "system" and line.get("subtype") in TURN_END_SUBTYPES)


def _peer_turn_newest_first(lines: Iterable[Mapping[str, object]]) -> bool:
    for line in lines:
        classified = classify(line)
        match classified.kind:
            case LineKind.SKIP:
                continue
            case LineKind.MZ_PEER:
                return True
            case LineKind.OTHER_PEER if classified.joined:
                continue
            case _:
                return False
    return False


class Transcript:
    """One transcript file at a known path; a missing file reads as empty."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ValueError(f"path must be a Path, got {type(path).__name__}")
        self.path = path

    def lines_after(
        self, offset: int, *, max_bytes: int = DEFAULT_TAIL_BYTES
    ) -> list[dict[str, object]]:
        """Complete JSON objects from `offset`, within the last `max_bytes`; bad lines skipped."""
        if offset < 0:
            raise ValueError(f"offset must be at least 0, got {offset}")
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")
        try:
            with self.path.open("rb") as transcript:
                data = _read_tail(transcript, offset, max_bytes)
        except FileNotFoundError:
            return []
        lines: list[dict[str, object]] = []
        skipped = 0
        # The last piece is empty or a line still being written; either way it is not read.
        for raw in data.split(b"\n")[:-1]:
            if not raw.strip():
                continue
            parsed = _parse_line(raw)
            if parsed is None:
                skipped += 1
            else:
                lines.append(parsed)
        if skipped:
            log.debug("skipped %d malformed lines in %s", skipped, self.path)
        return lines

    def lines_back(
        self, offset: int, *, chunk_bytes: int = DEFAULT_TAIL_BYTES
    ) -> Iterator[tuple[int, dict[str, object]]]:
        """Complete JSON objects after `offset`, newest first, each with the offset just past it;
        read `chunk_bytes` at a time from the end, so stopping early reads only the tail."""
        if offset < 0:
            raise ValueError(f"offset must be at least 0, got {offset}")
        if chunk_bytes <= 0:
            raise ValueError(f"chunk_bytes must be positive, got {chunk_bytes}")
        try:
            transcript = self.path.open("rb")
        except FileNotFoundError:
            return
        with transcript:
            yield from _lines_back(transcript, offset, chunk_bytes)

    def peer_turn_since(self, offset: int) -> bool:
        """peer_turn_active over the lines after `offset`, reading back past the tail as needed."""
        return _peer_turn_newest_first(line for _, line in self.lines_back(offset))

    def human_since(self, offset: int) -> bool:
        return any(classify(line).kind is LineKind.HUMAN for _, line in self.lines_back(offset))

    def assistant_since(self, offset: int) -> bool:
        return any(line.get("type") == "assistant" for _, line in self.lines_back(offset))

    def turn_end(self) -> int | None:
        """The offset just past the last line that ends a turn; None before any turn has ended."""
        return next((end for end, line in self.lines_back(0) if _ends_turn(line)), None)


class TranscriptWatcher:
    """Reads the transcript of one Claude session under `<config_dir>/projects/*/`."""

    def __init__(self, config_dir: Path, session_id: str) -> None:
        if not isinstance(config_dir, Path):
            raise ValueError(f"config_dir must be a Path, got {type(config_dir).__name__}")
        if not session_id or "/" in session_id or "\0" in session_id or session_id in {".", ".."}:
            raise ValueError(f"session_id must be a plain file name stem, got {session_id!r}")
        self.config_dir = config_dir
        self.session_id = session_id

    def path(self) -> Path | None:
        """The first `projects/<dir>/<session_id>.jsonl`, in name order; None when there is none."""
        projects = self.config_dir / "projects"
        if not projects.is_dir():
            return None
        name = f"{self.session_id}.jsonl"
        for project in sorted(projects.iterdir()):
            candidate = project / name
            if candidate.is_file():
                return candidate
        return None

    def transcript(self) -> Transcript | None:
        path = self.path()
        return None if path is None else Transcript(path)

    def size(self) -> int:
        path = self.path()
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def lines_after(
        self, offset: int, *, max_bytes: int = DEFAULT_TAIL_BYTES
    ) -> list[dict[str, object]]:
        """Complete JSON objects from `offset`, within the last `max_bytes`; bad lines skipped."""
        transcript = self.transcript()
        return [] if transcript is None else transcript.lines_after(offset, max_bytes=max_bytes)

    def has_peer_nonce(self, nonce: str, *, since: int) -> bool:
        """True once a complete mz-relay peer line after `since` contains `nonce`."""
        if not nonce:
            raise ValueError("nonce must be a non-empty string")
        for line in self.lines_after(since):
            classified = classify(line)
            if classified.kind is LineKind.MZ_PEER and nonce in classified.content:
                return True
        return False


def _parse_line(raw: bytes) -> dict[str, object] | None:
    """One JSON object line; None for a blank, malformed or non-object line."""
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_tail(transcript: BinaryIO, offset: int, max_bytes: int) -> bytes:
    end = os.fstat(transcript.fileno()).st_size
    window = end - max_bytes
    if offset >= window:
        if offset >= end:
            return b""
        transcript.seek(offset)
        return transcript.read()
    log.debug("transcript tail cut to the last %d bytes, past offset %d", max_bytes, offset)
    # Start one byte early so a window that opens exactly on a line start keeps that line.
    transcript.seek(window - 1)
    data = transcript.read()
    newline = data.find(b"\n")
    return b"" if newline < 0 else data[newline + 1 :]


def _lines_back(
    transcript: BinaryIO, offset: int, chunk_bytes: int
) -> Iterator[tuple[int, dict[str, object]]]:
    pos = os.fstat(transcript.fileno()).st_size
    # The head of the previous chunk: a line that started in this chunk or earlier.
    carry = b""
    # Bytes after the last newline belong to a line still being written; they are never read.
    unfinished = True
    while pos > offset:
        start = max(offset, pos - chunk_bytes)
        transcript.seek(start)
        data = transcript.read(pos - start) + carry
        pos = start
        pieces = data.split(b"\n")
        if unfinished:
            if len(pieces) == 1:
                continue
            pieces.pop()
            unfinished = False
        cursor = start
        carry = b""
        if start > offset:
            carry = pieces.pop(0)
            cursor += len(carry) + 1
        ends: list[tuple[int, bytes]] = []
        for raw in pieces:
            cursor += len(raw) + 1
            ends.append((cursor, raw))
        for end, raw in reversed(ends):
            parsed = _parse_line(raw)
            if parsed is not None:
                yield end, parsed
