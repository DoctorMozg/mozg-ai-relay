"""The extension framework: its DB and migrations, tools per role, human actions, events."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest
from mz_relay.errors import ProtocolError, RelayError, RosterError
from mz_relay.extensions import (
    ExtensionError,
    Extensions,
    ExtTool,
    HumanAction,
    HumanCall,
    ToolCall,
    discover,
)
from mz_relay.presence import Registered
from mz_relay.roster import parse_roster
from mz_relay.store import Event
from relay_rig import ServiceRig, pull_roster

READER = """
[agents.dave]
harness = "opencode"
label = "personal"
role = "reviewer"
description = "Reads the board, changes nothing."
tools = "read_only"
"""
SCHEMA = {"type": "object"}


@dataclass
class FakeExtension:
    """A note list: list reads, add writes, boom raises; it records every event it is handed."""

    name: str = "fake"
    schema_version: int = 1
    migrations: list[int] = field(default_factory=list)
    fail_on: str | None = None

    def migrate(self, db: sqlite3.Connection, from_version: int) -> None:
        self.migrations.append(from_version)
        db.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, author TEXT, text TEXT)")
        db.execute("CREATE TABLE seen(seq INTEGER PRIMARY KEY, kind TEXT)")

    def tools(self) -> Sequence[ExtTool]:
        return [
            ExtTool("list", "List notes.", SCHEMA, mutates=False, handler=self.list_notes),
            ExtTool("add", "Add a note.", SCHEMA, mutates=True, handler=self.add),
            ExtTool("boom", "Always fails.", SCHEMA, mutates=False, handler=self.boom),
        ]

    def human_actions(self) -> Sequence[HumanAction]:
        return [HumanAction("clear", self.clear)]

    def on_event(self, db: sqlite3.Connection, event: Event, now: float) -> None:
        if event.kind == self.fail_on:
            raise ValueError(f"cannot handle {event.kind}")
        db.execute("INSERT INTO seen(seq, kind) VALUES(?, ?)", (event.seq, event.kind))

    def list_notes(self, call: ToolCall) -> dict[str, object]:
        rows = call.db.execute("SELECT author, text FROM notes ORDER BY id").fetchall()
        return {"text": "; ".join(f"{r['author']}: {r['text']}" for r in rows) or "no notes"}

    def add(self, call: ToolCall) -> dict[str, object]:
        text = call.args.get("text")
        if not isinstance(text, str) or not text:
            raise ExtensionError("invalid_args", "add needs text")
        call.db.execute("INSERT INTO notes(author, text) VALUES(?, ?)", (call.caller.name, text))
        return {"text": f"added a note for {call.caller.name}"}

    def boom(self, call: ToolCall) -> dict[str, object]:
        raise ZeroDivisionError("bug in the extension")

    def clear(self, call: HumanCall) -> dict[str, object]:
        deleted = call.db.execute("DELETE FROM notes").rowcount
        return {"text": f"cleared {deleted} notes"}


def seen(ext_dir: Path) -> list[str]:
    with sqlite3.connect(ext_dir / "fake.db") as db:
        return [row[0] for row in db.execute("SELECT kind FROM seen ORDER BY seq")]


@pytest.fixture
def fake() -> FakeExtension:
    return FakeExtension()


@pytest.fixture
def ext_rig(relay_home: Path, fake: FakeExtension) -> Iterator[ServiceRig]:
    live = ServiceRig(relay_home, extra_agents=READER, extensions=[fake])
    try:
        yield live
    finally:
        live.close()


def ext_call(rig: ServiceRig, name: str, tool: str, **args: object) -> dict[str, object]:
    conn, token = rig.connect(name)
    return rig.call(conn, "ext_call", token=token, tool=tool, args=args)


class TestDatabase:
    def test_migration_runs_once_and_sets_the_user_version(
        self, tmp_path: Path, fake: FakeExtension
    ) -> None:
        Extensions([fake], tmp_path).close()
        Extensions([fake], tmp_path).close()

        with sqlite3.connect(tmp_path / "fake.db") as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
        assert fake.migrations == [0]
        assert version == 1

    def test_a_db_newer_than_the_extension_is_refused(self, tmp_path: Path) -> None:
        Extensions([FakeExtension(schema_version=2)], tmp_path).close()

        with pytest.raises(RuntimeError, match="newer"):
            Extensions([FakeExtension(schema_version=1)], tmp_path)

    def test_a_failed_migration_leaves_the_db_at_its_old_version(self, tmp_path: Path) -> None:
        class Broken(FakeExtension):
            def migrate(self, db: sqlite3.Connection, from_version: int) -> None:
                db.execute("CREATE TABLE half(x)")
                raise RuntimeError("migration bug")

        with pytest.raises(RuntimeError, match="migration bug"):
            Extensions([Broken()], tmp_path)

        with sqlite3.connect(tmp_path / "fake.db") as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            version = db.execute("PRAGMA user_version").fetchone()[0]
        assert "half" not in tables
        assert version == 0


class TestAgentTools:
    def test_tools_are_prefixed_and_a_read_only_agent_sees_no_mutating_one(
        self, ext_rig: ServiceRig
    ) -> None:
        def names(agent: str) -> list[str]:
            reg = ext_rig.register(ext_rig.hello(), agent)
            return [t["name"] for t in reg["ext_tools"]]

        assert names("bob") == ["fake_list", "fake_add", "fake_boom"]
        assert names("dave") == ["fake_list", "fake_boom"]

    def test_a_call_runs_as_the_calling_agent(self, ext_rig: ServiceRig) -> None:
        ext_call(ext_rig, "bob", "fake_add", text="ship it")

        assert ext_call(ext_rig, "alice", "fake_list")["text"] == "bob: ship it"

    def test_a_read_only_agent_is_refused_a_mutating_tool_at_call(
        self, ext_rig: ServiceRig
    ) -> None:
        with pytest.raises(RelayError) as caught:
            ext_call(ext_rig, "dave", "fake_add", text="sneaky")

        assert caught.value.code == "not_allowed"
        assert ext_call(ext_rig, "dave", "fake_list")["text"] == "no notes"

    def test_an_extension_error_keeps_its_code(self, ext_rig: ServiceRig) -> None:
        with pytest.raises(RelayError) as caught:
            ext_call(ext_rig, "bob", "fake_add")

        assert caught.value.code == "invalid_args"

    def test_a_crashing_tool_is_an_error_and_the_extension_keeps_working(
        self, ext_rig: ServiceRig
    ) -> None:
        with pytest.raises(RelayError) as caught:
            ext_call(ext_rig, "bob", "fake_boom")

        assert caught.value.code == "ext_failed"
        assert "ZeroDivisionError" in caught.value.message
        assert ext_call(ext_rig, "bob", "fake_list")["text"] == "no notes"

    def test_without_extensions_register_sends_no_tool_field(self, rig: ServiceRig) -> None:
        assert "ext_tools" not in rig.register(rig.hello(), "bob")

    @pytest.mark.parametrize("tool", ["fake_nope", "other_list", "fake"])
    def test_an_unknown_tool_is_not_found(self, ext_rig: ServiceRig, tool: str) -> None:
        with pytest.raises(RelayError) as caught:
            ext_call(ext_rig, "bob", tool)

        assert caught.value.code == "not_found"

    def test_args_must_be_an_object(self, ext_rig: ServiceRig) -> None:
        conn, token = ext_rig.connect("bob")

        with pytest.raises(RelayError, match="args must be an object"):
            ext_rig.call(conn, "ext_call", token=token, tool="fake_list", args=["x"])


class TestRegisteredTools:
    BASE: ClassVar[dict[str, object]] = {
        "token": "t",
        "agent": {},
        "delivery_mode": None,
        "brief": "b",
    }

    def test_the_register_reply_carries_the_tool_list_to_the_mcp_server(self) -> None:
        tool = {"name": "fake_list", "description": "List notes.", "inputSchema": SCHEMA}

        registered = Registered.from_result({**self.BASE, "ext_tools": [tool]})

        assert registered.ext_tools == (tool,)
        assert Registered.from_result(self.BASE).ext_tools == ()

    def test_a_malformed_tool_list_is_a_protocol_error(self) -> None:
        with pytest.raises(ProtocolError, match="ext_tools"):
            Registered.from_result({**self.BASE, "ext_tools": "fake_list"})


class TestHumanActions:
    def test_the_user_runs_an_action_from_the_cli(self, ext_rig: ServiceRig) -> None:
        ext_call(ext_rig, "bob", "fake_add", text="one")

        result = ext_rig.call(ext_rig.hello(client="cli"), "ext_human", ext="fake", action="clear")

        assert result["text"] == "cleared 1 notes"

    def test_an_agent_connection_cannot_reach_human_actions(self, ext_rig: ServiceRig) -> None:
        with pytest.raises(RelayError) as caught:
            ext_rig.call(ext_rig.hello(client="mcp"), "ext_human", ext="fake", action="clear")

        assert caught.value.code == "not_allowed"

    def test_an_unknown_action_is_not_found(self, ext_rig: ServiceRig) -> None:
        with pytest.raises(RelayError) as caught:
            ext_rig.call(ext_rig.hello(client="cli"), "ext_human", ext="fake", action="nuke")

        assert caught.value.code == "not_found"


class TestEvents:
    def test_a_new_extension_starts_at_the_head_then_sees_later_events(
        self, ext_rig: ServiceRig
    ) -> None:
        conn, alice = ext_rig.connect("alice")
        ext_rig.send(conn, alice, "bob")
        ext_rig.extensions.pump(ext_rig.store, 0.0)

        ext_rig.send(conn, alice, "bob")
        handed = ext_rig.extensions.pump(ext_rig.store, 0.0)

        assert handed == 1
        assert seen(ext_rig.paths.state_dir / "ext") == ["message"]

    def test_the_cursor_survives_a_restart(self, ext_rig: ServiceRig, fake: FakeExtension) -> None:
        conn, alice = ext_rig.connect("alice")
        ext_rig.extensions.pump(ext_rig.store, 0.0)
        ext_rig.send(conn, alice, "bob")
        ext_rig.extensions.pump(ext_rig.store, 0.0)
        ext_dir = ext_rig.paths.state_dir / "ext"
        ext_rig.extensions.close()
        ext_rig.send(conn, alice, "bob")

        reopened = Extensions([fake], ext_dir)
        try:
            handed = reopened.pump(ext_rig.store, 0.0)
        finally:
            reopened.close()

        assert handed == 1
        assert seen(ext_dir) == ["message", "message"]

    def test_a_failing_handler_is_logged_and_the_feed_moves_on(
        self, ext_rig: ServiceRig, fake: FakeExtension, caplog: pytest.LogCaptureFixture
    ) -> None:
        ext_rig.extensions.pump(ext_rig.store, 0.0)
        fake.fail_on = "presence"
        conn, alice = ext_rig.connect("alice")
        ext_rig.send(conn, alice, "bob")

        ext_rig.extensions.pump(ext_rig.store, 0.0)
        again = ext_rig.extensions.pump(ext_rig.store, 0.0)

        assert seen(ext_rig.paths.state_dir / "ext") == ["message"]
        assert again == 0
        assert "failed on event" in caplog.text


class TestRosterAndDiscovery:
    def roster(self, extensions: str) -> str:
        return pull_roster(Path("/tmp/mzr")) + extensions

    def test_the_roster_names_the_enabled_extensions(self) -> None:
        roster = parse_roster(
            self.roster('\n[extensions]\nenabled = ["board", "notes"]\n'), pw_home=Path("/tmp")
        )

        assert roster.extensions == ("board", "notes")

    def test_no_extensions_table_enables_none(self) -> None:
        assert parse_roster(self.roster(""), pw_home=Path("/tmp")).extensions == ()

    @pytest.mark.parametrize(
        ("table", "message"),
        [
            ('enabled = ["Board"]', "bad extension name"),
            ('enabled = ["board", "board"]', "twice"),
            ('enabled = "board"', "list of names"),
            ("disabled = []", "unknown"),
        ],
    )
    def test_a_bad_extensions_table_is_refused(self, table: str, message: str) -> None:
        with pytest.raises(RosterError, match=message):
            parse_roster(self.roster(f"\n[extensions]\n{table}\n"), pw_home=Path("/tmp"))

    def test_discovery_skips_an_enabled_name_with_no_module(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR)

        assert discover(["nosuchext"]) == []
        assert "nosuchext" in caplog.text
