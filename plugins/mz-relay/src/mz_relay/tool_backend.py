"""The MCP tools backed by relayd, the fallback when registration fails, and `mz-relay mcp`."""

from __future__ import annotations

import io
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType
from typing import TextIO

from mz_relay import __version__, ids
from mz_relay.clock import SystemClock
from mz_relay.errors import DaemonUnavailable, EnvelopeError, ProtocolError, RelayError
from mz_relay.launcher import DaemonLauncher
from mz_relay.mcp_server import INBOX_LIMIT_MAX, INBOX_LIMIT_MIN, McpServer, SendArgs, ToolBackend
from mz_relay.paths import Paths, user_home
from mz_relay.presence import PresenceSession, RegisterArgs
from mz_relay.roster import Harness

log = logging.getLogger(__name__)

MCP_HARNESSES = (Harness.CLAUDE, Harness.CODEX, Harness.OPENCODE)
NAME_VARIABLE = "MZ_RELAY_NAME"
LOG_FORMAT = "%(asctime)s mz-relay-mcp[%(process)d] %(levelname)s %(name)s: %(message)s"


class RelayToolBackend:
    """Each tool call opens its own relayd connection and speaks with the presence token."""

    def __init__(
        self,
        presence: PresenceSession,
        launcher: DaemonLauncher,
        *,
        new_id: Callable[[], str] = ids.new_id,
    ) -> None:
        self._presence = presence
        self._launcher = launcher
        self._new_id = new_id

    def instructions(self) -> str:
        return self._presence.current().brief

    def send(self, args: SendArgs) -> str:
        # Drawn once, so a retry after a lost connection dedupes instead of sending twice.
        wire: dict[str, object] = {
            "id": self._new_id(),
            "to": args.to,
            "kind": args.kind.value,
            "body": args.body,
        }
        if args.context_id is not None:
            wire["context_id"] = args.context_id
        if args.in_reply_to is not None:
            wire["in_reply_to"] = args.in_reply_to
        return self._call("send", wire)

    def inbox(self, limit: int) -> str:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise EnvelopeError(f"inbox limit must be an integer, got {limit!r}")
        if not INBOX_LIMIT_MIN <= limit <= INBOX_LIMIT_MAX:
            raise EnvelopeError(
                f"inbox limit must be {INBOX_LIMIT_MIN}..{INBOX_LIMIT_MAX}, got {limit}"
            )
        return self._call("inbox", {"limit": limit})

    def ack(self, msg_id: str) -> str:
        if not isinstance(msg_id, str) or not msg_id:
            raise EnvelopeError(f"ack needs a message id, got {msg_id!r}")
        return self._call("ack", {"id": msg_id})

    def peers(self) -> str:
        return self._call("peers", {})

    def _call(self, op: str, args: Mapping[str, object]) -> str:
        try:
            return self._attempt(op, args)
        except DaemonUnavailable as exc:
            log.info("relayd unavailable during %s (%s); retrying once", op, exc.message)
        try:
            return self._attempt(op, args)
        except DaemonUnavailable as exc:
            log.warning("relayd still unavailable during %s: %s", op, exc.message)
            raise

    def _attempt(self, op: str, args: Mapping[str, object]) -> str:
        relay = self._launcher.connect(client="mcp")
        try:
            result = relay.call(op, {**args, "token": self._presence.current().token})
        finally:
            relay.close()
        text = result.get("text")
        if not isinstance(text, str):
            raise ProtocolError("invalid_request", f"relayd's {op} result has no text")
        return text


class UnavailableBackend:
    """Keeps the MCP server answering when registration failed, so the agent sees why."""

    def __init__(self, error: RelayError) -> None:
        if not isinstance(error, RelayError):
            raise TypeError(f"error must be a RelayError, got {type(error).__name__}")
        self._code = error.code
        self._text = f"mz-relay is unavailable: {error.code}: {error.message}"

    def instructions(self) -> str:
        return self._text

    def send(self, args: SendArgs) -> str:
        raise self._error()

    def inbox(self, limit: int) -> str:
        raise self._error()

    def ack(self, msg_id: str) -> str:
        raise self._error()

    def peers(self) -> str:
        raise self._error()

    def _error(self) -> RelayError:
        return RelayError(self._code, self._text)


class _Terminated(BaseException):
    """Raised by the SIGTERM handler to unwind the blocking stdin read."""


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    raise _Terminated


def run_mcp(harness: Harness, env: Mapping[str, str]) -> int:
    """Registers this session with relayd and serves the MCP tools on stdin/stdout."""
    if harness not in MCP_HARNESSES:
        allowed = ", ".join(h.value for h in MCP_HARNESSES)
        raise ValueError(f"mcp serves {allowed}, not {harness}")
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    presence: PresenceSession | None = None
    try:
        backend: ToolBackend
        try:
            launcher = _launcher(env)
            presence = PresenceSession(launcher, _register_args(harness, env), clock=SystemClock())
            presence.start()
            backend = RelayToolBackend(presence, launcher)
        except RelayError as exc:
            log.warning("serving without relayd: %s: %s", exc.code, exc.message)
            backend = UnavailableBackend(exc)
        server = McpServer(backend, version=__version__)
        return server.serve(_utf8(sys.stdin), _utf8(sys.stdout))
    except _Terminated:
        log.info("SIGTERM; MCP server stops")
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        if presence is not None:
            presence.close()


def _launcher(env: Mapping[str, str]) -> DaemonLauncher:
    paths = Paths.from_env(env, uid=os.getuid(), pw_home=user_home())
    return DaemonLauncher(paths, clock=SystemClock(), env=env)


def _register_args(harness: Harness, env: Mapping[str, str]) -> RegisterArgs:
    name = _relay_name(env.get(NAME_VARIABLE))
    log.info("registering %s session as %s", harness, name or "<resolved from cwd>")
    return RegisterArgs(name=name, harness=harness, cwd=Path.cwd(), pid=os.getpid(), client="mcp")


def _relay_name(raw: str | None) -> str | None:
    """An empty value or an unsubstituted `${...}` placeholder means no name was configured."""
    if raw is None:
        return None
    name = raw.strip()
    if not name or (name.startswith("${") and name.endswith("}")):
        return None
    return name


def _utf8(stream: TextIO) -> TextIO:
    # Replies carry non-ASCII text, so a C-locale default encoding would fail on write.
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8")
    return stream
