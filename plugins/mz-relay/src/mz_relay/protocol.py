"""relayd wire framing: newline-delimited UTF-8 JSON requests and responses over a Unix socket."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import TypeGuard

from mz_relay.errors import ProtocolError

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1_048_576

# The per-line "v" stays readable across protocol bumps, so hello can still answer a mismatch.
_ENVELOPE_VERSION = 1
_RECV_BYTES = 65_536


@dataclass(frozen=True)
class Request:
    rid: int
    op: str
    args: dict[str, object]


@dataclass(frozen=True)
class Response:
    rid: int | None
    ok: bool
    result: dict[str, object] | None = None
    error: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError(f"an ok response needs a result and no error (rid {self.rid})")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError(f"an error response needs an error and no result (rid {self.rid})")


def encode(msg: Request | Response) -> bytes:
    """One JSON line; json escapes newlines inside strings, so the only newline is the last byte."""
    frame: dict[str, object] = {"v": _ENVELOPE_VERSION, "rid": msg.rid}
    if isinstance(msg, Request):
        frame["op"] = msg.op
        frame["args"] = msg.args
    elif isinstance(msg, Response):
        frame["ok"] = msg.ok
        if msg.ok:
            frame["result"] = msg.result
        else:
            frame["error"] = msg.error
    else:
        raise TypeError(f"can only encode a Request or Response, got {type(msg).__name__}")
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_request(line: bytes) -> Request:
    frame = _load_frame(line, "request")
    rid = frame.get("rid")
    if not _is_int(rid):
        raise _invalid(f"request rid must be an integer, got {_kind(rid)}")
    op = frame.get("op")
    if not isinstance(op, str) or not op:
        raise _invalid(f"request op must be a non-empty string, got {_kind(op)}")
    args = frame.get("args")
    if not isinstance(args, dict):
        raise _invalid(f"request args must be an object, got {_kind(args)}")
    return Request(rid=rid, op=op, args=args)


def decode_response(line: bytes) -> Response:
    frame = _load_frame(line, "response")
    raw_rid = frame.get("rid")
    if raw_rid is not None and not _is_int(raw_rid):
        raise _invalid(f"response rid must be an integer or null, got {_kind(raw_rid)}")
    rid = raw_rid if _is_int(raw_rid) else None
    ok = frame.get("ok")
    if not isinstance(ok, bool):
        raise _invalid(f"response ok must be a boolean, got {_kind(ok)}")
    if ok:
        result = frame.get("result")
        if not isinstance(result, dict):
            raise _invalid(f"ok response result must be an object, got {_kind(result)}")
        return Response(rid=rid, ok=True, result=result)
    error = frame.get("error")
    if not isinstance(error, dict):
        raise _invalid(f"error response error must be an object, got {_kind(error)}")
    for key in ("code", "message"):
        if not isinstance(error.get(key), str):
            raise _invalid(f"response error {key} must be a string, got {_kind(error.get(key))}")
    if "data" in error and not isinstance(error["data"], dict):
        raise _invalid(f"response error data must be an object, got {_kind(error['data'])}")
    return Response(rid=rid, ok=False, error=error)


class LineReader:
    """Reads newline-terminated lines from a socket, refusing any line over MAX_LINE_BYTES."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = bytearray()
        # Bytes already searched for a newline, so a long line is not rescanned per chunk.
        self._scanned = 0

    def next_line(self) -> bytes | None:
        """The next line with its newline, or None at EOF; the socket's timeout applies per read."""
        while True:
            end = self._buffer.find(b"\n", self._scanned)
            if end >= 0:
                if end > MAX_LINE_BYTES:
                    raise self._too_long()
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                self._scanned = 0
                return line
            self._scanned = len(self._buffer)
            if self._scanned > MAX_LINE_BYTES:
                raise self._too_long()
            chunk = self._sock.recv(_RECV_BYTES)
            if not chunk:
                # An unterminated tail is a peer that hung up mid-frame, not a request.
                self._buffer.clear()
                self._scanned = 0
                return None
            self._buffer.extend(chunk)

    def _too_long(self) -> ProtocolError:
        return ProtocolError("line_too_long", f"line exceeds {MAX_LINE_BYTES} bytes")


def _load_frame(line: bytes, what: str) -> dict[str, object]:
    try:
        frame = json.loads(line)
    except (ValueError, RecursionError) as exc:
        raise _invalid(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise _invalid(f"{what} must be a JSON object, got {_kind(frame)}")
    version = frame.get("v")
    if version != _ENVELOPE_VERSION or isinstance(version, bool):
        raise _invalid(f"unsupported envelope version {version}")
    return frame


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _kind(value: object) -> str:
    # Names the type, not the value, so a stray message body never lands in an error text.
    return "null" if value is None else type(value).__name__


def _invalid(message: str) -> ProtocolError:
    return ProtocolError("invalid_request", message)
