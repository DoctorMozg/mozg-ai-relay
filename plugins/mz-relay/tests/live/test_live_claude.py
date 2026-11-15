"""Live smoke: two real Claude accounts message each other, and the read_only gate holds.
Run by hand: MZ_RELAY_LIVE_CLAUDE_DIRS="<cfgA>:<cfgB>" uv run make live-claude"""

from __future__ import annotations

import json
import secrets
import subprocess
import warnings
from collections.abc import Mapping
from pathlib import Path

import pytest
from fakes_claude import claude_agent
from relay_rig import BIN, online_names, paths_in, relayd_status, stop_daemon, wait_until
from stream_claude import StreamJsonClaude

START_S = 180.0
PEER_LINE_S = 60.0
TURN_S = 240.0
POLL_S = 0.5
OWN_TOOLS = ",".join(
    f"mcp__plugin_mz-relay_mz-relay__{tool}" for tool in ("send", "inbox", "ack", "peers")
)
GATE_DENIAL = "is blocked because a peer message is part of this turn"
HOOK_S = 30.0


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


def run_hook(args: list[str], payload: Mapping[str, object], env: dict[str, str]) -> str:
    """One `mz-relay hook ...` call as Claude makes it: JSON on stdin, its stdout back."""
    done = subprocess.run(
        [str(BIN), "hook", *args],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=HOOK_S,
        check=True,
    )
    return done.stdout


def prompt_ends(transcript: Path) -> dict[str, tuple[str, int]]:
    """The first user line holding each word: its promptId and the offset just past it."""
    from mz_relay.adapters.claude_transcript import Transcript

    found: dict[str, tuple[str, int]] = {}
    for end, line in Transcript(transcript).lines_back(0):
        owner = line.get("promptId")
        message = line.get("message")
        # Raw content: classify() leaves SDK prompts (turnOrigin "sdk") without text.
        content = json.dumps(message.get("content")) if isinstance(message, dict) else ""
        for word in ("READY", "PING-"):
            if line.get("type") == "user" and isinstance(owner, str) and word in content:
                found[word] = (owner, end)  # newest first, so the oldest line wins
    return found


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

        # (d) turn 1's Stop, replayed after the peer turn replied, records turn 1's own end
        # by prompt_id, so the peer turn stays gated; without prompt_id it would be ungated.
        from mz_relay.adapters.claude_transcript import TranscriptWatcher
        from mz_relay.hooks import read_offset

        transcript = TranscriptWatcher(cfg_b, b_session).path()
        assert transcript is not None
        prompts = prompt_ends(transcript)
        assert {"READY", "PING-"} <= prompts.keys(), f"prompt lines without promptId: {prompts}"
        ready_id, _ = prompts["READY"]
        peer_id, peer_end = prompts["PING-"]
        assert ready_id != peer_id
        turn_end_file = paths.turn_end_dir / b_session

        def own_stop_recorded() -> bool:
            end = read_offset(turn_end_file)
            return end is not None and end > peer_end

        assert wait_until(own_stop_recorded, HOOK_S, POLL_S), "the peer turn's Stop never ran"
        hook_env = {**relay_env, "CLAUDE_CONFIG_DIR": str(cfg_b), "MZ_RELAY_NAME": "liveb"}
        base = {"session_id": b_session, "cwd": str(work_b), "transcript_path": str(transcript)}
        write = {**base, "hook_event_name": "PreToolUse", "tool_name": "Write"}
        stop = {**base, "hook_event_name": "Stop"}

        # B's waiter from an earlier Stop holds the lock, so each replay records and exits.
        run_hook(["wait", "Stop"], stop, hook_env)
        assert (read_offset(turn_end_file) or 0) > peer_end
        assert run_hook(["gate"], write, hook_env) == ""

        run_hook(["wait", "Stop"], {**stop, "prompt_id": ready_id}, hook_env)
        assert (read_offset(turn_end_file) or 0) < peer_end
        assert '"permissionDecision": "deny"' in run_hook(["gate"], write, hook_env)
    finally:
        for session in sessions:
            session.close()
        stop_daemon(paths.socket)
