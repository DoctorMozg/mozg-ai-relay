"""The notes extension: a per-project decision log under the board's project permissions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from mz_relay.cli import Terminal, main
from mz_relay.client import RelayClient
from mz_relay.errors import RelayError
from mz_relay.ext.board import Board
from mz_relay.ext.notes import NO_BOARD, Notes
from mz_relay.paths import Paths
from mz_relay.service import ConnState
from relay_rig import ServiceRig, pull_roster, running_daemon

NOTE = {"title": "Use SQLite", "body": "One file per extension keeps migrations apart."}


class NotesRig:
    """A service rig with notes enabled; agents call notes tools, the user runs both extensions."""

    def __init__(self, rig: ServiceRig) -> None:
        self.rig = rig
        self.tokens: dict[str, tuple[ConnState, str]] = {}

    def tool(self, agent: str, tool: str, **args: object) -> dict[str, object]:
        if agent not in self.tokens:
            self.tokens[agent] = self.rig.connect(agent)
        conn, token = self.tokens[agent]
        return self.rig.call(conn, "ext_call", token=token, tool=f"notes_{tool}", args=args)

    def text(self, agent: str, tool: str, **args: object) -> str:
        return str(self.tool(agent, tool, **args)["text"])

    def refused(self, agent: str, tool: str, **args: object) -> RelayError:
        with pytest.raises(RelayError) as caught:
            self.tool(agent, tool, **args)
        return caught.value

    def human(self, ext: str, action: str, **args: object) -> dict[str, Any]:
        cli = self.rig.hello(client="cli")
        return self.rig.call(cli, "ext_human", ext=ext, action=action, args=args)

    def head(self) -> int:
        return int(self.rig.call(self.rig.hello(client="cli"), "watch")["last_seq"])


@pytest.fixture
def n(relay_home: Path) -> Iterator[NotesRig]:
    rig = ServiceRig(relay_home, extensions=[Board(), Notes()])
    live = NotesRig(rig)
    live.human("board", "create_project", name="web", default_access="write")
    try:
        yield live
    finally:
        rig.close()


class TestAccess:
    def test_the_board_grants_decide_who_reads_and_who_writes(self, n: NotesRig) -> None:
        n.tool("bob", "add", project="web", **NOTE)
        n.human("board", "set_access", project="web", agent="carol", access="read")
        n.human("board", "set_access", project="web", agent="alice", access="none")

        assert "Use SQLite" in n.text("carol", "search", project="web")
        assert n.refused("carol", "add", project="web", **NOTE).code == "not_allowed"
        assert n.refused("alice", "search", project="web").code == "not_found"
        assert n.refused("alice", "add", project="web", **NOTE).code == "not_found"

    def test_an_unknown_project_looks_like_one_without_access(self, n: NotesRig) -> None:
        assert n.refused("bob", "search", project="nope").code == "not_found"

    def test_an_archived_project_keeps_its_log_readable_but_closed(self, n: NotesRig) -> None:
        n.tool("bob", "add", project="web", **NOTE)
        n.human("board", "archive_project", name="web")

        assert "Use SQLite" in n.text("bob", "search", project="web")
        assert n.refused("bob", "add", project="web", **NOTE).code == "not_allowed"

    def test_without_the_board_there_are_no_projects(self, relay_home: Path) -> None:
        rig = ServiceRig(relay_home, extensions=[Notes()])
        try:
            live = NotesRig(rig)
            err = live.refused("bob", "add", project="web", **NOTE)
            listed = live.human("notes", "list")
        finally:
            rig.close()

        assert (err.code, err.message) == ("not_found", NO_BOARD)
        assert listed["notes"] == []


class TestSearch:
    def test_words_match_title_or_body_ignoring_case_newest_first(self, n: NotesRig) -> None:
        n.tool("bob", "add", project="web", **NOTE)
        n.tool(
            "bob", "add", project="web", title="Port", body="The UI picks a free port.", kind="note"
        )
        n.tool("bob", "add", project="web", title="Logging", body="No bodies in logs.")

        found = n.text("carol", "search", project="web", query="PORT")
        everything = n.text("carol", "search", project="web")

        assert "#2 note by bob: Port" in found
        assert "SQLite" not in found
        assert everything.index("#3") < everything.index("#2") < everything.index("#1")

    def test_wildcards_in_a_query_are_plain_characters(self, n: NotesRig) -> None:
        n.tool("bob", "add", project="web", **NOTE)

        assert n.text("bob", "search", project="web", query="%") == "no notes in web for '%'"

    def test_bad_arguments_name_the_field(self, n: NotesRig) -> None:
        assert "kind" in n.refused("bob", "add", project="web", **NOTE, kind="rumour").message
        assert "title" in n.refused("bob", "add", project="web", title="x" * 201, body="b").message


class TestUserActions:
    def test_list_covers_every_project_and_filters_by_project_and_words(self, n: NotesRig) -> None:
        n.human("board", "create_project", name="api", default_access="write")
        n.tool("bob", "add", project="web", **NOTE)
        n.tool("alice", "add", project="api", title="Versioning", body="Paths carry v1.")

        everything = n.human("notes", "list")
        api = n.human("notes", "list", project="api")
        words = n.human("notes", "list", query="migrations")

        assert everything["projects"] == ["api", "web"]
        assert [(x["project"], x["author"]) for x in everything["notes"]] == [
            ("api", "alice"),
            ("web", "bob"),
        ]
        assert [x["title"] for x in api["notes"]] == ["Versioning"]
        assert [x["title"] for x in words["notes"]] == ["Use SQLite"]

    def test_delete_removes_a_note_and_an_unknown_id_is_not_found(self, n: NotesRig) -> None:
        note = n.tool("bob", "add", project="web", **NOTE)["id"]

        n.human("notes", "delete", id=note)

        assert n.human("notes", "list")["notes"] == []
        with pytest.raises(RelayError) as caught:
            n.human("notes", "delete", id=note)
        assert caught.value.code == "not_found"

    def test_an_added_note_is_an_ext_event_and_a_listing_is_not(self, n: NotesRig) -> None:
        head = n.head()

        n.tool("bob", "add", project="web", **NOTE)
        n.human("notes", "list")

        events = n.rig.call(n.rig.hello(client="cli"), "watch", since=head)["events"]
        assert [(e["ext"], e["agent"]) for e in events if e["kind"] == "ext"] == [("notes", "bob")]


class TestNotesCommand:
    def test_the_user_lists_notes_from_a_terminal(
        self, relay_paths: Paths, agent_free_env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        roster = (
            pull_roster(relay_paths.config_dir) + '\n[extensions]\nenabled = ["board", "notes"]\n'
        )
        terminal = Terminal(isatty=lambda: True, ask=lambda _: "", env={})
        with running_daemon(relay_paths, roster_text=roster):
            main(["board", "create_project", "name=web", "default_access=write"], terminal=terminal)
            with closing(RelayClient(relay_paths.socket)) as agent:
                agent.connect(client="mcp")
                hello = {
                    "name": "bob",
                    "harness": "opencode",
                    "cwd": str(relay_paths.config_dir),
                    "pid": 1,
                }
                reg = agent.call("register", hello)
                agent.call(
                    "ext_call",
                    {
                        "token": reg["token"],
                        "tool": "notes_add",
                        "args": {"project": "web", **NOTE},
                    },
                )
            capsys.readouterr()

            assert main(["notes", "list", "project=web"], terminal=terminal) == 0

        tools = reg["ext_tools"]
        assert isinstance(tools, list)
        assert [t["name"] for t in tools][-2:] == ["notes_add", "notes_search"]
        assert "#1  web  decision  bob: Use SQLite" in capsys.readouterr().out
