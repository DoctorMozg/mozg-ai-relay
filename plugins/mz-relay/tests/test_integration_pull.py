"""End to end pull messaging: three `mz-relay mcp` processes and an auto-spawned relayd."""

from __future__ import annotations

import os
import re
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest
from mz_relay.cli import Terminal, main
from relay_rig import (
    WAIT_S,
    McpProc,
    daemon_pid,
    paths_in,
    pull_roster,
    socket_accepts,
    stop_daemon,
    wait_until,
)

NAMES = ("alice", "bob", "carol")
QUEUED = re.compile(r"queued (\S+) for (\S+)")
HELD_TEXT = "held {id} for user approval — ask your user to run: mz-relay approve {id}"


class Pull:
    def __init__(self, socket_path: Path, procs: dict[str, McpProc]) -> None:
        self.socket = socket_path
        self.procs = procs

    def send(self, sender: str, to: str, body: str, **extra: object) -> str:
        text = self.procs[sender].tool("send", to=to, body=body, **extra)
        match = QUEUED.fullmatch(text)
        assert match is not None, text
        assert match.group(2) == to
        return match.group(1)


@pytest.fixture
def pull(relay_home: Path, relay_env: dict[str, str]) -> Iterator[Pull]:
    paths = paths_in(relay_home)
    paths.roster.write_text(pull_roster(relay_home), encoding="utf-8")
    assert not paths.socket.exists()
    procs: dict[str, McpProc] = {}
    try:
        for name in NAMES:
            log = relay_home / f"mcp-{name}.log"
            procs[name] = McpProc(name, relay_env, cwd=relay_home, log=log)
            procs[name].initialize()
        yield Pull(paths.socket, procs)
    finally:
        for proc in procs.values():
            proc.close()
        stop_daemon(paths.socket)


@pytest.fixture
def user_cli(agent_free_env: dict[str, str]) -> Terminal:
    return Terminal(isatty=lambda: True, ask=lambda _: "y", env={})


class TestPullMessaging:
    def test_first_mcp_client_spawned_relayd(self, pull: Pull) -> None:
        assert socket_accepts(pull.socket)
        pid = daemon_pid(pull.socket)
        assert pid is not None
        assert pid not in {proc.proc.pid for proc in pull.procs.values()}

    def test_message_is_pulled_acked_and_answered(self, pull: Pull) -> None:
        alice, bob = pull.procs["alice"], pull.procs["bob"]
        msg_id = pull.send("alice", "bob", "please review src/app.py")

        inbox = bob.tool("inbox")
        assert "Peer message from alice (role:" in inbox
        assert "  please review src/app.py" in inbox
        assert bob.tool("ack", id=msg_id) == f"acked {msg_id}"
        assert f"- {msg_id} → bob: acked" in alice.tool("inbox").splitlines()

        pull.send("bob", "alice", "looks good", kind="reply", in_reply_to=msg_id)
        answer = alice.tool("inbox")

        assert f"This answers {msg_id}." in answer
        assert f"context: {msg_id}  hop: 1" in answer

    def test_corporate_message_waits_for_user_approval(
        self, pull: Pull, user_cli: Terminal, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alice, carol = pull.procs["alice"], pull.procs["carol"]
        text = carol.tool("send", to="alice", body="corporate diff summary")
        match = re.fullmatch(r"held (\S+) for user approval.*", text)
        assert match is not None, text
        held_id = match.group(1)
        assert text == HELD_TEXT.format(id=held_id)
        assert "Peer message from carol" not in alice.tool("inbox")

        assert main(["approve", held_id], terminal=user_cli) == 0

        assert f"approved {held_id}" in capsys.readouterr().out
        inbox = alice.tool("inbox")
        assert "Peer message from carol (role:" in inbox
        assert "  corporate diff summary" in inbox

    def test_state_survives_kill_9_of_relayd(self, pull: Pull) -> None:
        alice, bob = pull.procs["alice"], pull.procs["bob"]
        msg_id = pull.send("alice", "bob", "please review src/app.py")
        bob.tool("inbox")
        bob.tool("ack", id=msg_id)
        old_pid = daemon_pid(pull.socket)
        assert old_pid is not None

        os.kill(old_pid, signal.SIGKILL)
        assert wait_until(lambda: not socket_accepts(pull.socket), WAIT_S)
        bob.tool("inbox")

        new_pid = daemon_pid(pull.socket)
        assert new_pid is not None
        assert new_pid != old_pid
        acked_line = f"- {msg_id} → bob: acked"
        # alice's presence re-registers on its own backoff, so give it a few seconds.
        assert wait_until(lambda: acked_line in _inbox_or_empty(alice), WAIT_S, 0.05)


def _inbox_or_empty(proc: McpProc) -> str:
    result = proc.tool_result("inbox")
    return "" if result.get("isError") else str(result["content"][0]["text"])
