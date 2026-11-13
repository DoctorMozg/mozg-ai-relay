"""Claude delivery: a frame to the session's inbox socket, confirmed by its transcript line."""

from __future__ import annotations

import logging
import math
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mz_relay.adapters.claude_registry import (
    MIN_CLAUDE_VERSION,
    PEER_PROTOCOL,
    ClaudeRegistry,
    RegistryEntry,
    build_frame,
    pid_alive,
    version_supported,
)
from mz_relay.adapters.claude_transcript import TranscriptWatcher
from mz_relay.clock import Clock
from mz_relay.delivery import DeliveryBinding, DeliveryJob, DeliveryResult, Outcome
from mz_relay.envelope import Status
from mz_relay.errors import BindingError
from mz_relay.roster import AgentSpec, ToolMode

log = logging.getLogger(__name__)

NATIVE = "native"
FALLBACK = "fallback"
BUSY_DETAIL = "recipient busy; read_only deliveries wait for idle"
# Claude never answers on its inbox socket, so this only bounds connect and sendall.
POST_TIMEOUT_S = 5.0
# A mark unasked this long past the grace belongs to a row acked or expired while handed off.
MARK_MARGIN_S = 600.0


@dataclass(frozen=True)
class ClaudeTiming:
    confirm_s: float = 5.0
    poll_s: float = 0.1
    busy_grace_s: float = 600.0

    def __post_init__(self) -> None:
        for name in ("confirm_s", "poll_s", "busy_grace_s"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")


DEFAULT_TIMING = ClaudeTiming()


@dataclass(frozen=True)
class _Native:
    """The fields of a native binding the adapter posts through."""

    pid: int
    messaging_socket: Path
    config_dir: Path


@dataclass(frozen=True)
class _Mark:
    """Where a handed-off post's transcript line must appear: that session, past that offset."""

    config_dir: Path
    session_id: str
    offset: int
    created_at: float


class ClaudeAdapter:
    """Delivers to Claude Code sessions; a post that never shows up falls back to the waiter."""

    exhausted_status: Status = Status.FALLBACK
    via: str = "claude"

    def __init__(self, *, clock: Clock, timing: ClaudeTiming = DEFAULT_TIMING) -> None:
        if not isinstance(timing, ClaudeTiming):
            raise ValueError(f"timing must be a ClaudeTiming, got {type(timing).__name__}")
        self._clock = clock
        self._timing = timing
        # Keyed by (source, id); a confirm after a daemon restart reads the whole transcript.
        self._marks: dict[tuple[str, str], _Mark] = {}

    @property
    def pending_marks(self) -> int:
        return len(self._marks)

    def validate_binding(
        self, spec: AgentSpec, binding: Mapping[str, object] | None
    ) -> DeliveryBinding | None:
        if binding is None:
            return None
        if not isinstance(binding, Mapping):
            raise BindingError(f"claude delivery must be an object, got {type(binding).__name__}")
        config_dir = _config_dir(spec, binding)
        if "messaging_socket" in binding:
            native = _native(binding)
            entry = ClaudeRegistry(config_dir).read_settled(native.pid, clock=self._clock)
            if entry is None:
                raise BindingError(
                    f"no Claude registry entry for pid {native.pid} under {config_dir}"
                )
            if entry.messaging_socket != native.messaging_socket:
                raise BindingError(
                    f"messaging socket {native.messaging_socket} is not the one registered "
                    f"for pid {native.pid} ({entry.messaging_socket})"
                )
            if not version_supported(entry.version, entry.peer_protocol):
                minimum = ".".join(str(part) for part in MIN_CLAUDE_VERSION)
                log.warning(
                    "Claude %s (peer protocol %d) at pid %d is not verified for native posts "
                    "(need >= %s, protocol %d); %s gets messages through the waiter",
                    entry.version,
                    entry.peer_protocol,
                    native.pid,
                    minimum,
                    PEER_PROTOCOL,
                    spec.name,
                )
                return DeliveryBinding(FALLBACK, dict(binding))
            return DeliveryBinding(NATIVE, dict(binding))
        if binding.get("rewake") is True:
            _session_id(binding)
            return DeliveryBinding(FALLBACK, dict(binding))
        raise BindingError("claude delivery needs messaging_socket or rewake: true")

    def deliver(self, job: DeliveryJob) -> DeliveryResult:
        env = job.message.envelope
        if job.binding.mode != NATIVE:
            log.info("%s to %s goes to the waiter: no native inbox", env.id, env.to)
            return DeliveryResult(Outcome.FALLBACK, "no native inbox; the waiter delivers")
        native = _native(job.binding.data)
        entry, gone = self._live_entry(native)
        if entry is None:
            log.info("%s to %s unreachable: %s", env.id, env.to, gone)
            return DeliveryResult(Outcome.UNREACHABLE, gone)
        if job.spec.tools is ToolMode.READ_ONLY and entry.is_busy:
            # The engine logs the deferral once per row at INFO; this line repeats every DEFER_S.
            log.debug("%s to %s deferred: read_only recipient is busy", env.id, env.to)
            return DeliveryResult(Outcome.DEFERRED, BUSY_DETAIL)
        # The current session id, since /clear rotates it under the same pid and socket.
        watcher = TranscriptWatcher(native.config_dir, entry.session_id)
        mark = _Mark(native.config_dir, entry.session_id, watcher.size(), self._clock.now())
        try:
            self._post(native.messaging_socket, job.text)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            detail = f"inbox socket {native.messaging_socket} refused the post: {exc.strerror}"
            log.info("%s to %s unreachable: %s", env.id, env.to, detail)
            return DeliveryResult(Outcome.UNREACHABLE, detail)
        except OSError as exc:
            detail = f"post to {native.messaging_socket} failed: {type(exc).__name__}"
            log.info("%s to %s not posted: %s", env.id, env.to, detail)
            return DeliveryResult(Outcome.NOT_CONFIRMED, detail)
        if self._await_line(watcher, env.id, mark.offset):
            log.info("%s to %s confirmed in session %s", env.id, env.to, entry.session_id)
            return DeliveryResult(Outcome.CONFIRMED)
        current = ClaudeRegistry(native.config_dir).read_settled(native.pid, clock=self._clock)
        if current is not None and current.is_busy:
            self._marks[(env.source, env.id)] = mark
            log.info("%s to %s handed off: session %s is busy", env.id, env.to, entry.session_id)
            return DeliveryResult(Outcome.HANDED_OFF)
        detail = f"no transcript line within {self._timing.confirm_s} s"
        log.info("%s to %s not confirmed: %s", env.id, env.to, detail)
        return DeliveryResult(Outcome.NOT_CONFIRMED, detail)

    def confirm(self, job: DeliveryJob) -> DeliveryResult:
        env = job.message.envelope
        self._prune_marks()
        result = self._confirm(job)
        if result.outcome is not Outcome.HANDED_OFF:
            self._marks.pop((env.source, env.id), None)
        return result

    def _prune_marks(self) -> None:
        cutoff = self._clock.now() - (self._timing.busy_grace_s + MARK_MARGIN_S)
        stale = [key for key, mark in self._marks.items() if mark.created_at < cutoff]
        for key in stale:
            del self._marks[key]
        if stale:
            log.info("dropped %d hand-off marks no confirm asked about", len(stale))

    def _confirm(self, job: DeliveryJob) -> DeliveryResult:
        env = job.message.envelope
        mark = self._marks.get((env.source, env.id))
        if mark is not None and _line_seen(mark, env.id):
            log.info("%s to %s confirmed in session %s", env.id, env.to, mark.session_id)
            return DeliveryResult(Outcome.CONFIRMED)
        if job.binding.mode != NATIVE:
            log.info("%s to %s now goes to the waiter: no native inbox", env.id, env.to)
            return DeliveryResult(Outcome.FALLBACK, "no native inbox; the waiter delivers")
        native = _native(job.binding.data)
        entry, gone = self._live_entry(native)
        if entry is None:
            log.info("%s to %s unreachable while handed off: %s", env.id, env.to, gone)
            return DeliveryResult(Outcome.UNREACHABLE, gone)
        # No mark after a daemon restart; another session id after /clear, which may hold the line.
        if mark is None or mark.session_id != entry.session_id:
            current = _Mark(native.config_dir, entry.session_id, 0, self._clock.now())
            if _line_seen(current, env.id):
                log.info("%s to %s confirmed in session %s", env.id, env.to, current.session_id)
                return DeliveryResult(Outcome.CONFIRMED)
        if entry.is_busy:
            return DeliveryResult(Outcome.HANDED_OFF)
        handed_off_at = job.message.handed_off_at
        grace = self._timing.busy_grace_s
        if handed_off_at is not None and job.now - handed_off_at < grace:
            return DeliveryResult(Outcome.HANDED_OFF)
        detail = f"no transcript line within {grace} s of the hand-off"
        log.info("%s to %s not confirmed: %s", env.id, env.to, detail)
        return DeliveryResult(Outcome.NOT_CONFIRMED, detail)

    def _live_entry(self, native: _Native) -> tuple[RegistryEntry | None, str]:
        """The session's registry entry, or None and why the session is gone."""
        if not pid_alive(native.pid):
            return None, f"Claude session pid {native.pid} is not running"
        entry = ClaudeRegistry(native.config_dir).read_settled(native.pid, clock=self._clock)
        if entry is None:
            return None, f"no Claude registry entry for pid {native.pid}"
        if entry.messaging_socket != native.messaging_socket:
            return None, f"Claude pid {native.pid} now listens on {entry.messaging_socket}"
        return entry, ""

    def _post(self, path: Path, text: str) -> None:
        frame = build_frame(text)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(POST_TIMEOUT_S)
            conn.connect(str(path))
            conn.sendall(frame)

    def _await_line(self, watcher: TranscriptWatcher, nonce: str, since: int) -> bool:
        deadline = self._clock.now() + self._timing.confirm_s
        while True:
            if watcher.has_peer_nonce(nonce, since=since):
                return True
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                return False
            self._clock.sleep(min(self._timing.poll_s, remaining))


def _line_seen(mark: _Mark, nonce: str) -> bool:
    watcher = TranscriptWatcher(mark.config_dir, mark.session_id)
    return watcher.has_peer_nonce(nonce, since=mark.offset)


def _config_dir(spec: AgentSpec, binding: Mapping[str, object]) -> Path:
    raw = binding.get("config_dir")
    if not isinstance(raw, str) or not raw:
        raise BindingError("claude delivery needs config_dir as a non-empty string")
    if spec.claude_config_dir is None:
        raise BindingError(f"{spec.name} has no claude_config_dir in the roster")
    config_dir = Path(raw)
    if config_dir.resolve() != spec.claude_config_dir.resolve():
        raise BindingError(
            f"config dir {raw} is not {spec.name}'s roster config dir {spec.claude_config_dir}"
        )
    return config_dir


def _session_id(binding: Mapping[str, object]) -> str:
    session_id = binding.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise BindingError("claude rewake delivery needs session_id as a non-empty string")
    return session_id


def _native(data: Mapping[str, object]) -> _Native:
    sock = data.get("messaging_socket")
    pid = data.get("pid")
    config_dir = data.get("config_dir")
    if not isinstance(sock, str) or not sock:
        raise BindingError("claude delivery needs messaging_socket as a non-empty string")
    # bool is an int subclass; true/false is not a pid.
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise BindingError(f"claude delivery needs pid as a positive integer, got {pid!r}")
    if not isinstance(config_dir, str) or not config_dir:
        raise BindingError("claude delivery needs config_dir as a non-empty string")
    return _Native(pid=pid, messaging_socket=Path(sock), config_dir=Path(config_dir))
