"""Finds the Claude session an MCP child runs in; reads Claude's per-config-dir session registry."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from mz_relay.clock import Clock
from mz_relay.errors import PathError, RelayError

log = logging.getLogger(__name__)

MIN_CLAUDE_VERSION = (2, 1, 322)
PEER_PROTOCOL = 1
IDLE = "idle"
# Claude rewrites sessions/<pid>.json in place, so a reader can catch it missing or torn once.
SETTLE_S = 0.5

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)", re.ASCII)


class NotInClaudeSession(RelayError):
    CODES: ClassVar[frozenset[str]] = frozenset({"not_in_claude_session"})

    def __init__(self, message: str, data: Mapping[str, object] | None = None) -> None:
        super().__init__("not_in_claude_session", message, data)


@dataclass(frozen=True)
class OwnSession:
    pid: int
    messaging_socket: Path
    config_dir: Path
    session_id: str | None


@dataclass(frozen=True)
class RegistryEntry:
    pid: int
    session_id: str
    cwd: Path
    messaging_socket: Path
    version: str
    peer_protocol: int
    status: str

    @property
    def is_idle(self) -> bool:
        return self.status == IDLE

    @property
    def is_busy(self) -> bool:
        # An unknown status counts as busy, so read_only deliveries never land mid-turn.
        return not self.is_idle


def claude_config_dir(env: Mapping[str, str]) -> Path:
    """CLAUDE_CONFIG_DIR, else .claude under Claude's own HOME, which may not be the passwd home."""
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise PathError(f"CLAUDE_CONFIG_DIR must be an absolute path, got {configured!r}")
        return path
    home = env.get("HOME")
    if not home:
        raise PathError(
            "neither CLAUDE_CONFIG_DIR nor HOME is set; cannot locate the Claude config dir"
        )
    return Path(home) / ".claude"


def discover_own_session(env: Mapping[str, str], *, ppid: int) -> OwnSession:
    """The Claude session whose MCP child this process is."""
    socket = env.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not socket:
        raise NotInClaudeSession(
            "CLAUDE_CODE_MESSAGING_SOCKET is not set; not running inside a Claude Code session"
        )
    if ppid <= 0:
        raise NotInClaudeSession(f"parent pid must be positive, got {ppid}")
    # The parent pid, not CLAUDE_PID: that leaks in from an outer session when Claude runs nested.
    own = OwnSession(
        pid=ppid,
        messaging_socket=Path(socket),
        config_dir=claude_config_dir(env),
        session_id=env.get("CLAUDE_CODE_SESSION_ID") or None,
    )
    log.info("running in Claude session pid %d, config dir %s", own.pid, own.config_dir)
    return own


class ClaudeRegistry:
    """Claude Code's `sessions/<pid>.json` files under one config dir."""

    def __init__(self, config_dir: Path) -> None:
        self.sessions_dir = config_dir / "sessions"

    def read(self, pid: int) -> RegistryEntry | None:
        if pid <= 0:
            raise ValueError(f"pid must be positive, got {pid}")
        path = self.sessions_dir / f"{pid}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError):
            return None
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            log.warning("malformed Claude registry entry %s: %s", path, exc)
            return None
        entry = _parse_entry(data)
        if entry is None:
            log.warning(
                "Claude registry entry %s lacks a required key or has a wrongly typed value", path
            )
            return None
        if entry.pid != pid:
            log.warning("Claude registry entry %s names pid %d", path, entry.pid)
            return None
        return entry

    def read_settled(self, pid: int, *, clock: Clock) -> RegistryEntry | None:
        """read(pid), read once more after a short pause when it misses while the pid lives."""
        entry = self.read(pid)
        if entry is not None or not pid_alive(pid):
            return entry
        log.debug("no Claude registry entry for live pid %d; reading again", pid)
        clock.sleep(SETTLE_S)
        return self.read(pid)

    def find_session(self, session_id: str) -> RegistryEntry | None:
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        matches: list[RegistryEntry] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            if not (path.stem.isascii() and path.stem.isdigit()) or int(path.stem) <= 0:
                continue
            entry = self.read(int(path.stem))
            if entry is not None and entry.session_id == session_id:
                matches.append(entry)
        if not matches:
            return None
        if len(matches) > 1:
            pids = ", ".join(str(m.pid) for m in matches)
            log.warning(
                "session %s is registered by several pids (%s); using %d",
                session_id,
                pids,
                matches[0].pid,
            )
        return matches[0]


def _parse_entry(data: object) -> RegistryEntry | None:
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    peer_protocol = data.get("peerProtocol")
    session_id = data.get("sessionId")
    cwd = data.get("cwd")
    socket = data.get("messagingSocketPath")
    version = data.get("version")
    status = data.get("status")
    # bool is an int subclass; a true/false pid or protocol is malformed.
    if isinstance(pid, bool) or isinstance(peer_protocol, bool):
        return None
    if not (
        isinstance(pid, int)
        and isinstance(peer_protocol, int)
        and isinstance(session_id, str)
        and isinstance(cwd, str)
        and isinstance(socket, str)
        and isinstance(version, str)
        and isinstance(status, str)
    ):
        return None
    return RegistryEntry(
        pid=pid,
        session_id=session_id,
        cwd=Path(cwd),
        messaging_socket=Path(socket),
        version=version,
        peer_protocol=peer_protocol,
        status=status,
    )


def version_supported(version: str, peer_protocol: int) -> bool:
    """Whether a Claude build speaks the inbox frame mz-relay was verified against."""
    if peer_protocol != PEER_PROTOCOL:
        return False
    match = _VERSION.fullmatch(version)
    if match is None:
        return False
    parsed = (int(match[1]), int(match[2]), int(match[3]))
    return parsed >= MIN_CLAUDE_VERSION


def pid_alive(pid: int) -> bool:
    # kill(0, 0) and negative pids signal process groups, which would always look alive.
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        # Larger than a pid_t can hold, so no process has it.
        return False
    except PermissionError:
        return True
    return True


def build_frame(text: str) -> bytes:
    """One inbox line for Claude's messaging socket; Claude takes only str content as a turn."""
    if not text:
        raise ValueError("frame text must be a non-empty string")
    frame = {"type": "user", "message": {"role": "user", "content": text}}
    return json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
