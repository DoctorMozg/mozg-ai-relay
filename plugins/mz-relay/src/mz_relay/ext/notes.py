"""Notes: a per-project decision log, under the board's project permissions."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from mz_relay.ext import board_data
from mz_relay.extensions import (
    ExtArgs,
    ExtensionError,
    ExtTool,
    HumanAction,
    HumanCall,
    ToolCall,
    object_schema,
)
from mz_relay.roster import AgentSpec, ToolMode
from mz_relay.store import Event

log = logging.getLogger(__name__)

SEARCH_LIMIT = 20
KINDS = ("decision", "note")
LIMITS = {"title": 200, "body": 8000}
SCHEMA = (
    """CREATE TABLE notes(
        id INTEGER PRIMARY KEY, project TEXT NOT NULL, author TEXT NOT NULL, kind TEXT NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL)""",
    "CREATE INDEX notes_project ON notes(project, id)",
)
NO_BOARD = "notes uses the board's projects; enable the board extension and create a project"


def _attach_board(db: sqlite3.Connection) -> bool:
    """Attaches board.db beside this DB; notes only reads its projects and grants."""
    attached = {row[1] for row in db.execute("PRAGMA database_list")}
    if "board" in attached:
        return True
    main = next(row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main")
    board_db = Path(main).with_name("board.db")
    if not board_db.exists():
        return False
    db.execute("ATTACH DATABASE ? AS board", (str(board_db),))
    return True


class Notes:
    name = "notes"
    schema_version = 1

    def migrate(self, db: sqlite3.Connection, from_version: int) -> None:
        if from_version < 1:
            for statement in SCHEMA:
                db.execute(statement)

    def tools(self) -> Sequence[ExtTool]:
        return [
            ExtTool(
                "add",
                "Record a decision or a note in a project's log, for agents who come later.",
                object_schema(
                    {
                        "project": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "kind": {"enum": list(KINDS), "default": "decision"},
                    },
                    ["project", "title", "body"],
                ),
                mutates=True,
                handler=self.add,
            ),
            ExtTool(
                "search",
                "Search a project's log by words in the title or body; newest first.",
                object_schema(
                    {"project": {"type": "string"}, "query": {"type": "string"}}, ["project"]
                ),
                mutates=False,
                handler=self.search,
            ),
        ]

    def human_actions(self) -> Sequence[HumanAction]:
        return [
            HumanAction("list", self.list_notes, mutates=False),
            HumanAction("delete", self.delete),
        ]

    def brief(self, caller: AgentSpec) -> str:
        line = "notes: a decision log per project. Check notes_search before settling a question"
        if caller.tools is ToolMode.READ_ONLY:
            return line + "."
        return line + " a peer may have settled already, and record decisions with notes_add."

    def on_event(self, db: sqlite3.Connection, event: Event, now: float) -> None:
        return None

    def add(self, call: ToolCall) -> dict[str, object]:
        args = ExtArgs(call.args, {"project", "title", "body", "kind"}, LIMITS)
        project = args.text("project")
        self._require(call, project, "write")
        # One statement, no BEGIN IMMEDIATE: that would also write-lock the attached board.db.
        note_id = call.db.execute(
            "INSERT INTO notes(project, author, kind, title, body, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                project,
                call.caller.name,
                args.choice("kind", KINDS, "decision"),
                args.text("title"),
                args.text("body"),
                call.now,
            ),
        ).lastrowid
        log.info("%s added note %s to %s", call.caller.name, note_id, project)
        return {"text": f"recorded note {note_id} in {project}", "id": note_id}

    def search(self, call: ToolCall) -> dict[str, object]:
        args = ExtArgs(call.args, {"project", "query"}, LIMITS)
        project, query = args.text("project"), args.optional_text("query")
        self._require(call, project, "read")
        rows = self._rows(call.db, project, query)
        if not rows:
            return {"text": f"no notes in {project}" + (f" for {query!r}" if query else "")}
        lines = [
            f"- #{r['id']} {r['kind']} by {r['author']}: {r['title']}\n  {r['body']}" for r in rows
        ]
        return {"text": f"{project} notes:\n" + "\n".join(lines)}

    def list_notes(self, call: HumanCall) -> dict[str, object]:
        args = ExtArgs(call.args, {"project", "query"}, LIMITS)
        project, query = args.optional_text("project"), args.optional_text("query")
        if not _attach_board(call.db):
            return {"text": NO_BOARD, "projects": [], "notes": []}
        projects = [
            row[0] for row in call.db.execute("SELECT name FROM board.projects ORDER BY name")
        ]
        notes = [dict(row) for row in self._rows(call.db, project, query)]
        return {"text": f"{len(notes)} notes", "projects": projects, "notes": notes}

    def delete(self, call: HumanCall) -> dict[str, object]:
        note_id = call.args.get("id")
        if isinstance(note_id, bool) or not isinstance(note_id, int):
            raise ExtensionError("invalid_args", "id must be a note number")
        if not call.db.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount:
            raise ExtensionError("not_found", f"no note {note_id}")
        log.info("user deleted note %s", note_id)
        return {"text": f"deleted note {note_id}"}

    def _require(self, call: ToolCall, project: str, need: str) -> None:
        if not _attach_board(call.db):
            raise ExtensionError("not_found", NO_BOARD)
        # One access rule for both extensions. The board's queries name projects and grants
        # unqualified; notes.db has neither, so they resolve to the attached board.db.
        board_data.require(call.db, call.caller, project, need)

    @staticmethod
    def _rows(db: sqlite3.Connection, project: str | None, query: str | None) -> list[sqlite3.Row]:
        where, params = ["1"], []
        if project is not None:
            where.append("project = ?")
            params.append(project)
        if query is not None:
            # instr, unlike LIKE, has no wildcards an agent could slip into its words.
            where.append("(instr(lower(title), lower(?)) OR instr(lower(body), lower(?)))")
            params += [query, query]
        sql = f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
        return db.execute(sql, [*params, SEARCH_LIMIT]).fetchall()


EXTENSION = Notes()
