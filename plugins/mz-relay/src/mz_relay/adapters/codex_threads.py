"""Codex threads: the newest thread with a /rename name, and its rollout file under sessions/."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from mz_relay.errors import RelayError

log = logging.getLogger(__name__)

SESSION_INDEX = "session_index.jsonl"
# Codex writes nanoseconds, which datetime cuts to microseconds, so the fraction is kept apart.
_UPDATED_AT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})"
)
_ROLLOUT_STAMP = r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"


class CodexThreadMissing(RelayError):
    CODES: ClassVar[frozenset[str]] = frozenset({"codex_thread_missing"})

    def __init__(self, codex_home: Path, name: str) -> None:
        super().__init__(
            "codex_thread_missing",
            f"no Codex thread named {name} in {codex_home / SESSION_INDEX}; "
            f"run /rename {name} in the Codex TUI",
        )


@dataclass(frozen=True)
class CodexThread:
    id: str
    name: str
    updated_at: str


@dataclass(frozen=True)
class _Candidate:
    thread: CodexThread
    # (whole seconds as an aware datetime, nanoseconds), so tuples order as instants.
    instant: tuple[datetime, int]


def _instant(updated_at: str) -> tuple[datetime, int] | None:
    """A sort key that orders updated_at values as instants; None when it is not a zoned time."""
    match = _UPDATED_AT_RE.fullmatch(updated_at)
    if match is None:
        return None
    stamp, fraction, zone = match.groups()
    try:
        whole = datetime.fromisoformat(stamp + zone)
    except ValueError:
        return None
    return whole, int((fraction or "").ljust(9, "0"))


def _candidate(line: str, name: str) -> _Candidate | None:
    try:
        raw = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("thread_name") != name:
        return None
    thread_id, updated_at = raw.get("id"), raw.get("updated_at")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(updated_at, str):
        return None
    instant = _instant(updated_at)
    if instant is None:
        return None
    return _Candidate(CodexThread(id=thread_id, name=name, updated_at=updated_at), instant)


def resolve_thread(codex_home: Path, name: str) -> CodexThread:
    """The newest thread named `name` in <codex_home>/session_index.jsonl."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"name must be a non-empty string, got {name!r}")
    index = codex_home / SESSION_INDEX
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log.info("no Codex session index at %s", index)
        raise CodexThreadMissing(codex_home, name) from None
    matches = [found for line in text.splitlines() if (found := _candidate(line, name))]
    if not matches:
        raise CodexThreadMissing(codex_home, name)
    # Codex appends to the index, so of equal instants the later line is the later write.
    _, newest = max(enumerate(matches), key=lambda pair: (pair[1].instant, pair[0]))
    thread = newest.thread
    if len(matches) > 1:
        log.warning("%d Codex threads named %s; using %s", len(matches), name, thread.id)
    return thread


def find_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """The newest sessions/<yyyy>/<mm>/<dd>/rollout-<stamp>-<thread_id>.jsonl, or None."""
    if not isinstance(thread_id, str) or not thread_id or "/" in thread_id:
        raise ValueError(f"thread_id must be a non-empty string without /, got {thread_id!r}")
    # The full name is matched, so thread t-new does not pick up a rollout of x-t-new.
    name_re = re.compile(_ROLLOUT_STAMP + re.escape(thread_id) + r"\.jsonl")
    newest: tuple[float, Path] | None = None
    for path in (codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"):
        if name_re.fullmatch(path.name) is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return None if newest is None else newest[1]


def rollout_has_nonce(path: Path, nonce: str) -> bool:
    """Whether a line of the rollout holds `nonce` as raw text; a missing rollout holds none."""
    if not isinstance(nonce, str) or not nonce or "\n" in nonce:
        raise ValueError(f"nonce must be a non-empty single-line string, got {nonce!r}")
    needle = nonce.encode("utf-8")
    try:
        with path.open("rb") as handle:
            return any(needle in line for line in handle)
    except FileNotFoundError:
        return False


@dataclass(frozen=True)
class RolloutScan:
    found: bool
    # Where the next scan starts: past the last complete line read.
    offset: int


def scan_rollout(path: Path, nonce: str, offset: int) -> RolloutScan:
    """Whether a complete line after `offset` holds `nonce`, and the offset past the lines read.
    A partial last line waits for the next scan; a rollout shorter than `offset` is read anew."""
    if not isinstance(nonce, str) or not nonce or "\n" in nonce:
        raise ValueError(f"nonce must be a non-empty single-line string, got {nonce!r}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(f"offset must be a non-negative integer, got {offset!r}")
    needle = nonce.encode("utf-8")
    try:
        with path.open("rb") as handle:
            if offset > os.fstat(handle.fileno()).st_size:
                log.info("Codex rollout %s shrank below offset %d; reading it anew", path, offset)
                offset = 0
            handle.seek(offset)
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                offset += len(line)
                if needle in line:
                    return RolloutScan(found=True, offset=offset)
    except FileNotFoundError:
        return RolloutScan(found=False, offset=0)
    return RolloutScan(found=False, offset=offset)
