"""Live smoke: a real Pi loads the mz-relay extension, gets peer messages, keeps a read_only role.
Run by hand: MZ_RELAY_LIVE_PI="pi -ne --no-session <model flags>" uv run make live-pi
Pi runs with the real HOME from the test environment, so it uses the user's own Pi settings."""

from __future__ import annotations

import contextlib
import secrets
import time
from pathlib import Path

import pytest
from live_rig import (
    BOARD_ASK,
    BOARD_EXTENSION,
    POLL_S,
    SETTLE_S,
    START_S,
    TURN_S,
    Architect,
    RelayRows,
    TmuxPane,
)
from relay_rig import PLUGIN, online_names, paths_in, relayd_status, running_daemon, wait_until

# Lets the user's bash call start before the peer message arrives.
BUSY_LEAD_S = 5.0
ADAPTER = PLUGIN / "adapters" / "pi" / "mz_relay.ts"

ROSTER = (
    """\
version = 1

[policy]
hold_corporate_to_personal = false

[agents.architect]
harness = "opencode"
label = "personal"
role = "architect"
description = "Plans the work; a test client with tools only."

[agents.scout]
harness = "pi"
label = "personal"
role = "scout"
description = "Reads code and answers questions."
tools = "read_only"
"""
    + BOARD_EXTENSION
)


@pytest.mark.live_pi
def test_pi_gets_peer_messages_and_a_read_only_peer_run_cannot_write(
    live_pi_argv: list[str], tmux_bin: str, relay_home: Path, relay_env: dict[str, str]
) -> None:
    """If the extension's `context` event did not fire before the LLM call that follows a
    mid-run delivery, the busy row would stay handed_off and the gate would never close."""
    from mz_relay.adapters import AdapterContext, build_adapters
    from mz_relay.clock import SystemClock

    nonce = secrets.token_hex(4)
    paths = paths_in(relay_home)
    work = relay_home / "work"
    work.mkdir()
    ctx = AdapterContext(paths=paths, clock=SystemClock(), pw_home=paths.pw_home, base_env={})
    # The stack unwinds before the daemon stops, so Pi never runs against a dead relayd.
    with (
        running_daemon(paths, build_adapters(ctx), roster_text=ROSTER),
        contextlib.ExitStack() as stack,
    ):
        env = {**relay_env, "MZ_RELAY_NAME": "scout"}
        argv = [*live_pi_argv, "-e", str(ADAPTER)]
        pane = stack.enter_context(TmuxPane(tmux_bin, argv, cwd=work, env=env))
        rows = stack.enter_context(RelayRows(paths.db))
        registered = wait_until(
            lambda: "scout" in online_names(relayd_status(paths.socket)), START_S, POLL_S
        )
        assert registered, f"scout never came online; pane:\n{pane.screen()}"
        architect = stack.enter_context(
            Architect(paths.socket, work, harness="opencode", target="scout")
        )

        # (a) a peer message is handed to Pi and confirmed through `seen`
        ping = architect.send(f"Reply with the single word PONG-{nonce}.", kind="inform")
        arrived = wait_until(lambda: rows.arrived_via("architect", ping, "pi"), TURN_S, POLL_S)
        assert arrived, f"{rows.row('architect', ping)}; pane:\n{pane.screen()}"

        # (b) a read_only peer run may use the read-only board tools relayd gave it. It runs
        # before any write request, after which the model holds every peer request for the user.
        marker = architect.seed_board()
        architect.send(BOARD_ASK)
        assert wait_until(lambda: rows.sent_with("scout", marker), TURN_S, POLL_S), pane.screen()

        # (c) busy: the user's own bash call runs while a peer message arrives mid-run.
        # Before any blocked peer run: a weak local model then refuses the user's bash too.
        user_file = work / f"user-{nonce}.txt"
        pane.send_line(f"Use your bash tool to run exactly: sleep 20 && touch {user_file.name}")
        time.sleep(BUSY_LEAD_S)
        busy_gate = work / f"busy-gate-{nonce}.txt"
        busy = architect.send(f"Use your bash tool to run: touch {busy_gate.name}")
        assert wait_until(user_file.exists, TURN_S, POLL_S), pane.screen()
        assert wait_until(lambda: rows.arrived_via("architect", busy, "pi"), TURN_S, POLL_S)
        assert not wait_until(busy_gate.exists, SETTLE_S, POLL_S), pane.screen()

        # (d) a read_only peer run cannot write
        gate = work / f"gate-{nonce}.txt"
        ask = architect.send(f"Use your bash tool to run: touch {gate.name}")
        assert wait_until(lambda: rows.arrived_via("architect", ask, "pi"), TURN_S, POLL_S)
        assert not wait_until(gate.exists, SETTLE_S, POLL_S), pane.screen()

        # (e) the brief told the agent its name
        pane.send_line("What is your mz-relay name? Answer with that name in capital letters only.")
        assert pane.wait_for("SCOUT", TURN_S), pane.screen()
