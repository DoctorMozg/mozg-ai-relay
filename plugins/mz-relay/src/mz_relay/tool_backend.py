"""The MCP tools backed by relayd, the retry after a failed registration, and `mz-relay mcp`."""

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
from mz_relay.adapters.claude_registry import (
    NotInClaudeSession,
    claude_config_dir,
    discover_own_session,
)
from mz_relay.clock import SystemClock
from mz_relay.errors import (
    DaemonUnavailable,
    EnvelopeError,
    ProtocolError,
    RelayError,
    RelayUnavailable,
)
from mz_relay.launcher import DaemonLauncher
from mz_relay.mcp_server import INBOX_LIMIT_MAX, INBOX_LIMIT_MIN, McpServer, SendArgs
from mz_relay.paths import Paths, user_home
from mz_relay.presence import PresenceSession, RegisterArgs, configured_name
from mz_relay.roster import Harness

log = logging.getLogger(__name__)

MCP_HARNESSES = (Harness.CLAUDE, Harness.CODEX, Harness.OPENCODE)
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

    def close(self) -> None:
        self._presence.close()

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

    def extension_tools(self) -> list[dict[str, object]]:
        return list(self._presence.current().ext_tools)

    def ext_call(self, name: str, arguments: Mapping[str, object]) -> str:
        return self._call("ext_call", {"tool": name, "args": dict(arguments)})

    def _call(self, op: str, args: Mapping[str, object]) -> str:
        text = self._result(op, args).get("text")
        if not isinstance(text, str):
            raise ProtocolError("invalid_request", f"relayd's {op} result has no text")
        return text

    def _result(self, op: str, args: Mapping[str, object]) -> dict[str, object]:
        try:
            return self._attempt(op, args)
        except DaemonUnavailable as exc:
            log.info("relayd unavailable during %s (%s); retrying once", op, exc.message)
        try:
            return self._attempt(op, args)
        except DaemonUnavailable as exc:
            log.warning("relayd still unavailable during %s: %s", op, exc.message)
            raise

    def _attempt(self, op: str, args: Mapping[str, object]) -> dict[str, object]:
        relay = self._launcher.connect(client="mcp")
        try:
            return relay.call(op, {**args, "token": self._presence.current().token})
        finally:
            relay.close()


def _unavailable(error: RelayError) -> RelayUnavailable:
    return RelayUnavailable(error.code, f"mz-relay is unavailable: {error.code}: {error.message}")


class LateRegistrationBackend:
    """Serves the tools after a failed registration and registers again on each tool call.
    Harnesses keep an MCP server running for long (Codex: a shared daemon), so a fix must land."""

    def __init__(self, error: RelayError, register: Callable[[], RelayToolBackend]) -> None:
        if not isinstance(error, RelayError):
            raise TypeError(f"error must be a RelayError, got {type(error).__name__}")
        self._error = error
        self._register = register
        self._backend: RelayToolBackend | None = None

    def instructions(self) -> str:
        return f"{_unavailable(self._error).message}. Each tool call tries to register again."

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()

    def send(self, args: SendArgs) -> str:
        return self._call(lambda backend: backend.send(args))

    def inbox(self, limit: int) -> str:
        return self._call(lambda backend: backend.inbox(limit))

    def ack(self, msg_id: str) -> str:
        return self._call(lambda backend: backend.ack(msg_id))

    def peers(self) -> str:
        return self._call(lambda backend: backend.peers())

    def extension_tools(self) -> list[dict[str, object]]:
        # Unregistered at initialize means no extension tools this session; the list is fixed then.
        return [] if self._backend is None else self._backend.extension_tools()

    def ext_call(self, name: str, arguments: Mapping[str, object]) -> str:
        return self._call(lambda backend: backend.ext_call(name, arguments))

    def _call(self, tool: Callable[[RelayToolBackend], str]) -> str:
        if self._backend is not None:
            return tool(self._backend)
        try:
            backend = self._register()
        except RelayError as exc:
            log.info("registration still fails: %s: %s", exc.code, exc.message)
            self._error = exc
            raise _unavailable(exc) from exc
        log.info("registered on a later tool call")
        self._backend = backend
        # The agent got the unavailable text at initialize, so its first answer carries the brief.
        return f"{backend.instructions()}\n\n{tool(backend)}"


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

    def register() -> RelayToolBackend:
        launcher = _launcher(env)
        presence = PresenceSession(launcher, _register_args(harness, env), clock=SystemClock())
        presence.start()
        return RelayToolBackend(presence, launcher)

    backend: RelayToolBackend | LateRegistrationBackend | None = None
    try:
        try:
            backend = register()
        except RelayError as exc:
            log.warning("serving without relayd: %s: %s", exc.code, exc.message)
            backend = LateRegistrationBackend(exc, register)
        server = McpServer(backend, version=__version__)
        return server.serve(_utf8(sys.stdin), _utf8(sys.stdout))
    except _Terminated:
        log.info("SIGTERM; MCP server stops")
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        if backend is not None:
            backend.close()


def _launcher(env: Mapping[str, str]) -> DaemonLauncher:
    paths = Paths.from_env(env, uid=os.getuid(), pw_home=user_home())
    return DaemonLauncher(paths, clock=SystemClock(), env=env)


def _register_args(harness: Harness, env: Mapping[str, str]) -> RegisterArgs:
    name = configured_name(env)
    log.info("registering %s session as %s", harness, name or "<resolved from cwd>")
    config_dir: Path | None = None
    delivery: dict[str, object] | None = None
    if harness is Harness.CLAUDE:
        config_dir, delivery = _claude_binding(env)
    return RegisterArgs(
        name=name,
        harness=harness,
        cwd=Path.cwd(),
        pid=os.getpid(),
        client="mcp",
        config_dir=config_dir,
        delivery=delivery,
    )


def _claude_binding(env: Mapping[str, str]) -> tuple[Path, dict[str, object] | None]:
    """The session's native inbox binding; without a messaging socket the waiter delivers."""
    try:
        # Claude Code is this MCP child's parent; CLAUDE_PID may leak in from an outer session.
        own = discover_own_session(env, ppid=os.getppid())
    except NotInClaudeSession as exc:
        log.warning("registering tools only, with no native inbox: %s", exc.message)
        return claude_config_dir(env), None
    delivery: dict[str, object] = {
        "messaging_socket": str(own.messaging_socket),
        "pid": own.pid,
        "config_dir": str(own.config_dir),
        "session_id": own.session_id,
    }
    log.info("native inbox binding for Claude pid %d", own.pid)
    return own.config_dir, delivery


def _utf8(stream: TextIO) -> TextIO:
    # Replies carry non-ASCII text, so a C-locale default encoding would fail on write.
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8")
    return stream
