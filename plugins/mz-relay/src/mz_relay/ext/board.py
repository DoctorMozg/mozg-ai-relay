"""The board: per-project task lists that agents post to, claim, report on and review."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from mz_relay.ext import board_data as data
from mz_relay.ext.board_admin import BoardAdmin
from mz_relay.ext.board_data import board_args
from mz_relay.extensions import (
    ExtArgs,
    ExtensionError,
    ExtTool,
    HumanAction,
    ToolCall,
    object_schema,
)
from mz_relay.roster import AgentSpec
from mz_relay.store import Event

log = logging.getLogger(__name__)

OPEN_CAP = 50
REVIEWER_ROLE = "reviewer"

_STR = {"type": "string"}
_TASK_ID = {"type": "string", "description": "Task id such as web-3."}
_PROJECT = {"type": "string", "description": "Project name such as web."}


class Board:
    name = "board"
    schema_version = 1

    def __init__(self) -> None:
        self._admin = BoardAdmin()

    def migrate(self, db: sqlite3.Connection, from_version: int) -> None:
        if from_version < 1:
            for statement in data.SCHEMA:
                db.execute(statement)

    def tools(self) -> Sequence[ExtTool]:
        return [
            ExtTool(
                "list",
                "Without project: the board projects you can see. With project: its tasks, "
                "ready ones marked; filter by state or your own.",
                object_schema(
                    {
                        "project": _PROJECT,
                        "state": {"enum": list(data.STATES)},
                        "mine": {"type": "boolean"},
                    }
                ),
                mutates=False,
                handler=self.list_tasks,
            ),
            ExtTool(
                "get",
                "One task whole: body, acceptance criteria, dependencies and recent events.",
                object_schema({"id": _TASK_ID}, ["id"]),
                mutates=False,
                handler=self.get,
            ),
            ExtTool(
                "post",
                "Post a task to a project. Acceptance criteria are required: how is done checked?",
                object_schema(
                    {
                        "project": _PROJECT,
                        "title": _STR,
                        "body": _STR,
                        "acceptance": _STR,
                        "priority": {
                            "type": "integer",
                            "minimum": data.PRIORITY_MIN,
                            "maximum": data.PRIORITY_MAX,
                        },
                        "depends_on": {"type": "array", "items": _TASK_ID},
                    },
                    ["project", "title", "body", "acceptance"],
                ),
                mutates=True,
                handler=self.post,
            ),
            ExtTool(
                "claim",
                "Take a ready task: by id, or the top ready task of a project. Your claim holds "
                f"{data.LEASE_S // 60} minutes and renews on each board call you make.",
                object_schema({"id": _TASK_ID, "project": _PROJECT}),
                mutates=True,
                handler=self.claim,
            ),
            ExtTool(
                "update",
                "Report on a task you hold: add a note, hand it to review, or release it to open.",
                object_schema(
                    {"id": _TASK_ID, "note": _STR, "state": {"enum": ["review", "open"]}}, ["id"]
                ),
                mutates=True,
                handler=self.update,
            ),
            ExtTool(
                "review",
                "Reviewers only: approve a task in review (done) or reject it back to its holder.",
                object_schema(
                    {"id": _TASK_ID, "decision": {"enum": ["approve", "reject"]}, "note": _STR},
                    ["id", "decision"],
                ),
                mutates=True,
                handler=self.review,
            ),
        ]

    def human_actions(self) -> Sequence[HumanAction]:
        return self._admin.actions()

    def on_event(self, db: sqlite3.Connection, event: Event, now: float) -> None:
        # Any relay activity is a tick; leases need no thread of their own.
        with data.transaction(db):
            data.reclaim(db, now)

    def list_tasks(self, call: ToolCall) -> dict[str, object]:
        args = board_args(call.args, {"project", "state", "mine"})
        project = args.optional_text("project")
        with data.transaction(call.db):
            self._tick(call)
            if project is None:
                text = self._project_lines(call.db, call.caller)
            else:
                text = self._task_lines(call.db, call.caller, project, args)
            return self._answer(call, text)

    def get(self, call: ToolCall) -> dict[str, object]:
        task_id = board_args(call.args, {"id"}).text("id")
        with data.transaction(call.db):
            self._tick(call)
            task = data.task_for(call.db, call.caller, task_id, "read")
            return self._answer(call, data.task_text(call.db, task, call.now))

    def post(self, call: ToolCall) -> dict[str, object]:
        args = board_args(
            call.args, {"project", "title", "body", "acceptance", "priority", "depends_on"}
        )
        project = args.text("project")
        db, me = call.db, call.caller
        with data.transaction(db):
            self._tick(call)
            data.require(db, me, project, "write")
            active = db.execute(
                f"SELECT count(*) FROM tasks WHERE creator = ? AND state IN {data.ACTIVE}",
                (me.name,),
            ).fetchone()[0]
            if active >= OPEN_CAP:
                raise ExtensionError(
                    "cap_reached", f"{me.name} has {active} unfinished tasks, the cap is {OPEN_CAP}"
                )
            deps = [data.task_for(db, me, dep, "read")["id"] for dep in args.strings("depends_on")]
            n = db.execute(
                "SELECT coalesce(max(n), 0) + 1 FROM tasks WHERE project = ?", (project,)
            ).fetchone()[0]
            task_id = f"{project}-{n}"
            db.execute(
                "INSERT INTO tasks(id, project, n, title, body, acceptance, state, priority, "
                "creator, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (
                    task_id,
                    project,
                    n,
                    args.text("title"),
                    args.text("body"),
                    args.text("acceptance"),
                    args.integer("priority", 0, data.PRIORITY_MIN, data.PRIORITY_MAX),
                    me.name,
                    call.now,
                    call.now,
                ),
            )
            db.executemany(
                "INSERT INTO task_deps(task, depends_on) VALUES(?, ?)", [(task_id, d) for d in deps]
            )
            data.add_event(db, task_id, call.now, me.name, "posted")
            log.info("%s posted %s", me.name, task_id)
            waits = f", waiting on {', '.join(deps)}" if deps else ""
            return self._answer(call, f"posted {task_id}{waits}", id=task_id)

    def claim(self, call: ToolCall) -> dict[str, object]:
        args = board_args(call.args, {"id", "project"})
        task_id, project = args.optional_text("id"), args.optional_text("project")
        if (task_id is None) == (project is None):
            raise ExtensionError("invalid_args", "claim takes either id or project")
        db, me = call.db, call.caller
        with data.transaction(db):
            self._tick(call)
            if task_id is not None:
                task = data.task_for(db, me, task_id, "write")
                if task["state"] != "open":
                    holder = f" by {task['assignee']}" if task["assignee"] else ""
                    raise ExtensionError("conflict", f"{task_id} is {task['state']}{holder}")
                if not data.ready(db, task_id):
                    raise ExtensionError("conflict", f"{task_id} waits on unfinished dependencies")
            else:
                data.require(db, me, str(project), "write")
                top = data.top_ready(db, str(project))
                if top is None:
                    return self._answer(call, f"no ready task in {project}")
                task = top
            claimed = db.execute(
                "UPDATE tasks SET state = 'in_progress', assignee = ?, lease_until = ?, "
                "version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (me.name, call.now + data.LEASE_S, call.now, task["id"], task["version"]),
            ).rowcount
            if not claimed:
                raise ExtensionError("conflict", f"{task['id']} changed while claiming; try again")
            data.add_event(db, task["id"], call.now, me.name, "claimed")
            log.info("%s claimed %s", me.name, task["id"])
            text = f"claimed {task['id']}: {task['title']}\nacceptance: {task['acceptance']}"
            return self._answer(call, text, id=task["id"])

    def update(self, call: ToolCall) -> dict[str, object]:
        args = board_args(call.args, {"id", "note", "state"})
        note = args.optional_text("note")
        state = args.optional_choice("state", ["review", "open"])
        if note is None and state is None:
            raise ExtensionError("invalid_args", "update needs a note, a state or both")
        db, me = call.db, call.caller
        with data.transaction(db):
            self._tick(call)
            task = data.task_for(db, me, args.text("id"), "write")
            if task["state"] != "in_progress" or task["assignee"] != me.name:
                raise ExtensionError("conflict", f"{task['id']} is not in progress with you")
            if note is not None:
                data.add_event(db, task["id"], call.now, me.name, "note", note)
            if state == "review":
                data.set_state(db, task["id"], "review", call.now, keep_assignee=True)
                data.add_event(db, task["id"], call.now, me.name, "to_review")
            elif state == "open":
                data.set_state(db, task["id"], "open", call.now, keep_assignee=False)
                data.add_event(db, task["id"], call.now, me.name, "released")
            return self._answer(call, f"updated {task['id']}" + (f": now {state}" if state else ""))

    def review(self, call: ToolCall) -> dict[str, object]:
        args = board_args(call.args, {"id", "decision", "note"})
        decision = args.choice("decision", ["approve", "reject"])
        note = args.optional_text("note")
        db, me = call.db, call.caller
        if me.role != REVIEWER_ROLE:
            raise ExtensionError(
                "not_allowed", f"only an agent with role {REVIEWER_ROLE!r} reviews"
            )
        if decision == "reject" and note is None:
            raise ExtensionError("invalid_args", "a rejection needs a note saying what is missing")
        with data.transaction(db):
            self._tick(call)
            task = data.task_for(db, me, args.text("id"), "write")
            if task["state"] != "review":
                raise ExtensionError("conflict", f"{task['id']} is {task['state']}, not in review")
            if task["assignee"] == me.name:
                raise ExtensionError("not_allowed", f"{me.name} cannot review its own work")
            if decision == "approve":
                data.set_state(db, task["id"], "done", call.now, keep_assignee=True)
            else:
                db.execute(
                    "UPDATE tasks SET state = 'in_progress', lease_until = ?, "
                    "version = version + 1, updated_at = ? WHERE id = ?",
                    (call.now + data.LEASE_S, call.now, task["id"]),
                )
                data.add_notice(
                    db, str(task["assignee"]), f"{task['id']} came back from review: {note}"
                )
            data.add_event(db, task["id"], call.now, me.name, decision, note)
            log.info("%s %sd %s", me.name, decision, task["id"])
            return self._answer(call, f"{decision}d {task['id']}")

    def _tick(self, call: ToolCall) -> None:
        data.reclaim(call.db, call.now)
        # Any board call by the holder keeps its claims alive.
        call.db.execute(
            "UPDATE tasks SET lease_until = ? WHERE assignee = ? AND state = 'in_progress'",
            (call.now + data.LEASE_S, call.caller.name),
        )

    def _answer(self, call: ToolCall, text: str, **extra: object) -> dict[str, object]:
        """The tool text, with the caller's pending notices appended and cleared."""
        notices = call.db.execute(
            "SELECT id, text FROM notices WHERE agent = ? ORDER BY id", (call.caller.name,)
        ).fetchall()
        if notices:
            call.db.execute("DELETE FROM notices WHERE agent = ?", (call.caller.name,))
            text += "\n\nboard notices:\n" + "\n".join(f"- {n['text']}" for n in notices)
        return {"text": text, **extra}

    def _project_lines(self, db: sqlite3.Connection, me: AgentSpec) -> str:
        lines: list[str] = []
        for project in db.execute("SELECT name, title FROM projects ORDER BY name"):
            access = data.access(db, project["name"], me.name)
            if access == "none":
                continue
            counts = dict(
                db.execute(
                    "SELECT state, count(*) FROM tasks WHERE project = ? GROUP BY state",
                    (project["name"],),
                ).fetchall()
            )
            summary = ", ".join(f"{counts.get(s, 0)} {s}" for s in data.ACTIVE)
            lines.append(f"- {project['name']} ({access}): {project['title']}; {summary}")
        return "board projects you can see:\n" + "\n".join(lines) if lines else "no board projects"

    def _task_lines(
        self, db: sqlite3.Connection, me: AgentSpec, project: str, args: ExtArgs
    ) -> str:
        data.require(db, me, project, "read")
        state = args.optional_choice("state", data.STATES)
        query = "SELECT * FROM tasks WHERE project = ?"
        params: list[object] = [project]
        if state is not None:
            query += " AND state = ?"
            params.append(state)
        if args.flag("mine"):
            query += " AND assignee = ?"
            params.append(me.name)
        rows = db.execute(query + " ORDER BY priority DESC, n", params).fetchall()
        lines = []
        for task in rows:
            marks = [task["state"]]
            if task["state"] == "open" and data.ready(db, task["id"]):
                marks.append("ready")
            holder = f", with {task['assignee']}" if task["assignee"] else ""
            lines.append(
                f"- {task['id']} [{', '.join(marks)}] p{task['priority']} {task['title']}{holder}"
            )
        return f"{project}:\n" + "\n".join(lines) if lines else f"no matching tasks in {project}"


EXTENSION = Board()
