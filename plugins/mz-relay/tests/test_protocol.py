"""relayd wire framing: newline-delimited JSON requests and responses."""

from __future__ import annotations

import contextlib
import json
import socket

import pytest
from mz_relay.errors import ProtocolError
from mz_relay.protocol import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    LineReader,
    Request,
    Response,
    decode_request,
    decode_response,
    encode,
)
from relay_rig import PROC_WAIT_S, InThread


class TestEncoding:
    def test_constants_match_the_spec(self) -> None:
        assert PROTOCOL_VERSION == 1
        assert MAX_LINE_BYTES == 1_048_576

    def test_a_request_round_trips_with_non_ascii_and_newlines_in_args(self) -> None:
        request = Request(rid=7, op="send", args={"body": "привет\n", "to": "bob"})
        line = encode(request)
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1
        assert decode_request(line) == request

    def test_a_request_has_the_spec_wire_shape(self) -> None:
        line = encode(Request(rid=1, op="hello", args={"protocol": 1}))
        assert json.loads(line) == {"v": 1, "rid": 1, "op": "hello", "args": {"protocol": 1}}

    def test_an_ok_response_round_trips(self) -> None:
        response = Response(rid=3, ok=True, result={"text": "queued m1 for bob ✓"})
        line = encode(response)
        assert line.count(b"\n") == 1
        assert json.loads(line) == {
            "v": 1,
            "rid": 3,
            "ok": True,
            "result": {"text": "queued m1 for bob ✓"},
        }
        assert decode_response(line) == response

    def test_an_error_response_round_trips_with_data(self) -> None:
        error: dict[str, object] = {
            "code": "protocol_mismatch",
            "message": "old",
            "data": {"daemon_protocol": 1},
        }
        response = Response(rid=4, ok=False, error=error)
        line = encode(response)
        assert json.loads(line) == {"v": 1, "rid": 4, "ok": False, "error": error}
        assert decode_response(line) == response

    def test_a_response_to_a_malformed_line_carries_a_null_rid(self) -> None:
        response = Response(rid=None, ok=False, error={"code": "invalid_request", "message": "x"})
        assert json.loads(encode(response))["rid"] is None
        assert decode_response(encode(response)) == response


class TestDecodeErrors:
    @pytest.mark.parametrize(
        "line",
        [
            b"not json\n",
            b'{"v":1,"rid":1,"args":{}}\n',
            b'{"v":1,"rid":1,"op":"hello","args":[]}\n',
            b"[1, 2]\n",
        ],
        ids=["not-json", "missing-op", "args-list", "not-an-object"],
    )
    def test_malformed_requests_are_invalid_request(self, line: bytes) -> None:
        with pytest.raises(ProtocolError) as caught:
            decode_request(line)
        assert caught.value.code == "invalid_request"

    def test_an_unknown_envelope_version_is_named(self) -> None:
        with pytest.raises(ProtocolError, match="unsupported envelope version 2") as caught:
            decode_request(b'{"v":2,"rid":1,"op":"hello","args":{}}\n')
        assert caught.value.code == "invalid_request"


class TestLineReader:
    def test_lines_split_across_chunks_come_out_whole_then_none_at_eof(self) -> None:
        left, right = socket.socketpair()
        with left, right:
            reader = LineReader(left)
            right.sendall(b'{"a":1}\n{"b"')
            assert json.loads(reader.next_line() or b"") == {"a": 1}
            right.sendall(b":2}\n")
            right.shutdown(socket.SHUT_WR)
            assert json.loads(reader.next_line() or b"") == {"b": 2}
            assert reader.next_line() is None

    def test_a_line_of_exactly_one_mebibyte_is_accepted(self) -> None:
        left, right = socket.socketpair()
        with left, right:
            # The writer blocks once the socket buffer fills, so it runs beside the reader.
            writer = InThread(lambda: right.sendall(b"x" * MAX_LINE_BYTES + b"\n"))
            line = LineReader(left).next_line()
            writer.result(PROC_WAIT_S)

        assert line is not None
        assert line.rstrip(b"\n") == b"x" * MAX_LINE_BYTES

    def test_a_line_over_one_mebibyte_is_line_too_long(self) -> None:
        left, right = socket.socketpair()

        def flood() -> None:
            # The reader gives up and closes mid-line, which breaks this write.
            with contextlib.suppress(OSError):
                right.sendall(b"x" * (MAX_LINE_BYTES + 1) + b"\n")

        writer = InThread(flood)
        try:
            with pytest.raises(ProtocolError) as caught:
                LineReader(left).next_line()
            assert caught.value.code == "line_too_long"
        finally:
            left.close()
            writer.result(PROC_WAIT_S)
            right.close()
