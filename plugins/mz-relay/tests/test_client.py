"""RelayClient against a fake relayd thread on a real Unix socket."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from contextlib import closing

import pytest
from mz_relay.client import RelayClient
from mz_relay.errors import DaemonUnavailable, ProtocolMismatch, RelayError
from mz_relay.paths import Paths
from mz_relay.protocol import PROTOCOL_VERSION
from relay_rig import (
    PROC_WAIT_S,
    WAIT_S,
    FakeRelayd,
    InThread,
    Message,
    Reply,
    close_connection,
    fail,
    hello_result,
    hello_then,
    ok,
    wait_until,
)

Serve = Callable[[Reply], FakeRelayd]


class TestConnect:
    def test_connect_sends_hello_and_returns_its_result(
        self, relay_paths: Paths, fake_relayd: Serve
    ) -> None:
        server = fake_relayd(hello_then(close_connection))
        with closing(RelayClient(relay_paths.socket)) as client:
            assert client.connect(client="cli") == hello_result()
        hello = server.requests[0]
        assert hello["v"] == 1
        assert hello["op"] == "hello"
        assert hello["args"] == {"protocol": PROTOCOL_VERSION, "client": "cli", "pid": os.getpid()}

    def test_a_protocol_mismatch_raises_protocol_mismatch(
        self, relay_paths: Paths, fake_relayd: Serve
    ) -> None:
        fake_relayd(
            lambda req: fail(req, "protocol_mismatch", "old daemon", {"daemon_protocol": 2})
        )
        with (
            closing(RelayClient(relay_paths.socket)) as client,
            pytest.raises(ProtocolMismatch) as caught,
        ):
            client.connect(client="cli")
        assert caught.value.data == {"daemon_protocol": 2}

    @pytest.mark.parametrize(
        "code", ["protocol_mismatch", "internal"], ids=["mismatch", "other-error"]
    )
    def test_a_refused_hello_closes_the_connection(
        self, relay_paths: Paths, fake_relayd: Serve, code: str
    ) -> None:
        server = fake_relayd(lambda req: fail(req, code, "refused", {"daemon_protocol": 2}))
        client = RelayClient(relay_paths.socket)
        try:
            with pytest.raises(RelayError) as caught:
                client.connect(client="cli")
            assert caught.value.code == code

            # The client object is still alive, so only an explicit close can end the line.
            assert wait_until(lambda: server.hangups == 1, WAIT_S), "hello failed, socket kept"
        finally:
            client.close()

    def test_a_missing_socket_is_daemon_unavailable(self, relay_paths: Paths) -> None:
        client = RelayClient(relay_paths.socket)
        with pytest.raises(DaemonUnavailable):
            client.connect(client="cli")


class TestCall:
    def test_an_ok_reply_returns_its_result(self, relay_paths: Paths, fake_relayd: Serve) -> None:
        server = fake_relayd(hello_then(lambda req: ok(req, {"agents": [], "echo": req["args"]})))
        with closing(RelayClient(relay_paths.socket)) as client:
            client.connect(client="mcp")
            assert client.call("peers", {"token": "t1"}) == {"agents": [], "echo": {"token": "t1"}}
        rids = [req["rid"] for req in server.requests]
        assert all(isinstance(rid, int) for rid in rids)
        assert len(set(rids)) == len(rids)

    def test_an_error_reply_raises_relay_error_with_its_code(
        self, relay_paths: Paths, fake_relayd: Serve
    ) -> None:
        fake_relayd(hello_then(lambda req: fail(req, "not_allowed", "bob may not message carol")))
        with closing(RelayClient(relay_paths.socket)) as client:
            client.connect(client="mcp")
            with pytest.raises(RelayError) as caught:
                client.call("send", {"token": "t1"})
        assert caught.value.code == "not_allowed"
        assert caught.value.message == "bob may not message carol"

    def test_error_data_is_kept(self, relay_paths: Paths, fake_relayd: Serve) -> None:
        fake_relayd(hello_then(lambda req: fail(req, "turn_cap", "cap", {"context_id": "c1"})))
        with closing(RelayClient(relay_paths.socket)) as client:
            client.connect(client="mcp")
            with pytest.raises(RelayError) as caught:
                client.call("send", {})
        assert caught.value.data == {"context_id": "c1"}

    def test_the_server_closing_mid_call_is_daemon_unavailable(
        self, relay_paths: Paths, fake_relayd: Serve
    ) -> None:
        fake_relayd(hello_then(close_connection))
        with closing(RelayClient(relay_paths.socket)) as client:
            client.connect(client="mcp")
            with pytest.raises(DaemonUnavailable):
                client.call("inbox", {"token": "t1"})

    def test_no_reply_within_the_timeout_names_the_timeout(
        self, relay_paths: Paths, fake_relayd: Serve
    ) -> None:
        release = threading.Event()

        def hang(_req: Message) -> Message | None:
            release.wait(PROC_WAIT_S)
            return None

        fake_relayd(hello_then(hang))
        with closing(RelayClient(relay_paths.socket)) as client:
            try:
                client.connect(client="mcp")
                started = time.monotonic()
                with pytest.raises(RelayError, match=r"no reply within 0\.2 s"):
                    client.call("wait", {"token": "t1"}, timeout_s=0.2)
                assert time.monotonic() - started < 2.0
            finally:
                release.set()

    def test_recv_eof_returns_once_the_server_closes(
        self, relay_paths: Paths, fake_relayd: Serve
    ) -> None:
        server = fake_relayd(hello_then(close_connection))
        with closing(RelayClient(relay_paths.socket)) as client:
            client.connect(client="mcp")
            monitor = InThread(client.recv_eof)
            # Still blocked while the server keeps the connection open.
            time.sleep(0.3)
            assert not monitor.done()
            server.drop_connections()
            monitor.result(PROC_WAIT_S)
