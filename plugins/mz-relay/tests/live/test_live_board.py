"""Live: two real Claude accounts take one board task from post to done, and log a decision.
Run by hand: MZ_RELAY_LIVE_CLAUDE_DIRS="<cfgA>:<cfgB>" uv run make live-board"""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fakes_claude import claude_agent
from mz_relay.client import RelayClient
from relay_rig import online_names, paths_in, relayd_status, stop_daemon, wait_until
from stream_claude import StreamJsonClaude

START_S = 180.0
TURN_S = 300.0
POLL_S = 0.5
PROJECT = "live"
TOOLS = ",".join(
    f"mcp__plugin_mz-relay_mz-relay__{tool}"
    for tool in (
        *("board_list", "board_get", "board_post", "board_claim", "board_update", "board_review"),
        *("notes_add", "notes_search"),
    )
)


def board_roster(cfg_a: Path, cfg_b: Path) -> str:
    """`builder` posts and does the work, `checker` reviews it; both extensions enabled."""
    builder = claude_agent(cfg_a, name="builder").replace('role = "reviewer"', 'role = "builder"')
    return (
        "version = 1\n"
        + builder
        + claude_agent(cfg_b, name="checker")
        + '\n[extensions]\nenabled = ["board", "notes"]\n'
    )


def board_row(state_dir: Path, sql: str, *args: object) -> tuple[object, ...]:
    with closing(sqlite3.connect(state_dir / "ext" / "board.db")) as db:
        row = db.execute(sql, args).fetchone()
    return tuple(row) if row else ()


@pytest.mark.live_board
def test_a_builder_and_a_reviewer_take_a_task_to_done(
    live_claude_dirs: tuple[Path, Path],
    claude_bin: str,
    relay_home: Path,
    relay_env: dict[str, str],
    sessions: list[StreamJsonClaude],
) -> None:
    cfg_a, cfg_b = live_claude_dirs
    nonce = secrets.token_hex(4)
    paths = paths_in(relay_home)
    paths.roster.write_text(board_roster(cfg_a, cfg_b), encoding="utf-8")

    def start(name: str, cfg: Path) -> StreamJsonClaude:
        work = relay_home / name
        work.mkdir()
        env = {**relay_env, "CLAUDE_CONFIG_DIR": str(cfg), "MZ_RELAY_NAME": name}
        session = StreamJsonClaude(
            claude_bin,
            env,
            cwd=work,
            log=relay_home / f"{name}.log",
            extra=["--allowedTools", TOOLS],
        )
        sessions.append(session)
        return session

    try:
        builder = start("builder", cfg_a)
        checker = start("checker", cfg_b)
        both_online = wait_until(
            lambda: {"builder", "checker"} <= online_names(relayd_status(paths.socket)),
            START_S,
            POLL_S,
        )
        assert both_online, f"builder and checker not both online: {relayd_status(paths.socket)}"
        with closing(RelayClient(paths.socket)) as user:
            user.connect(client="cli")
            args = {"name": PROJECT, "default_access": "write"}
            user.call("ext_human", {"ext": "board", "action": "create_project", "args": args})

        builder.prompt(
            f"Use the mz-relay board tools in project {PROJECT}. Post one task titled "
            f"'Check {nonce}' with body 'Say hello.' and acceptance 'hello was said'. "
            "Claim it, then update it to state review with the note 'hello'. "
            f"Then add a note to project {PROJECT} titled 'Decision {nonce}' with body "
            "'Reviews go to checker.' Reply DONE."
        )
        assert builder.wait_results(1, TURN_S), "builder did not finish its turn"
        task = f"{PROJECT}-1"
        assert board_row(
            paths.state_dir, "SELECT state, assignee FROM tasks WHERE id = ?", task
        ) == (
            "review",
            "builder",
        )

        checker.prompt(
            f"Use the mz-relay board tools: approve task {task} in review. "
            f"Then search the notes of project {PROJECT} and reply with the title you find."
        )
        assert checker.wait_results(1, TURN_S), "checker did not finish its turn"

        assert board_row(paths.state_dir, "SELECT state, title FROM tasks WHERE id = ?", task) == (
            "done",
            f"Check {nonce}",
        )
        reviewed = "SELECT actor FROM task_events WHERE task = ? AND kind = 'approve'"
        assert board_row(paths.state_dir, reviewed, task) == ("checker",)
        assert checker.saw(f"Decision {nonce}")
    finally:
        for session in sessions:
            session.close()
        stop_daemon(paths.socket)
