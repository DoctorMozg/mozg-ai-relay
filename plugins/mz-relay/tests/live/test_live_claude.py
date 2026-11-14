"""Live smoke: two real Claude accounts message each other, and the read_only gate holds.
Run by hand: MZ_RELAY_LIVE_CLAUDE_DIRS="<cfgA>:<cfgB>" uv run make live-claude"""

from __future__ import annotations

import secrets
import warnings
from pathlib import Path

import pytest
from fakes_claude import claude_agent
from relay_rig import online_names, paths_in, relayd_status, stop_daemon, wait_until
from stream_claude import StreamJsonClaude

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
    """asyncRewake hooks under `claude -p` did not hang a session on 2.1.325; a SessionStart
    waiter run in the foreground would show up here as a start or turn timeout."""
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
        assert b.wait_results(1, START_S), f"liveb did not finish its first turn\n{b.diagnose()}"
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
        assert a.wait_results(1, START_S), f"livea did not finish its send turn\n{a.diagnose()}"

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
        assert b.wait_results(2, TURN_S), f"liveb's peer turn did not end\n{b.diagnose()}"
        assert not (work_b / f"gate-{nonce}.txt").exists()
        assert not (work_a / f"gate-{nonce}.txt").exists()
        # A model that refuses on its own also leaves no file, so say whether the gate fired.
        if not b.saw(GATE_DENIAL):
            warnings.warn("no gate denial in liveb's output; (c) passed without it", stacklevel=1)
    finally:
        for session in sessions:
            session.close()
        stop_daemon(paths.socket)
