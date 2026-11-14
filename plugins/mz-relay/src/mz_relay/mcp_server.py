"""The stdio MCP server: JSON-RPC framing, the handshake, and the send/inbox/ack/peers tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Protocol, TextIO

from mz_relay.envelope import Kind
from mz_relay.errors import RelayError, RelayUnavailable
from mz_relay.policy import MAX_BODY_CHARS

log = logging.getLogger(__name__)

# 2026-07-28 is the server/discover era; answering it would skip the initialize fallback.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-11-25")
DEFAULT_PROTOCOL = "2025-11-25"
SERVER_NAME = "mz-relay"
INBOX_LIMIT_MIN = 1
INBOX_LIMIT_MAX = 50
INBOX_LIMIT_DEFAULT = 20

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class SendArgs:
    to: str
    body: str
    kind: Kind
    context_id: str | None
    in_reply_to: str | None


class ToolBackend(Protocol):
    """What the tools call; each returns the tool text and raises RelayError for business errors."""

    def instructions(self) -> str: ...
    def send(self, args: SendArgs) -> str: ...
    def inbox(self, limit: int) -> str: ...
    def ack(self, msg_id: str) -> str: ...
    def peers(self) -> str: ...
    def extension_tools(self) -> list[dict[str, object]]: ...
    def ext_call(self, name: str, arguments: Mapping[str, object]) -> str: ...


def _schema(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


TOOLS: list[dict[str, object]] = [
    {
        "name": "send",
        "description": (
            "Send a message to another agent on mz-relay by roster name. "
            "Returns the message id and whether it was queued or held for user approval."
        ),
        "inputSchema": _schema(
            {
                "to": {"type": "string", "description": "Roster name of the recipient."},
                "body": {
                    "type": "string",
                    "maxLength": MAX_BODY_CHARS,
                    "description": "The message text.",
                },
                "kind": {
                    "type": "string",
                    "enum": [kind.value for kind in Kind],
                    "default": Kind.REQUEST.value,
                    "description": (
                        "request expects a reply, inform does not, reply answers a message, "
                        "done closes the context."
                    ),
                },
                "context_id": {
                    "type": "string",
                    "description": "Only with in_reply_to; must equal the parent's context",
                },
                "in_reply_to": {
                    "type": "string",
                    "description": "Id of the message this one answers.",
                },
            },
            ["to", "body"],
        ),
    },
    {
        "name": "inbox",
        "description": (
            "Read new peer messages, the ones still awaiting your ack, and the status of "
            "messages you sent."
        ),
        "inputSchema": _schema(
            {
                "limit": {
                    "type": "integer",
                    "minimum": INBOX_LIMIT_MIN,
                    "maximum": INBOX_LIMIT_MAX,
                    "default": INBOX_LIMIT_DEFAULT,
                    "description": "Most new messages to return.",
                }
            }
        ),
    },
    {
        "name": "ack",
        "description": "Mark a peer message as handled.",
        "inputSchema": _schema(
            {"id": {"type": "string", "description": "Id of the message to ack."}}, ["id"]
        ),
    },
    {
        "name": "peers",
        "description": "List the other agents on mz-relay, their roles, and who is online.",
        "inputSchema": _schema({}),
    },
]


class _RpcError(Exception):
    """A JSON-RPC error reply; raised inside dispatch, answered by handle_line."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Arguments:
    """Hand-written checks of one tool's arguments against its inputSchema."""

    def __init__(self, tool: str, arguments: Mapping[str, object], allowed: set[str]) -> None:
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise self._fail(tool, f"unknown arguments: {', '.join(unknown)}")
        self.tool = tool
        self.arguments = arguments

    @staticmethod
    def _fail(tool: str, message: str) -> _RpcError:
        return _RpcError(INVALID_PARAMS, f"{tool}: {message}")

    def optional_string(self, name: str) -> str | None:
        return self.required_string(name) if name in self.arguments else None

    def required_string(self, name: str) -> str:
        if name not in self.arguments:
            raise self._fail(self.tool, f"{name} is required")
        value = self.arguments[name]
        if not isinstance(value, str):
            raise self._fail(self.tool, f"{name} must be a string, got {type(value).__name__}")
        return value


