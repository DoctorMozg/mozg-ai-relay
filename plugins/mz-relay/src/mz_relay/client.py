"""A blocking relayd client: one Unix-socket connection, hello first, then calls."""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import socket
from collections.abc import Mapping
from pathlib import Path

from mz_relay.errors import DaemonUnavailable, ProtocolError, ProtocolMismatch, RelayError
from mz_relay.paths import check_socket_path
from mz_relay.protocol import (
    PROTOCOL_VERSION,
    LineReader,
    Request,
    Response,
    decode_response,
    encode,
)

log = logging.getLogger(__name__)

CLIENT_KINDS = frozenset({"mcp", "cli", "hook", "pi", "opencode"})


class RelayClient:
    def __init__(self, socket_path: Path, *, timeout_s: float = 10.0) -> None:
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {timeout_s}")
        self._path = check_socket_path(socket_path)
        self._timeout_s = timeout_s
        self._rids = itertools.count(1)
        self._sock: socket.socket | None = None
        self._reader: LineReader | None = None

    def connect(self, *, client: str) -> dict[str, object]:
        """Connects and says hello; returns hello's result."""
        if client not in CLIENT_KINDS:
            raise ValueError(
                f"client must be one of {', '.join(sorted(CLIENT_KINDS))}, got {client!r}"
            )
        self.open()
        hello = {"protocol": PROTOCOL_VERSION, "client": client, "pid": os.getpid()}
        try:
            result = self.call("hello", hello)
        except BaseException:
            # Nothing but shutdown works after a refused hello, so the socket is not kept.
            self.close()
            raise
        log.info("connected to relayd pid %s as %s", result.get("daemon_pid"), client)
        return result

    def open(self) -> None:
        """Connects without hello; only hello and shutdown are allowed on such a connection."""
        if self._sock is not None:
            raise ValueError(f"already connected to relayd at {self._path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout_s)
        try:
            sock.connect(str(self._path))
        except OSError as exc:
            sock.close()
            raise DaemonUnavailable(
                f"relayd is not serving at {self._path}: {exc.strerror or exc}",
                {"socket": str(self._path)},
            ) from exc
        self._sock = sock
        self._reader = LineReader(sock)

    def call(
        self, op: str, args: Mapping[str, object], *, timeout_s: float | None = None
    ) -> dict[str, object]:
        """Sends one request and returns its result; an error reply raises its RelayError."""
        if not op:
            raise ValueError("op must be a non-empty string")
        timeout = self._timeout_s if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError(f"timeout_s must be positive, got {timeout}")
        sock, reader = self._connection(op)
        rid = next(self._rids)
        try:
            sock.settimeout(timeout)
            sock.sendall(encode(Request(rid=rid, op=op, args=dict(args))))
            line = reader.next_line()
        except TimeoutError as exc:
            # A late reply would answer the wrong call, so the connection is dropped.
            self.close()
            log.warning("relayd sent no reply to %s within %g s", op, timeout)
            raise DaemonUnavailable(
                f"no reply within {timeout:g} s to {op}", {"op": op, "timeout_s": timeout}
            ) from exc
        except OSError as exc:
            self.close()
            raise DaemonUnavailable(f"connection to relayd lost during {op}: {exc}") from exc
        except ProtocolError:
            self.close()
            raise
        if line is None:
            self.close()
            raise DaemonUnavailable(f"relayd closed the connection during {op}", {"op": op})
        try:
            response = decode_response(line)
        except ProtocolError:
            self.close()
            raise
        return self._unwrap(op, rid, response)

    def recv_eof(self) -> None:
        """Blocks until the server closes the connection or close() is called."""
        sock = self._sock
        if sock is None:
            return
        try:
            sock.settimeout(None)
            while sock.recv(4096):
                pass
        except OSError as exc:
            log.debug("relayd connection ended with %s", exc)
        log.info("relayd closed the connection at %s", self._path)

    def close(self) -> None:
        sock, self._sock, self._reader = self._sock, None, None
        if sock is None:
            return
        # shutdown wakes a recv_eof blocked on another thread; close alone does not.
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        sock.close()

    def _connection(self, op: str) -> tuple[socket.socket, LineReader]:
        if self._sock is None or self._reader is None:
            raise DaemonUnavailable(f"not connected to relayd at {self._path}; cannot send {op}")
        return self._sock, self._reader

    def _unwrap(self, op: str, rid: int, response: Response) -> dict[str, object]:
        if response.rid is not None and response.rid != rid:
            self.close()
            raise ProtocolError(
                "invalid_request", f"reply to {op} carries rid {response.rid}, expected {rid}"
            )
        if response.ok and response.result is not None:
            return response.result
        raise _error_from_wire(response.error or {})


def _error_from_wire(error: Mapping[str, object]) -> RelayError:
    code = str(error.get("code") or "internal")
    message = str(error.get("message") or "")
    raw_data = error.get("data")
    data = raw_data if isinstance(raw_data, dict) else None
    if code == "protocol_mismatch":
        log.warning("relayd refused protocol %s: %s", PROTOCOL_VERSION, message)
        return ProtocolMismatch(message, data)
    return RelayError(code, message, data)
