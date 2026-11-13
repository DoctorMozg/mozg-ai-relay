"""The `wait` op ends with {"timeout": true} when relayd stops, instead of outliving the drain."""

from __future__ import annotations

import json
import os
import time

from mz_relay.paths import Paths
from relay_rig import InThread, RawConn, ServiceRig, running_daemon

# Well under relayd's 2 s drain, so a reply only arrives this soon if `wait` noticed the stop.
PROMPT_S = 1.5
LONG_WAIT_S = 60
# Lets the waiting call reach its condition wait before the stop arrives.
PARK_S = 0.2


def wait_args(token: str) -> dict[str, object]:
    return {"token": token, "session_id": "s-1", "timeout_s": LONG_WAIT_S}


class TestServiceWait:
    def test_shutdown_op_ends_a_parked_wait_with_a_timeout(self, rig: ServiceRig) -> None:
        _, bob = rig.connect("bob")
        waiting = InThread(lambda: rig.call(rig.hello(client="hook"), "wait", **wait_args(bob)))
        time.sleep(PARK_S)

        rig.call(rig.hello(client="cli"), "shutdown")

        assert waiting.result(PROMPT_S) == {"timeout": True}

    def test_wait_after_shutdown_returns_a_timeout_at_once(self, rig: ServiceRig) -> None:
        _, bob = rig.connect("bob")
        rig.call(rig.hello(client="cli"), "shutdown")

        waiting = InThread(lambda: rig.call(rig.hello(client="hook"), "wait", **wait_args(bob)))

        assert waiting.result(PROMPT_S) == {"timeout": True}


def register_bob(conn: RawConn) -> str:
    args = {"name": "bob", "harness": "opencode", "cwd": "/tmp", "pid": os.getpid()}
    token = conn.request("register", args, rid=2)["result"]["token"]
    assert isinstance(token, str)
    return token


def park_wait(conn: RawConn) -> None:
    """Registers bob and writes a long wait request without reading its reply."""
    conn.hello()
    frame = {"v": 1, "rid": 3, "op": "wait", "args": wait_args(register_bob(conn))}
    conn.send_line(json.dumps(frame).encode() + b"\n")
    time.sleep(PARK_S)


class TestDaemonWait:
    def test_request_stop_answers_a_parked_wait_with_a_timeout(self, relay_paths: Paths) -> None:
        with running_daemon(relay_paths) as daemon, RawConn(relay_paths.socket) as conn:
            park_wait(conn)
            daemon.request_stop()
            conn.sock.settimeout(PROMPT_S)

            reply = conn.reply()

        assert (reply["rid"], reply["ok"], reply["result"]) == (3, True, {"timeout": True})

    def test_shutdown_op_answers_a_parked_wait_with_a_timeout(self, relay_paths: Paths) -> None:
        with running_daemon(relay_paths), RawConn(relay_paths.socket) as conn:
            park_wait(conn)
            with RawConn(relay_paths.socket) as admin:
                admin.hello()
                admin.request("shutdown", {}, rid=2)
            conn.sock.settimeout(PROMPT_S)

            reply = conn.reply()

        assert (reply["rid"], reply["ok"], reply["result"]) == (3, True, {"timeout": True})
