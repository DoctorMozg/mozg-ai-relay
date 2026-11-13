"""`mz-relay mcp` over real pipes: the Claude Code 2.1.323 handshake byte for byte."""

from __future__ import annotations

import json
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from relay_rig import (
    PROC_WAIT_S,
    WAIT_S,
    FakeRelayd,
    McpProc,
    Message,
    Reply,
    hello_then,
    ok,
    paths_in,
    pull_roster,
    stop_daemon,
    token,
    wait_until,
)

# The first frame Claude Code 2.1.323 sends; a -32601 reply makes it fall back to initialize.
DISCOVER = (
    b'{"jsonrpc":"2.0","id":"server-discover-probe-1","method":"server/discover",'
    b'"params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}\n'
)
INITIALIZE = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25",'
    b'"capabilities":{},"clientInfo":{"name":"claude-code","version":"2.1.323"}}}\n'
)
INITIALIZED = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
TOOLS_LIST = b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'


@pytest.fixture
def pipes(relay_home: Path, relay_env: dict[str, str]) -> Iterator[McpProc]:
    paths = paths_in(relay_home)
    paths.roster.write_text(pull_roster(relay_home), encoding="utf-8")
    proc = McpProc("alice", relay_env, cwd=relay_home, log=relay_home / "mcp.log")
    try:
        yield proc
    finally:
        proc.close()
        stop_daemon(paths.socket)


def assert_json_rpc_lines(lines: list[bytes]) -> None:
    for raw in lines:
        assert raw.endswith(b"\n"), raw
        frame = json.loads(raw.decode("utf-8"))
        assert frame["jsonrpc"] == "2.0", raw
        assert ("result" in frame) != ("error" in frame), raw


class TestStdioHandshake:
    def test_claude_handshake_then_clean_exit_on_stdin_eof(self, pipes: McpProc) -> None:
        pipes.write(DISCOVER)
        discover = pipes.read()
        assert discover["id"] == "server-discover-probe-1"
        assert discover["error"]["code"] == -32601

        pipes.write(INITIALIZE)
        init = pipes.read()
        assert init["id"] == 1
        assert init["result"]["protocolVersion"] == "2025-11-25"
        assert init["result"]["serverInfo"]["name"] == "mz-relay"
        assert 'You are "alice"' in init["result"]["instructions"]

        pipes.write(INITIALIZED)
        pipes.write(TOOLS_LIST)
        # The notification got no reply, so the next line answers tools/list.
        tools = pipes.read()
        assert tools["id"] == 2
        assert [t["name"] for t in tools["result"]["tools"]] == ["send", "inbox", "ack", "peers"]

        assert pipes.finish() == 0
        pipes.drain()
        assert len(pipes.seen) == 3
        assert_json_rpc_lines(pipes.seen)

    def test_sigterm_stops_the_server(self, pipes: McpProc) -> None:
        pipes.write(INITIALIZE)
        assert pipes.read()["id"] == 1

        pipes.proc.send_signal(signal.SIGTERM)

        pipes.proc.wait(timeout=PROC_WAIT_S)
        pipes.drain()
        assert_json_rpc_lines(pipes.seen)


class TestSigtermDuringToolCall:
    def test_sigterm_while_a_tool_call_waits_on_relayd_ends_the_process(
        self,
        relay_home: Path,
        relay_env: dict[str, str],
        fake_relayd: Callable[[Reply], FakeRelayd],
    ) -> None:
        release = threading.Event()

        def answer(req: Message) -> Message | None:
            if req["op"] == "register":
                card = {"name": "alice", "role": "planner", "harness": "opencode"}
                return ok(
                    req,
                    {"token": token(1), "agent": card, "delivery_mode": None, "brief": "brief"},
                )
            # Every tool call hangs here until teardown, like a relayd that stopped answering.
            release.wait(PROC_WAIT_S)
            return None

        relayd = fake_relayd(hello_then(answer))
        mcp = McpProc("alice", relay_env, cwd=relay_home, log=relay_home / "mcp.log")
        try:
            mcp.initialize()
            mcp.write(b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"peers"}}\n')
            assert wait_until(lambda: "peers" in relayd.ops(), WAIT_S), (
                "the call never reached relayd"
            )

            mcp.proc.send_signal(signal.SIGTERM)

            try:
                mcp.proc.wait(timeout=WAIT_S)
            except subprocess.TimeoutExpired:
                pytest.fail(f"mz-relay mcp still running {WAIT_S} s after SIGTERM")
            mcp.drain()
            assert not [line for line in mcp.seen if b'"error"' in line], mcp.seen
        finally:
            release.set()
            mcp.close()
