"""Live smoke: relayd queues a peer message into a real Codex thread and finds it in the rollout.
Run by hand: MZ_RELAY_LIVE_CODEX_HOME=<logged-in codex home> uv run make live-codex
Codex runs in that real CODEX_HOME, with the mz-relay snippet passed as `-c` flags rather than
written to its config.toml; the test deletes every thread started in its work dir afterwards."""

from __future__ import annotations

import contextlib
import secrets
import sys
import time
import tomllib
from pathlib import Path

import pytest
from live_rig import (
    BOARD_ASK,
    BOARD_EXTENSION,
    POLL_S,
    START_S,
    TURN_S,
    Architect,
    RelayRows,
    TmuxPane,
    codex_threads_in,
    delete_codex_thread,
)
from relay_rig import PLUGIN, paths_in, running_daemon, wait_until

sys.path.insert(0, str(PLUGIN / "scripts"))

from demo import CODEX_ARGV, CODEX_STARTUP_PROMPTS
from mz_relay.install import MCP_SERVER_NAME, codex_snippet, toml_string

# The TUI counts as ready once its screen stops changing for this long.
TUI_QUIET_S = 3.0
# Lets the TUI finish completing the slash command before Enter.
RENAME_PAUSE_S = 1.0
ROLLOUT_S = 60.0
HEADER = "[mz-relay] Peer message from architect"
AGENT = "reviewer"


def toml_value(value: object) -> str:
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"no TOML form for {type(value).__name__} in the snippet")


def snippet_flags(name: str) -> list[str]:
    """`codex -c` flags carrying `mz-relay snippet codex` for this run; config.toml stays as is."""
    snippet = tomllib.loads(codex_snippet(PLUGIN / "bin" / "mz-relay", name))
    flags: list[str] = []
    for key, value in snippet["mcp_servers"][MCP_SERVER_NAME].items():
        flags += ["-c", f"mcp_servers.{MCP_SERVER_NAME}.{key}={toml_value(value)}"]
    return flags


def live_roster(name: str, codex_home: Path, thread: str, codex_bin: str) -> str:
    return (
        f"""\
version = 1

[policy]
hold_corporate_to_personal = false

[agents.architect]
harness = "pi"
label = "personal"
role = "architect"
description = "Plans the work; a test client with tools only."

[agents.{name}]
harness = "codex"
label = "personal"
role = "reviewer"
description = "Reviews changes in a Codex thread."
codex_home = "{codex_home}"
codex_thread = "{thread}"
codex_bin = "{codex_bin}"
"""
        + BOARD_EXTENSION
    )


@pytest.mark.live_codex
def test_a_peer_message_is_queued_into_a_renamed_codex_thread(
    live_codex_home: Path,
    codex_bin: str,
    tmux_bin: str,
    relay_home: Path,
    relay_env: dict[str, str],
) -> None:
    """A trust prompt would hold the TUI; the demo's workdir showed one, this one none (0.169)."""
    from mz_relay.adapters import AdapterContext, build_adapters
    from mz_relay.adapters.codex_threads import (
        CodexThreadMissing,
        find_rollout,
        resolve_thread,
        scan_rollout,
    )
    from mz_relay.clock import SystemClock

    name = AGENT
    thread_name = f"mzr-live-{secrets.token_hex(4)}"
    paths = paths_in(relay_home)
    work = relay_home / "work"
    work.mkdir()
    env = {**relay_env, "CODEX_HOME": str(live_codex_home)}
    ctx = AdapterContext(paths=paths, clock=SystemClock(), pw_home=paths.pw_home, base_env=env)
    roster = live_roster(name, live_codex_home, thread_name, codex_bin)
    thread_ids: list[str] = []

    def renamed() -> bool:
        try:
            thread_ids.append(resolve_thread(live_codex_home, thread_name).id)
        except CodexThreadMissing:
            return False
        return True

    started = time.time()

    def delete_threads() -> None:
        # Besides the renamed thread, Codex starts review threads of its own in the work dir.
        in_work = codex_threads_in(live_codex_home, work, started)
        for thread_id in dict.fromkeys([*thread_ids, *in_work]):
            delete_codex_thread(codex_bin, thread_id, env)

    with (
        running_daemon(paths, build_adapters(ctx), roster_text=roster),
        contextlib.ExitStack() as stack,
    ):
        # Registered first, so it runs last: after the pane has closed Codex.
        stack.callback(delete_threads)
        argv = [codex_bin, *CODEX_ARGV[1:], *snippet_flags(name)]
        pane = stack.enter_context(TmuxPane(tmux_bin, argv, cwd=work, env=env))
        rows = stack.enter_context(RelayRows(paths.db))
        assert pane.wait_quiet(TUI_QUIET_S, START_S), f"the TUI never settled:\n{pane.screen()}"
        for _ in CODEX_STARTUP_PROMPTS:
            screen = pane.screen()
            shown = [p for p in CODEX_STARTUP_PROMPTS if all(m in screen for m in p.markers)]
            if not shown:
                break
            pane.send_key(shown[0].key)
            assert pane.wait_quiet(TUI_QUIET_S, START_S), pane.screen()

        pane.send_line(f"/rename {thread_name}", pause_s=RENAME_PAUSE_S)
        took = wait_until(renamed, START_S, POLL_S)
        assert took, f"/rename {thread_name} never reached session_index.jsonl:\n{pane.screen()}"
        found = thread_ids[0]

        architect = stack.enter_context(Architect(paths.socket, work, harness="pi", target=name))
        msg_id = architect.send("Reply with the single word PONG.", kind="inform")

        def in_rollout() -> bool:
            path = find_rollout(live_codex_home, found)
            return path is not None and scan_rollout(path, msg_id, 0).found

        assert wait_until(in_rollout, ROLLOUT_S, POLL_S), pane.screen()
        arrived = wait_until(lambda: rows.arrived_via("architect", msg_id, "codex"), TURN_S, POLL_S)
        assert arrived, rows.row("architect", msg_id)
        rollout = find_rollout(live_codex_home, found)
        assert rollout is not None
        lines = [
            line for line in rollout.read_text(encoding="utf-8").splitlines() if msg_id in line
        ]
        assert any(HEADER in line for line in lines), f"no rollout line with {HEADER!r}: {lines}"

        # A Codex session reaches the board tools through the same MCP server.
        marker = architect.seed_board()
        architect.send(BOARD_ASK)
        assert wait_until(lambda: rows.sent_with(name, marker), TURN_S, POLL_S), pane.screen()
