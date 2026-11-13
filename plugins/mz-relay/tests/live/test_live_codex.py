"""Live smoke: relayd queues a peer message into a real Codex thread and finds it in the rollout.
Run by hand: MZ_RELAY_LIVE_CODEX_HOME=<codex home with the mz-relay snippet> uv run make live-codex
Codex runs in that real CODEX_HOME; the test deletes the thread it renamed afterwards."""

from __future__ import annotations

import contextlib
import secrets
import tomllib
from pathlib import Path

import pytest
from live_rig import (
    POLL_S,
    START_S,
    TURN_S,
    Architect,
    RelayRows,
    TmuxPane,
    delete_codex_thread,
)
from relay_rig import paths_in, running_daemon, wait_until

# The TUI counts as ready once its screen stops changing for this long.
TUI_QUIET_S = 3.0
# Lets the TUI finish completing the slash command before Enter.
RENAME_PAUSE_S = 1.0
ROLLOUT_S = 60.0
HEADER = "[mz-relay] Peer message from architect"


def configured_name(codex_home: Path) -> str:
    """The agent name the mz-relay snippet in config.toml gives; skips when it is not set up."""
    config = codex_home / "config.toml"
    if not config.is_file():
        pytest.skip(f"{config} does not exist; paste `mz-relay snippet codex` into it")
    server = tomllib.loads(config.read_text(encoding="utf-8")).get("mcp_servers", {})
    name = server.get("mz-relay", {}).get("env", {}).get("MZ_RELAY_NAME")
    if not isinstance(name, str) or not name:
        pytest.skip(f"{config} has no [mcp_servers.mz-relay] with env MZ_RELAY_NAME")
    return name


def live_roster(name: str, codex_home: Path, thread: str, codex_bin: str) -> str:
    return f"""\
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


@pytest.mark.live_codex
def test_a_peer_message_is_queued_into_a_renamed_codex_thread(
    live_codex_home: Path,
    codex_bin: str,
    tmux_bin: str,
    relay_home: Path,
    relay_env: dict[str, str],
) -> None:
    """UNTESTED: a first start in an untrusted folder may show a trust prompt that holds the TUI."""
    from mz_relay.adapters import AdapterContext, build_adapters
    from mz_relay.adapters.codex_threads import (
        CodexThreadMissing,
        find_rollout,
        resolve_thread,
        rollout_has_nonce,
    )
    from mz_relay.clock import SystemClock

    name = configured_name(live_codex_home)
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

    def delete_thread() -> None:
        for thread_id in thread_ids:
            delete_codex_thread(codex_bin, thread_id, env)

    with (
        running_daemon(paths, build_adapters(ctx), roster_text=roster),
        contextlib.ExitStack() as stack,
    ):
        # Registered first, so it runs last: after the pane has closed Codex.
        stack.callback(delete_thread)
        pane = stack.enter_context(TmuxPane(tmux_bin, [codex_bin], cwd=work, env=env))
        rows = stack.enter_context(RelayRows(paths.db))
        assert pane.wait_quiet(TUI_QUIET_S, START_S), f"the TUI never settled:\n{pane.screen()}"

        pane.send_line(f"/rename {thread_name}", pause_s=RENAME_PAUSE_S)
        took = wait_until(renamed, START_S, POLL_S)
        assert took, f"/rename {thread_name} never reached session_index.jsonl:\n{pane.screen()}"
        found = thread_ids[0]

        architect = stack.enter_context(Architect(paths.socket, work, harness="pi", target=name))
        msg_id = architect.send("Reply with the single word PONG.", kind="inform")

        def in_rollout() -> bool:
            path = find_rollout(live_codex_home, found)
            return path is not None and rollout_has_nonce(path, msg_id)

        assert wait_until(in_rollout, ROLLOUT_S, POLL_S), pane.screen()
        arrived = wait_until(lambda: rows.arrived_via("architect", msg_id, "codex"), TURN_S, POLL_S)
        assert arrived, rows.row("architect", msg_id)
        rollout = find_rollout(live_codex_home, found)
        assert rollout is not None
        lines = [
            line for line in rollout.read_text(encoding="utf-8").splitlines() if msg_id in line
        ]
        assert any(HEADER in line for line in lines), f"no rollout line with {HEADER!r}: {lines}"
