"""McpServer: JSON-RPC handshake, tool schemas and calls, framing and error codes."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from mz_relay import __version__
from mz_relay.envelope import Kind
from mz_relay.errors import RelayError
from mz_relay.mcp_server import DEFAULT_PROTOCOL, SUPPORTED_PROTOCOLS, McpServer, SendArgs

BRIEF = 'You are "alice" on mz-relay, role planner: Plans the work.'
HELD_TEXT = "held m1 for user approval — ask your user to run: mz-relay approve m1"


class FakeBackend:
    """Stands in for relayd behind the ToolBackend seam; records calls, replays texts."""

    def __init__(self, *, text: str = "queued m1 for bob", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def _answer(self, tool: str, arg: object) -> str:
        self.calls.append((tool, arg))
        if self.error is not None:
            raise self.error
        return self.text

    def instructions(self) -> str:
        return BRIEF

    def send(self, args: SendArgs) -> str:
        return self._answer("send", args)

    def inbox(self, limit: int) -> str:
        return self._answer("inbox", limit)

    def ack(self, msg_id: str) -> str:
        return self._answer("ack", msg_id)

    def peers(self) -> str:
        return self._answer("peers", None)


def make_server(backend: FakeBackend | None = None) -> McpServer:
    return McpServer(backend or FakeBackend(), version=__version__)


def exchange(server: McpServer, frame: object) -> dict[str, Any]:
    raw = server.handle_line(json.dumps(frame))
    assert raw is not None, f"no reply to {frame!r}"
    reply: dict[str, Any] = json.loads(raw)
    return reply


def request(method: str, params: object = None, rid: object = 1) -> dict[str, object]:
    frame: dict[str, object] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def tool_call(server: McpServer, name: str, arguments: object = None) -> dict[str, Any]:
    params: dict[str, object] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return exchange(server, request("tools/call", params))


def error_code(reply: dict[str, Any]) -> int:
    assert "error" in reply, f"expected an error, got {reply!r}"
    code = reply["error"]["code"]
    assert isinstance(code, int)
    return code


class TestHandshake:
    def test_server_discover_is_method_not_found(self) -> None:
        params = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}

        reply = exchange(
            make_server(), request("server/discover", params, "server-discover-probe-1")
        )

        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == "server-discover-probe-1"
        assert error_code(reply) == -32601

    @pytest.mark.parametrize(
        ("offered", "expected"),
        [
            ("2025-11-25", "2025-11-25"),
            ("2025-06-18", "2025-06-18"),
            ("2026-07-28", "2025-11-25"),
            ("1999-01-01", "2025-11-25"),
        ],
    )
    def test_initialize_negotiates_the_protocol_version(self, offered: str, expected: str) -> None:
        params = {"protocolVersion": offered, "capabilities": {}, "clientInfo": {"name": "t"}}

        reply = exchange(make_server(), request("initialize", params))

        assert reply["result"]["protocolVersion"] == expected

    def test_supported_protocols_never_include_the_discover_era(self) -> None:
        assert SUPPORTED_PROTOCOLS == ("2025-06-18", "2025-11-25")
        assert DEFAULT_PROTOCOL == "2025-11-25"

    def test_initialize_result_carries_tools_capability_server_info_and_brief(self) -> None:
        params = {"protocolVersion": "2025-11-25", "capabilities": {}}

        result = exchange(make_server(), request("initialize", params, 7))["result"]

        assert result["capabilities"] == {"tools": {}}
        assert result["serverInfo"] == {"name": "mz-relay", "version": __version__}
        assert result["instructions"] == BRIEF

    def test_initialized_notification_gets_no_reply(self) -> None:
        frame = {"jsonrpc": "2.0", "method": "notifications/initialized"}

        assert make_server().handle_line(json.dumps(frame)) is None

    def test_unknown_notification_gets_no_reply(self) -> None:
        frame = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}

        assert make_server().handle_line(json.dumps(frame)) is None

    def test_ping_returns_an_empty_result(self) -> None:
        reply = exchange(make_server(), request("ping", rid="p-1"))

        assert reply == {"jsonrpc": "2.0", "id": "p-1", "result": {}}


class TestTools:
    def test_tools_list_names_and_schemas(self) -> None:
        tools = exchange(make_server(), request("tools/list"))["result"]["tools"]

        assert [t["name"] for t in tools] == ["send", "inbox", "ack", "peers"]
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"
            assert tool["inputSchema"]["additionalProperties"] is False
            assert tool["description"]
        by_name = {t["name"]: t["inputSchema"] for t in tools}
        assert sorted(by_name["send"]["required"]) == ["body", "to"]
        assert by_name["send"]["properties"]["body"]["maxLength"] == 32000
        assert by_name["send"]["properties"]["kind"]["enum"] == [
            "request",
            "inform",
            "reply",
            "done",
        ]
        assert by_name["send"]["properties"]["kind"]["default"] == "request"
        assert "in_reply_to" in by_name["send"]["properties"]["context_id"]["description"]
        limit = by_name["inbox"]["properties"]["limit"]
        assert (limit["type"], limit["minimum"], limit["maximum"]) == ("integer", 1, 50)
        assert by_name["ack"]["required"] == ["id"]
        assert by_name["peers"].get("properties", {}) == {}

    @pytest.mark.parametrize("text", ["queued m1 for bob", HELD_TEXT])
    def test_send_returns_the_backend_text_verbatim(self, text: str) -> None:
        server = make_server(FakeBackend(text=text))

        result = tool_call(server, "send", {"to": "bob", "body": "hi"})["result"]

        assert result["content"] == [{"type": "text", "text": text}]
        assert result.get("isError", False) is False

    def test_send_defaults_kind_to_request_and_omits_chain_fields(self) -> None:
        backend = FakeBackend()

        tool_call(make_server(backend), "send", {"to": "bob", "body": "hi"})

        assert backend.calls == [
            (
                "send",
                SendArgs(to="bob", body="hi", kind=Kind.REQUEST, context_id=None, in_reply_to=None),
            )
        ]

    def test_send_passes_reply_fields_through(self) -> None:
        backend = FakeBackend()
        arguments = {
            "to": "alice",
            "body": "ok",
            "kind": "reply",
            "in_reply_to": "m1",
            "context_id": "m1",
        }

        tool_call(make_server(backend), "send", arguments)

        assert backend.calls == [
            (
                "send",
                SendArgs(to="alice", body="ok", kind=Kind.REPLY, context_id="m1", in_reply_to="m1"),
            )
        ]

    def test_inbox_defaults_limit_to_20(self) -> None:
        backend = FakeBackend(text="no new messages")

        result = tool_call(make_server(backend), "inbox", {})["result"]

        assert backend.calls == [("inbox", 20)]
        assert result["content"][0]["text"] == "no new messages"

    def test_inbox_passes_an_explicit_limit(self) -> None:
        backend = FakeBackend()

        tool_call(make_server(backend), "inbox", {"limit": 50})

        assert backend.calls == [("inbox", 50)]

    def test_ack_and_peers_reach_the_backend(self) -> None:
        backend = FakeBackend()
        server = make_server(backend)

        tool_call(server, "ack", {"id": "m1"})
        tool_call(server, "peers")

        assert backend.calls == [("ack", "m1"), ("peers", None)]

    def test_business_error_is_a_tool_result_with_is_error(self) -> None:
        backend = FakeBackend(error=RelayError("not_allowed", "bob may not message carol"))

        reply = tool_call(make_server(backend), "send", {"to": "carol", "body": "hi"})

        assert "error" not in reply
        assert reply["result"]["isError"] is True
        assert reply["result"]["content"] == [
            {"type": "text", "text": "mz-relay error not_allowed: bob may not message carol"}
        ]

    @pytest.mark.parametrize(
        ("name", "arguments"),
        [
            ("send", {"body": "hi"}),
            ("send", {"to": "bob"}),
            ("send", {"to": 7, "body": "hi"}),
            ("send", {"to": "bob", "body": "hi", "kind": "ack"}),
            ("send", {"to": "bob", "body": "hi", "colour": "red"}),
            ("send", {"to": "bob", "body": "x" * 32_001}),
            ("send", "x"),
            ("inbox", {"limit": 51}),
            ("inbox", {"limit": 0}),
            ("inbox", {"limit": "5"}),
            ("ack", {}),
            ("broadcast", {"body": "hi"}),
        ],
    )
    def test_bad_arguments_or_unknown_tool_are_invalid_params(
        self, name: str, arguments: object
    ) -> None:
        backend = FakeBackend()

        reply = tool_call(make_server(backend), name, arguments)

        assert error_code(reply) == -32602
        assert backend.calls == []


class TestFraming:
    def test_unparsable_line_is_parse_error_with_null_id(self) -> None:
        raw = make_server().handle_line("{bad")

        assert raw is not None
        reply = json.loads(raw)
        assert error_code(reply) == -32700
        assert reply["id"] is None

    @pytest.mark.parametrize(
        "frame",
        [[], {"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "1.0", "id": 1, "method": "ping"}, "ping"],
    )
    def test_malformed_requests_are_invalid_request(self, frame: object) -> None:
        assert error_code(exchange(make_server(), frame)) == -32600

    def test_deeply_nested_line_is_parse_error_and_server_survives(self) -> None:
        server = make_server()

        raw = server.handle_line("[" * 100_000 + "]" * 100_000)

        assert raw is not None
        reply = json.loads(raw)
        assert error_code(reply) == -32700
        assert reply["id"] is None
        assert exchange(server, request("ping", rid=2)) == {"jsonrpc": "2.0", "id": 2, "result": {}}

    def test_unknown_method_with_id_is_method_not_found(self) -> None:
        assert error_code(exchange(make_server(), request("resources/list"))) == -32601

    def test_unexpected_backend_exception_is_internal_and_server_survives(self) -> None:
        server = make_server(FakeBackend(error=ValueError("boom")))

        reply = tool_call(server, "peers")

        assert error_code(reply) == -32603
        assert exchange(server, request("ping", rid=2)) == {"jsonrpc": "2.0", "id": 2, "result": {}}

    def test_reply_is_one_line_and_keeps_non_ascii_unescaped(self) -> None:
        server = make_server(FakeBackend(text="готово: m1"))
        frame = request("tools/call", {"name": "peers", "arguments": {}})

        raw = server.handle_line(json.dumps(frame))

        assert raw is not None
        assert "\n" not in raw.rstrip("\n")
        assert "готово" in raw

    def test_serve_writes_one_json_line_per_reply_and_returns_0_at_eof(self) -> None:
        frames = [
            request("server/discover", {}, "probe"),
            request("initialize", {"protocolVersion": "2025-11-25"}, 1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request("tools/list", rid=2),
            request("ping", rid=3),
        ]
        stdin = io.StringIO("".join(json.dumps(f) + "\n" for f in frames))
        stdout = io.StringIO()

        assert make_server().serve(stdin, stdout) == 0

        output = stdout.getvalue()
        assert output.endswith("\n")
        replies = [json.loads(line) for line in output.splitlines()]
        assert [r["id"] for r in replies] == ["probe", 1, 2, 3]
        assert all(r["jsonrpc"] == "2.0" for r in replies)
