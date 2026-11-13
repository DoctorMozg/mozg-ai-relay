"""Live smoke: two real Claude accounts message each other, and the read_only gate holds.
Run by hand: MZ_RELAY_LIVE_CLAUDE_DIRS="<cfgA>:<cfgB>" uv run make live-claude"""

from __future__ import annotations

import contextlib
import json
import secrets
import subprocess
import threading
import time
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import IO

import pytest
from fakes_claude import claude_agent
from relay_rig import (
    PLUGIN,
    Message,
    online_names,
    paths_in,
    reap,
    relayd_status,
    stop_daemon,
    wait_until,
)

START_S = 180.0
PEER_LINE_S = 60.0
TURN_S = 240.0
POLL_S = 0.5
OWN_TOOLS = ",".join(
    f"mcp__plugin_mz-relay_mz-relay__{tool}" for tool in ("send", "inbox", "ack", "peers")
)
GATE_DENIAL = "is blocked because a peer message is part of this turn"


def live_roster(cfg_a: Path, cfg_b: Path) -> str:
    """`livea` (personal, full tools) and `liveb` (corporate, read_only), with no hold rule."""
    return (
        "version = 1\n\n[policy]\nhold_corporate_to_personal = false\n"
        + claude_agent(cfg_a, name="livea")
        + claude_agent(cfg_b, name="liveb", label="corporate", tools="read_only")
    )


class StreamJsonClaude:
    """`claude -p` in stream-json mode with this plugin loaded; prompts go in on stdin."""

    def __init__(
        self, claude: str, env: Mapping[str, str], *, cwd: Path, log: Path, extra: list[str]
    ) -> None:
        self.events: list[Message] = []
        # results() re-enters the lock from inside wait_results(), so it must be reentrant.
        self._changed = threading.Condition(threading.RLock())
        self._log = log.open("wb")
        argv = [
            claude,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--plugin-dir",
            str(PLUGIN),
            *extra,
        ]
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                env=dict(env),
                cwd=cwd,
            )
        except BaseException:
            self._log.close()
            raise
        threading.Thread(target=self._read, args=(self.proc.stdout,), daemon=True).start()

    def _read(self, stream: IO[bytes] | None) -> None:
        assert stream is not None
        for raw in iter(stream.readline, b""):
            with contextlib.suppress(ValueError):
                event = json.loads(raw)
                if isinstance(event, dict):
                    with self._changed:
                        self.events.append(event)
                        self._changed.notify_all()

    def prompt(self, text: str) -> None:
        assert self.proc.stdin is not None
        line = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(line).encode() + b"\n")
        self.proc.stdin.flush()

    def results(self) -> int:
        with self._changed:
            return sum(1 for event in self.events if event.get("type") == "result")

    def wait_results(self, count: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._changed:
            while self.results() < count:
                left = deadline - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return False
                self._changed.wait(min(left, 1.0))
            return True

    def session_id(self) -> str | None:
        with self._changed:
            for event in self.events:
                if event.get("type") == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    return sid if isinstance(sid, str) else None
        return None

    def saw(self, text: str) -> bool:
        """True when any stream-json event so far contains `text`."""
        with self._changed:
            return any(text in json.dumps(event) for event in self.events)

    def close(self) -> None:
        try:
            reap(self.proc)
        finally:
            self._log.close()


@pytest.fixture
def sessions() -> Iterator[list[StreamJsonClaude]]:
    started: list[StreamJsonClaude] = []
    yield started
    for session in started:
        session.close()


def peer_line_with(cfg: Path, session_id: str, needle: str) -> bool:
    """True once the session's transcript has a line classified MZ_PEER that holds `needle`."""
    from mz_relay.adapters.claude_transcript import LineKind, TranscriptWatcher, classify

    for line in TranscriptWatcher(cfg, session_id).lines_after(0):
        classified = classify(line)
        if classified.kind is LineKind.MZ_PEER and needle in classified.content:
            return True
    return False


@pytest.mark.live_claude
def test_two_claude_accounts_exchange_a_message_and_the_read_only_gate_holds(
    live_claude_dirs: tuple[Path, Path],
    claude_bin: str,
    relay_home: Path,
    relay_env: dict[str, str],
    sessions: list[StreamJsonClaude],
) -> None:
    """UNTESTED: asyncRewake hooks may run in the foreground under `claude -p`, so the SessionStart
    waiter could hang a session; that shows up here as a start or turn timeout."""
    from mz_relay.envelope import Status
    from mz_relay.store import Store

    cfg_a, cfg_b = live_claude_dirs
    nonce = secrets.token_hex(4)
    paths = paths_in(relay_home)
    paths.roster.write_text(live_roster(cfg_a, cfg_b), encoding="utf-8")
    work_a, work_b = relay_home / "a", relay_home / "b"
    work_a.mkdir()
    work_b.mkdir()

    def start(name: str, cfg: Path, cwd: Path, extra: list[str]) -> StreamJsonClaude:
        env = {**relay_env, "CLAUDE_CONFIG_DIR": str(cfg), "MZ_RELAY_NAME": name}
        session = StreamJsonClaude(
            claude_bin, env, cwd=cwd, log=relay_home / f"{name}.log", extra=extra
        )
        sessions.append(session)
        return session

    try:
        # acceptEdits lets B write in its folder, so only the gate can stop the peer's request.
        b = start(
            "liveb",
            cfg_b,
            work_b,
            ["--permission-mode", "acceptEdits", "--allowedTools", OWN_TOOLS],
        )
        b.prompt("Reply with the single word READY.")
        assert b.wait_results(1, START_S), "liveb did not finish its first turn"
        a = start("livea", cfg_a, work_a, ["--allowedTools", OWN_TOOLS])
        both_online = wait_until(
            lambda: {"livea", "liveb"} <= online_names(relayd_status(paths.socket)), START_S, POLL_S
        )
        assert both_online, f"livea and liveb not both online: {relayd_status(paths.socket)}"
        b_session = b.session_id()
        assert b_session is not None, "liveb's stream-json output had no init session_id"

        a.prompt(
            f"Use the mz-relay send tool to send liveb the text PING-{nonce}. "
            f"Ask liveb to create the file gate-{nonce}.txt"
        )
        assert a.wait_results(1, START_S), "livea did not finish its send turn"

        # (a) a real MZ_PEER line with the nonce in B's transcript
        ping = f"PING-{nonce}"
        assert wait_until(lambda: peer_line_with(cfg_b, b_session, ping), PEER_LINE_S, POLL_S)

        # (b) relayd delivered it through Claude's inbox socket
        store = Store(paths.db)
        try:
            rows = [row for row in store.sent_by("livea") if f"PING-{nonce}" in row.envelope.body]
        finally:
            store.close()
        assert len(rows) == 1, rows
        assert rows[0].via == "claude"
        assert rows[0].status in {Status.DELIVERED, Status.ACKED}

        # (c) the read_only gate left no file once B's peer turn ended
        assert b.wait_results(2, TURN_S), "liveb's peer turn did not end"
        assert not (work_b / f"gate-{nonce}.txt").exists()
        assert not (work_a / f"gate-{nonce}.txt").exists()
        # A model that refuses on its own also leaves no file, so say whether the gate fired.
        if not b.saw(GATE_DENIAL):
            warnings.warn("no gate denial in liveb's output; (c) passed without it", stacklevel=1)
    finally:
        for session in sessions:
            session.close()
        stop_daemon(paths.socket)
