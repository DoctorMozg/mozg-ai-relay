"""The board extension: projects and access, the ready queue, claims and leases, review."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing
from functools import partial
from pathlib import Path

import pytest
from mz_relay.cli import Terminal, main
from mz_relay.client import RelayClient
from mz_relay.errors import RelayError
from mz_relay.ext import board
from mz_relay.ext.board import Board
from mz_relay.ext.board_data import LEASE_S
from mz_relay.ids import new_id
from mz_relay.paths import Paths
from mz_relay.service import ConnState
from relay_rig import (
    WAIT_S,
    FakeClock,
    InThread,
    ServiceRig,
    pull_roster,
    running_daemon,
    wait_until,
)

REVIEWER = """
[agents.dave]
harness = "opencode"
label = "personal"
role = "reviewer"
description = "Reviews finished board tasks."

[agents.frank]
harness = "opencode"
label = "personal"
role = "auditor"
description = "Reads the board, changes nothing."
tools = "read_only"
"""
TASK = {"title": "Fix login", "body": "The form loses its state.", "acceptance": "test passes"}


class BoardRig:
    """A service rig with the board enabled; agents call tools, the user runs human actions."""

    def __init__(self, rig: ServiceRig) -> None:
        self.rig = rig
        self.tokens: dict[str, tuple[ConnState, str]] = {}

    def tool(self, agent: str, tool: str, **args: object) -> dict[str, object]:
        if agent not in self.tokens:
            self.tokens[agent] = self.rig.connect(agent)
        conn, token = self.tokens[agent]
        return self.rig.call(conn, "ext_call", token=token, tool=f"board_{tool}", args=args)

    def text(self, agent: str, tool: str, **args: object) -> str:
        return str(self.tool(agent, tool, **args)["text"])

    def refused(self, agent: str, tool: str, **args: object) -> RelayError:
        with pytest.raises(RelayError) as caught:
            self.tool(agent, tool, **args)
        return caught.value

    def human(self, action: str, **args: object) -> str:
        cli = self.rig.hello(client="cli")
        return str(self.rig.call(cli, "ext_human", ext="board", action=action, args=args)["text"])

    def state(self, task: str) -> str:
        """The stored state, read past the board so no sweep runs first."""
        with closing(sqlite3.connect(self.rig.paths.state_dir / "ext" / "board.db")) as db:
            return str(db.execute("SELECT state FROM tasks WHERE id = ?", (task,)).fetchone()[0])

    def post(self, agent: str = "alice", project: str = "web", **extra: object) -> str:
        return str(self.tool(agent, "post", project=project, **{**TASK, **extra})["id"])


@pytest.fixture
def b(relay_home: Path) -> Iterator[BoardRig]:
    rig = ServiceRig(relay_home, extra_agents=REVIEWER, extensions=[Board()])
    live = BoardRig(rig)
    live.human("create_project", name="web", title="Website", default_access="write")
    try:
        yield live
    finally:
        rig.close()


class TestBrief:
    def test_the_brief_names_the_board_steps_each_agent_may_take(self, b: BoardRig) -> None:
        def brief(agent: str) -> str:
            return str(b.rig.register(b.rig.hello(), agent)["brief"])

        worker, reviewer, reader = brief("bob"), brief("dave"), brief("frank")

        assert "board_claim" in worker
        assert "30 minutes" in worker
        assert "board_review" not in worker
        assert "board_review" in reviewer
        assert "board_list" in reader
        assert "board_claim" not in reader


class TestAccess:
    def test_an_agent_with_no_access_gets_not_found_for_the_project_and_its_tasks(
        self, b: BoardRig
    ) -> None:
        task = b.post()
        b.human("set_access", project="web", agent="carol", access="none")

        for tool, args in (("list", {"project": "web"}), ("get", {"id": task}), ("post", TASK)):
            extra = {"project": "web"} if tool == "post" else {}
            assert b.refused("carol", tool, **args, **extra).code == "not_found"
        assert "web" not in b.text("carol", "list")

    def test_read_access_lists_but_cannot_change(self, b: BoardRig) -> None:
        task = b.post()
        b.human("set_access", project="web", agent="carol", access="read")

        assert task in b.text("carol", "list", project="web")
        assert b.refused("carol", "claim", id=task).code == "not_allowed"

    def test_a_grant_overrides_the_default_and_inherit_drops_it(self, b: BoardRig) -> None:
        b.human("create_project", name="corp", default_access="none")
        b.human("set_access", project="corp", agent="bob", access="write")

        assert b.post("bob", "corp") == "corp-1"
        assert b.refused("alice", "list", project="corp").code == "not_found"

        b.human("set_access", project="corp", agent="bob", access="inherit")
        assert b.refused("bob", "list", project="corp").code == "not_found"

    def test_an_archived_project_is_read_only(self, b: BoardRig) -> None:
        b.post()
        b.human("archive_project", name="web")

        assert "web-1" in b.text("bob", "list", project="web")
        assert b.refused("bob", "claim", project="web").code == "not_allowed"

    def test_only_the_user_creates_projects(self, b: BoardRig) -> None:
        assert b.refused("alice", "post", project="new", **TASK).code == "not_found"
        with pytest.raises(RelayError, match="exists"):
            b.human("create_project", name="web")


class TestPosting:
    def test_acceptance_criteria_are_required(self, b: BoardRig) -> None:
        error = b.refused("alice", "post", project="web", title="x", body="y")

        assert (error.code, "acceptance" in error.message) == ("invalid_args", True)

    def test_ids_count_per_project(self, b: BoardRig) -> None:
        b.human("create_project", name="api", default_access="write")

        assert [b.post(), b.post(project="api"), b.post()] == ["web-1", "api-1", "web-2"]

    def test_the_open_task_cap_holds_per_creator(
        self, b: BoardRig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(board, "OPEN_CAP", 2)
        b.post()
        b.post()

        assert b.refused("alice", "post", project="web", **TASK).code == "cap_reached"
        assert b.post("bob") == "web-3"


class TestReadyQueue:
    def test_claim_by_project_takes_the_top_ready_task_and_skips_blocked_ones(
        self, b: BoardRig
    ) -> None:
        first = b.post(priority=1)
        blocked = b.post(priority=5, depends_on=[first])

        claimed = b.tool("bob", "claim", project="web")

        assert claimed["id"] == first
        assert b.refused("bob", "claim", id=blocked).message.endswith("unfinished dependencies")
        assert f"{blocked} [open] p5" in b.text("bob", "list", project="web")

    def test_a_dependency_done_makes_its_dependent_ready(self, b: BoardRig) -> None:
        first = b.post()
        second = b.post(depends_on=[first])
        b.tool("bob", "claim", id=first)
        b.tool("bob", "update", id=first, state="review")

        b.tool("dave", "review", id=first, decision="approve")

        assert f"{second} [open, ready]" in b.text("bob", "list", project="web")

    def test_two_claims_at_once_have_one_winner(self, b: BoardRig) -> None:
        task = b.post()
        b.text("bob", "list")
        b.text("dave", "list")

        def claim(agent: str) -> str:
            try:
                return str(b.tool(agent, "claim", id=task)["id"])
            except RelayError as exc:
                return exc.code

        racers = [InThread(partial(claim, agent)) for agent in ("bob", "dave")]

        assert sorted(r.result(5.0) for r in racers) == ["conflict", task]


class TestLeases:
    def test_an_expired_claim_goes_back_to_open_and_the_creator_hears_of_it(
        self, b: BoardRig
    ) -> None:
        task = b.post()
        b.tool("bob", "claim", id=task)
        b.rig.clock.advance(LEASE_S + 1)

        listing = b.text("dave", "list", project="web")
        creator = b.text("alice", "list", project="web")

        assert f"{task} [open, ready]" in listing
        assert f"{task} went back to open: bob's claim expired" in creator
        assert "claim expired" not in b.text("alice", "list", project="web")

    def test_any_call_of_the_holder_renews_its_claim(self, b: BoardRig) -> None:
        task = b.post()
        b.tool("bob", "claim", id=task)
        b.rig.clock.advance(LEASE_S - 10)
        b.text("bob", "list")
        b.rig.clock.advance(LEASE_S - 10)

        assert "in_progress" in b.text("alice", "get", id=task)

    def test_relay_activity_alone_sweeps_expired_claims(self, b: BoardRig) -> None:
        task = b.post()
        b.tool("bob", "claim", id=task)
        b.rig.extensions.pump(b.rig.store, b.rig.clock.now())
        b.rig.clock.advance(LEASE_S + 1)
        conn, alice = b.tokens["alice"]
        assert b.state(task) == "in_progress"
        b.rig.send(conn, alice, "bob")

        b.rig.extensions.pump(b.rig.store, b.rig.clock.now())

        assert b.state(task) == "open"


class TestReview:
    def test_the_builder_cannot_reach_done_and_a_reviewer_can(self, b: BoardRig) -> None:
        task = b.post()
        b.tool("bob", "claim", id=task)
        b.tool("bob", "update", id=task, note="fixed in abc123", state="review")

        assert b.refused("bob", "review", id=task, decision="approve").code == "not_allowed"
        b.tool("dave", "review", id=task, decision="approve")

        detail = b.text("alice", "get", id=task)
        assert detail.startswith(f"{task} [done]")
        assert "bob note: fixed in abc123" in detail

    def test_a_reviewer_cannot_approve_its_own_work(self, b: BoardRig) -> None:
        task = b.post()
        b.tool("dave", "claim", id=task)
        b.tool("dave", "update", id=task, state="review")

        assert b.refused("dave", "review", id=task, decision="approve").code == "not_allowed"

    def test_a_rejection_needs_a_note_and_returns_the_task_to_its_holder(self, b: BoardRig) -> None:
        task = b.post()
        b.tool("bob", "claim", id=task)
        b.tool("bob", "update", id=task, state="review")

        assert b.refused("dave", "review", id=task, decision="reject").code == "invalid_args"
        b.tool("dave", "review", id=task, decision="reject", note="no test for the empty form")

        assert f"{task} came back from review: no test for the empty form" in b.text("bob", "list")
        assert "in_progress" in b.text("alice", "get", id=task)

    def test_only_the_holder_updates_a_task(self, b: BoardRig) -> None:
        task = b.post()
        b.tool("bob", "claim", id=task)

        assert b.refused("alice", "update", id=task, state="review").code == "conflict"


class TestHumanActions:
    def test_the_user_approves_a_task_in_review(self, b: BoardRig) -> None:
        task = b.post()
        b.tool("bob", "claim", id=task)
        b.tool("bob", "update", id=task, state="review")

        assert b.human("approve", id=task) == f"approved {task}"
        assert "[done]" in b.text("alice", "get", id=task)

    def test_reassign_hands_the_task_over_with_a_notice(self, b: BoardRig) -> None:
        task = b.post()

        b.human("reassign", id=task, agent="bob")

        listing = b.text("bob", "list", project="web", mine=True)
        assert f"{task} [in_progress]" in listing
        assert f"your user gave you {task}" in listing

    def test_cancel_and_reprioritize(self, b: BoardRig) -> None:
        first, second = b.post(), b.post()

        b.human("reprioritize", id=second, priority=9)
        b.human("cancel", id=first, reason="duplicate")

        assert b.tool("bob", "claim", project="web")["id"] == second
        assert "user cancelled: duplicate" in b.text("alice", "get", id=first)


class TestInRelayd:
    def test_a_roster_that_enables_the_board_serves_its_tools_over_the_socket(
        self, relay_paths: Paths
    ) -> None:
        roster = pull_roster(relay_paths.config_dir) + '\n[extensions]\nenabled = ["board"]\n'
        with running_daemon(relay_paths, roster_text=roster):
            with closing(RelayClient(relay_paths.socket)) as cli:
                cli.connect(client="cli")
                args = {"name": "web", "default_access": "write"}
                cli.call("ext_human", {"ext": "board", "action": "create_project", "args": args})
            with closing(RelayClient(relay_paths.socket)) as agent:
                agent.connect(client="mcp")
                reg = agent.call(
                    "register",
                    {
                        "name": "bob",
                        "harness": "opencode",
                        "cwd": str(relay_paths.config_dir),
                        "pid": 1,
                    },
                )
                call = {
                    "token": reg["token"],
                    "tool": "board_post",
                    "args": {"project": "web", **TASK},
                }
                posted = agent.call("ext_call", call)

        tools = reg["ext_tools"]
        assert isinstance(tools, list)
        assert [t["name"] for t in tools][:2] == ["board_list", "board_get"]
        assert posted["id"] == "web-1"
        assert (relay_paths.state_dir / "ext" / "board.db").exists()

    def test_relayds_event_pump_reclaims_an_expired_claim_when_a_message_arrives(
        self, relay_paths: Paths
    ) -> None:
        roster = pull_roster(relay_paths.config_dir) + '\n[extensions]\nenabled = ["board"]\n'
        clock = FakeClock()
        board_db = relay_paths.state_dir / "ext" / "board.db"

        def query(db_path: Path, sql: str) -> object:
            with closing(sqlite3.connect(db_path)) as db:
                return db.execute(sql).fetchone()[0]

        def pump_caught_up() -> bool:
            head = query(relay_paths.state_dir / "relay.db", "SELECT max(seq) FROM events")
            return head == query(board_db, "SELECT seq FROM mz_ext_cursor")

        with running_daemon(relay_paths, roster_text=roster, clock=clock), ExitStack() as stack:
            cli = stack.enter_context(closing(RelayClient(relay_paths.socket)))
            cli.connect(client="cli")
            args = {"name": "web", "default_access": "write"}
            cli.call("ext_human", {"ext": "board", "action": "create_project", "args": args})
            tokens = {}
            for name in ("alice", "bob"):
                agent = stack.enter_context(closing(RelayClient(relay_paths.socket)))
                agent.connect(client="mcp")
                where = {"harness": "opencode", "cwd": str(relay_paths.config_dir), "pid": 1}
                tokens[name] = (agent, agent.call("register", {"name": name, **where})["token"])

            def call(name: str, op: str, args: dict[str, object]) -> dict[str, object]:
                agent, token = tokens[name]
                reply: dict[str, object] = agent.call(op, {"token": token, **args})
                return reply

            post = {"project": "web", **TASK}
            task = call("alice", "ext_call", {"tool": "board_post", "args": post})["id"]
            call("bob", "ext_call", {"tool": "board_claim", "args": {"id": task}})
            # The claim's own event must be handed over before the lease runs out.
            assert wait_until(pump_caught_up, WAIT_S)
            clock.advance(LEASE_S + 1)
            assert query(board_db, f"SELECT state FROM tasks WHERE id = '{task}'") == "in_progress"

            call("alice", "send", {"id": new_id(), "to": "bob", "kind": "inform", "body": "hi"})

            reclaimed = f"SELECT state FROM tasks WHERE id = '{task}'"
            assert wait_until(lambda: query(board_db, reclaimed) == "open", WAIT_S)


class TestUserViews:
    def test_overview_shows_every_project_grant_and_task_and_records_no_event(
        self, b: BoardRig
    ) -> None:
        b.human("create_project", name="corp", default_access="none")
        b.human("set_access", project="corp", agent="bob", access="write")
        first = b.post()
        b.post(depends_on=[first])
        head = b.rig.call(b.rig.hello(client="cli"), "watch")["last_seq"]

        cli = b.rig.hello(client="cli")
        view = b.rig.call(cli, "ext_human", ext="board", action="overview")

        projects = {p["name"]: p for p in view["projects"]}
        assert projects["corp"]["grants"] == {"bob": "write"}
        assert [(t["id"], t["ready"]) for t in view["tasks"]] == [("web-1", True), ("web-2", False)]
        assert b.rig.call(cli, "watch")["last_seq"] == head

    def test_task_shows_body_dependencies_and_every_event(self, b: BoardRig) -> None:
        first = b.post()
        second = b.post(depends_on=[first])
        b.tool("bob", "claim", id=first)

        cli = b.rig.hello(client="cli")
        view = b.rig.call(cli, "ext_human", ext="board", action="task", args={"id": second})
        claimed = b.rig.call(cli, "ext_human", ext="board", action="task", args={"id": first})

        assert view["task"]["body"] == TASK["body"]
        assert view["deps"] == [{"id": first, "state": "in_progress"}]
        assert [e["kind"] for e in claimed["events"]] == ["posted", "claimed"]

    def test_a_change_is_an_ext_event_on_the_feed(self, b: BoardRig) -> None:
        head = b.rig.call(b.rig.hello(client="cli"), "watch")["last_seq"]

        b.post("bob")

        events = b.rig.call(b.rig.hello(client="cli"), "watch", since=head)["events"]
        changes = [(e["ext"], e["agent"]) for e in events if e["kind"] == "ext"]
        assert changes == [("board", "bob")]


class TestBoardCommand:
    @pytest.fixture
    def board_relayd(self, relay_paths: Paths, agent_free_env: dict[str, str]) -> Iterator[Paths]:
        roster = pull_roster(relay_paths.config_dir) + '\n[extensions]\nenabled = ["board"]\n'
        with running_daemon(relay_paths, roster_text=roster):
            yield relay_paths

    def run(self, *argv: str, env: dict[str, str] | None = None) -> int:
        terminal = Terminal(isatty=lambda: True, ask=lambda _: "", env=env or {})
        return main(["board", *argv], terminal=terminal)

    def test_actions_take_key_value_pairs_and_print_the_result(
        self, board_relayd: Paths, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert self.run("create_project", "name=web", "default_access=write") == 0
        assert self.run("overview") == 0

        out = capsys.readouterr().out
        assert "created project web, default access write" in out
        assert "1 projects, 0 tasks" in out

    def test_json_values_keep_their_type(
        self, board_relayd: Paths, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.run("create_project", "name=web", "default_access=write")
        with closing(RelayClient(board_relayd.socket)) as agent:
            agent.connect(client="mcp")
            reg = agent.call(
                "register",
                {
                    "name": "bob",
                    "harness": "opencode",
                    "cwd": str(board_relayd.config_dir),
                    "pid": 1,
                },
            )
            call = {"token": reg["token"], "tool": "board_post", "args": {"project": "web", **TASK}}
            agent.call("ext_call", call)

        assert self.run("reprioritize", "id=web-1", "priority=7") == 0
        self.run("overview")
        assert "web-1  open  p7  Fix login" in capsys.readouterr().out

    def test_an_agent_session_is_refused(self, board_relayd: Paths) -> None:
        assert self.run("overview", env={"CODEX_THREAD_ID": "t1"}) == 2

    def test_a_pair_without_an_equals_sign_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as caught:
            self.run("approve", "web-1")

        assert caught.value.code == 2