class McpServer:
    def __init__(self, backend: ToolBackend, *, version: str) -> None:
        if not isinstance(version, str) or not version:
            raise ValueError(f"version must be a non-empty string, got {version!r}")
        self._backend = backend
        self._version = version
        self._methods: dict[str, Callable[[Mapping[str, object]], dict[str, object]]] = {
            "initialize": self._initialize,
            "ping": lambda _params: {},
            "tools/list": lambda _params: {"tools": [*TOOLS, *self._ext_tools]},
            "tools/call": self._call_tool,
        }
        self._tools: dict[str, Callable[[Mapping[str, object]], str]] = {
            "send": self._send,
            "inbox": self._inbox,
            "ack": self._ack,
            "peers": self._peers,
        }
        self._ext_tools: list[dict[str, object]] = []

    def handle_line(self, line: str) -> str | None:
        """One JSON-RPC line in, one reply line out, or None for a notification."""
        try:
            frame = json.loads(line)
        except (ValueError, RecursionError) as exc:
            log.warning("unparsable MCP frame: %s", exc)
            return self._error(None, PARSE_ERROR, f"parse error: {exc}")
        if not isinstance(frame, dict):
            return self._error(None, INVALID_REQUEST, "a request must be a JSON object")
        rid = frame.get("id")
        if "id" in frame and (isinstance(rid, bool) or not isinstance(rid, str | int)):
            return self._error(None, INVALID_REQUEST, f"id must be a string or integer: {rid!r}")
        if frame.get("jsonrpc") != "2.0":
            return self._error(rid, INVALID_REQUEST, 'jsonrpc must be "2.0"')
        method = frame.get("method")
        if not isinstance(method, str):
            return self._error(rid, INVALID_REQUEST, "method must be a string")
        if "id" not in frame:
            log.debug("notification %s", method)
            return None
        handler = self._methods.get(method)
        if handler is None:
            log.info("method not found: %s", method)
            return self._error(rid, METHOD_NOT_FOUND, f"method not found: {method}")
        params = frame.get("params", {})
        if not isinstance(params, dict):
            return self._error(rid, INVALID_PARAMS, f"{method}: params must be an object")
        try:
            result = handler(params)
        except _RpcError as exc:
            log.info("%s rejected: %s", method, exc.message)
            return self._error(rid, exc.code, exc.message)
        except Exception as exc:
            log.exception("%s failed", method)
            return self._error(rid, INTERNAL_ERROR, f"internal error: {type(exc).__name__}")
        return self._encode({"jsonrpc": "2.0", "id": rid, "result": result})

    def serve(self, stdin: TextIO, stdout: TextIO) -> int:
        """Answers stdin line by line until EOF; stdout carries only reply frames."""
        log.info("MCP server %s reading stdin", self._version)
        for line in stdin:
            if not line.strip():
                continue
            reply = self.handle_line(line)
            if reply is not None:
                stdout.write(reply + "\n")
                stdout.flush()
        log.info("stdin closed; MCP server stops")
        return 0

    def _initialize(self, params: Mapping[str, object]) -> dict[str, object]:
        offered = params.get("protocolVersion")
        version = offered if offered in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        log.info("initialize: client offered %r, using %s", offered, version)
        # Harnesses fix the tool list at connect (list_changed is unreliable), so it is read once.
        self._ext_tools = self._backend.extension_tools()
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": self._version},
            "instructions": self._backend.instructions(),
        }

    def _call_tool(self, params: Mapping[str, object]) -> dict[str, object]:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None and any(entry["name"] == name for entry in self._ext_tools):
            tool = partial(self._backend.ext_call, str(name))
        if tool is None:
            raise _RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, f"{name}: arguments must be an object")
        try:
            text = tool(arguments)
        except RelayUnavailable as exc:
            # Its message already names the code, so it goes out as-is.
            log.info("tool %s answered unavailable: %s", name, exc.code)
            return _tool_result(exc.message, is_error=True)
        except RelayError as exc:
            log.info("tool %s returned error %s", name, exc.code)
            return _tool_result(f"mz-relay error {exc.code}: {exc.message}", is_error=True)
        return _tool_result(text, is_error=False)

    def _send(self, arguments: Mapping[str, object]) -> str:
        args = _Arguments("send", arguments, {"to", "body", "kind", "context_id", "in_reply_to"})
        body = args.required_string("body")
        if len(body) > MAX_BODY_CHARS:
            raise _RpcError(
                INVALID_PARAMS, f"send: body has {len(body)} characters, max {MAX_BODY_CHARS}"
            )
        kind = args.optional_string("kind")
        if kind is None:
            kind = Kind.REQUEST.value
        elif kind not in {k.value for k in Kind}:
            allowed = ", ".join(k.value for k in Kind)
            raise _RpcError(INVALID_PARAMS, f"send: kind must be one of {allowed}, got {kind!r}")
        return self._backend.send(
            SendArgs(
                to=args.required_string("to"),
                body=body,
                kind=Kind(kind),
                context_id=args.optional_string("context_id"),
                in_reply_to=args.optional_string("in_reply_to"),
            )
        )

    def _inbox(self, arguments: Mapping[str, object]) -> str:
        _Arguments("inbox", arguments, {"limit"})
        limit = arguments.get("limit", INBOX_LIMIT_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise _RpcError(INVALID_PARAMS, f"inbox: limit must be an integer, got {limit!r}")
        if not INBOX_LIMIT_MIN <= limit <= INBOX_LIMIT_MAX:
            raise _RpcError(
                INVALID_PARAMS,
                f"inbox: limit must be {INBOX_LIMIT_MIN}..{INBOX_LIMIT_MAX}, got {limit}",
            )
        return self._backend.inbox(limit)

    def _ack(self, arguments: Mapping[str, object]) -> str:
        return self._backend.ack(_Arguments("ack", arguments, {"id"}).required_string("id"))

    def _peers(self, arguments: Mapping[str, object]) -> str:
        _Arguments("peers", arguments, set())
        return self._backend.peers()

    def _error(self, rid: object, code: int, message: str) -> str:
        return self._encode(
            {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
        )

    @staticmethod
    def _encode(frame: dict[str, object]) -> str:
        return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def _tool_result(text: str, *, is_error: bool) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}
