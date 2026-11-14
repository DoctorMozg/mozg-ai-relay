"""The board: per-project task lists that agents post to, claim, report on and review."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from mz_relay.extensions import ExtensionError, ExtTool, HumanAction, HumanCall, ToolCall
from mz_relay.roster import AgentSpec
from mz_relay.store import Event

log = logging.getLogger(__name__)

LEASE_S = 1800
OPEN_CAP = 50
REVIEWER_ROLE = "reviewer"
ACCESS = ("none", "read", "write")
STATES = ("open", "in_progress", "review", "done", "cancelled")
ACTIVE = ("open", "in_progress", "review")
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
LIMITS = {"title": 200, "body": 8000, "acceptance": 4000, "note": 2000, "reason": 2000}
EVENTS_SHOWN = 10

SCHEMA = (
    """CREATE TABLE projects(
        name TEXT PRIMARY KEY, title TEXT NOT NULL, default_access TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)""",
    """CREATE TABLE grants(
        project TEXT NOT NULL REFERENCES projects(name), agent TEXT NOT NULL,
        access TEXT NOT NULL, PRIMARY KEY(project, agent))""",
    """CREATE TABLE tasks(
        id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(name), n INTEGER NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, acceptance TEXT NOT NULL, state TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0, creator TEXT NOT NULL, assignee TEXT,
        lease_until REAL, version INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(project, n))""",
    """CREATE TABLE task_deps(
        task TEXT NOT NULL REFERENCES tasks(id), depends_on TEXT NOT NULL REFERENCES tasks(id),
        PRIMARY KEY(task, depends_on))""",
    """CREATE TABLE task_events(
        id INTEGER PRIMARY KEY, task TEXT NOT NULL, at REAL NOT NULL, actor TEXT NOT NULL,
        kind TEXT NOT NULL, note TEXT)""",
    "CREATE TABLE notices(id INTEGER PRIMARY KEY, agent TEXT NOT NULL, text TEXT NOT NULL)",
    "CREATE INDEX tasks_ready ON tasks(project, state, priority DESC, n)",
)


def _object_schema(
    properties: dict[str, object], required: Sequence[str] = ()
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_STR = {"type": "string"}
_TASK_ID = {"type": "string", "description": "Task id such as web-3."}
_PROJECT = {"type": "string", "description": "Project name such as web."}


class _Args:
    """One call's arguments; a bad one is invalid_args naming the field."""

    def __init__(self, args: Mapping[str, object], allowed: set[str]) -> None:
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ExtensionError("invalid_args", f"unknown arguments: {', '.join(unknown)}")
        self.args = args

    def text(self, name: str) -> str:
        value = self.optional_text(name)
        if value is None:
            raise ExtensionError("invalid_args", f"{name} is required")
        return value

    def optional_text(self, name: str) -> str | None:
        value = self.args.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ExtensionError("invalid_args", f"{name} must be a non-empty string")
        limit = LIMITS.get(name)
        if limit is not None and len(value) > limit:
            raise ExtensionError("invalid_args", f"{name} has {len(value)} characters, max {limit}")
        return value.strip()

    def choice(self, name: str, allowed: Sequence[str], default: str | None = None) -> str:
        value = self.args.get(name, default)
        if value not in allowed:
            raise ExtensionError(
                "invalid_args", f"{name} must be one of {', '.join(allowed)}, got {value!r}"
            )
        return str(value)

    def optional_choice(self, name: str, allowed: Sequence[str]) -> str | None:
        return None if self.args.get(name) is None else self.choice(name, allowed)

    def integer(self, name: str, default: int) -> int:
        value = self.args.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not -100 <= value <= 100:
            raise ExtensionError("invalid_args", f"{name} must be an integer in -100..100")
        return value

    def names(self, name: str) -> list[str]:
        value = self.args.get(name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ExtensionError("invalid_args", f"{name} must be a list of task ids")
        return list(dict.fromkeys(value))

    def flag(self, name: str) -> bool:
        value = self.args.get(name, False)
        if not isinstance(value, bool):
            raise ExtensionError("invalid_args", f"{name} must be true or false")
        return value


@contextmanager
def _tx(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _not_found(what: str) -> ExtensionError:
    return ExtensionError("not_found", f"no {what}")


class Board:
    name = "board"
    schema_version = 1

    def migrate(self, db: sqlite3.Connection, from_version: int) -> None:
        if from_version < 1:
            for statement in SCHEMA:
                db.execute(statement)

    def tools(self) -> Sequence[ExtTool]:
        return [
            ExtTool(
                "list",
                "Without project: the board projects you can see. With project: its tasks, "
                "ready ones marked; filter by state or your own.",
                _object_schema(
                    {
                        "project": _PROJECT,
                        "state": {"enum": list(STATES)},
                        "mine": {"type": "boolean"},
                    }
                ),
                mutates=False,
                handler=self.list_tasks,
            ),
            ExtTool(
                "get",
                "One task whole: body, acceptance criteria, dependencies and recent events.",
                _object_schema({"id": _TASK_ID}, ["id"]),
                mutates=False,
                handler=self.get,
            ),
            ExtTool(
                "post",
                "Post a task to a project. Acceptance criteria are required: how is done checked?",
                _object_schema(
                    {
                        "project": _PROJECT,
                        "title": _STR,
                        "body": _STR,
                        "acceptance": _STR,
                        "priority": {"type": "integer", "minimum": -100, "maximum": 100},
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
                f"{LEASE_S // 60} minutes and renews on each board call you make.",
                _object_schema({"id": _TASK_ID, "project": _PROJECT}),
                mutates=True,
                handler=self.claim,
            ),
            ExtTool(
                "update",
                "Report on a task you hold: add a note, hand it to review, or release it to open.",
                _object_schema(
                    {"id": _TASK_ID, "note": _STR, "state": {"enum": ["review", "open"]}}, ["id"]
                ),
                mutates=True,
                handler=self.update,
            ),
            ExtTool(
                "review",
                "Reviewers only: approve a task in review (done) or reject it back to its holder.",
                _object_schema(
                    {"id": _TASK_ID, "decision": {"enum": ["approve", "reject"]}, "note": _STR},
                    ["id", "decision"],
                ),
                mutates=True,
                handler=self.review,
            ),
        ]

    def human_actions(self) -> Sequence[HumanAction]:
        return [
            HumanAction("create_project", self.create_project),
            HumanAction("archive_project", self.archive_project),
            HumanAction("set_access", self.set_access),
            HumanAction("move", self.move),
            HumanAction("reprioritize", self.reprioritize),
            HumanAction("reassign", self.reassign),
            HumanAction("approve", self.approve),
            HumanAction("cancel", self.cancel),
        ]

    def on_event(self, db: sqlite3.Connection, event: Event, now: float) -> None:
        # Any relay activity is a tick; leases need no thread of their own.
        with _tx(db):
            self._reclaim(db, now)

    # Agent tools

    def list_tasks(self, call: ToolCall) -> dict[str, object]:
        args = _Args(call.args, {"project", "state", "mine"})
        project = args.optional_text("project")
        with _tx(call.db):
            self._tick(call)
            if project is None:
                text = self._project_lines(call.db, call.caller)
            else:
                text = self._task_lines(call.db, call.caller, project, args)
            return self._answer(call, text)

    def get(self, call: ToolCall) -> dict[str, object]:
        task_id = _Args(call.args, {"id"}).text("id")
        with _tx(call.db):
            self._tick(call)
            task = self._task(call.db, call.caller, task_id, "read")
            return self._answer(call, self._task_detail(call.db, task, call.now))

    def post(self, call: ToolCall) -> dict[str, object]:
        args = _Args(
            call.args, {"project", "title", "body", "acceptance", "priority", "depends_on"}
        )
        project = args.text("project")
        db, me = call.db, call.caller
        with _tx(db):
            self._tick(call)
            self._require(db, me, project, "write")
            active = db.execute(
                f"SELECT count(*) FROM tasks WHERE creator = ? AND state IN {ACTIVE}", (me.name,)
            ).fetchone()[0]
            if active >= OPEN_CAP:
                raise ExtensionError(
                    "cap_reached", f"{me.name} has {active} unfinished tasks, the cap is {OPEN_CAP}"
                )
            deps = [self._task(db, me, dep, "read")["id"] for dep in args.names("depends_on")]
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
                    args.integer("priority", 0),
                    me.name,
                    call.now,
                    call.now,
                ),
            )
            db.executemany(
                "INSERT INTO task_deps(task, depends_on) VALUES(?, ?)", [(task_id, d) for d in deps]
            )
            self._event(db, task_id, call.now, me.name, "posted")
            log.info("%s posted %s", me.name, task_id)
            waits = f", waiting on {', '.join(deps)}" if deps else ""
            return self._answer(call, f"posted {task_id}{waits}", id=task_id)

    def claim(self, call: ToolCall) -> dict[str, object]:
        args = _Args(call.args, {"id", "project"})
        task_id, project = args.optional_text("id"), args.optional_text("project")
        if (task_id is None) == (project is None):
            raise ExtensionError("invalid_args", "claim takes either id or project")
        db, me = call.db, call.caller
        with _tx(db):
            self._tick(call)
            if task_id is not None:
                task = self._task(db, me, task_id, "write")
                if task["state"] != "open":
                    holder = f" by {task['assignee']}" if task["assignee"] else ""
                    raise ExtensionError("conflict", f"{task_id} is {task['state']}{holder}")
                if not self._ready(db, task_id):
                    raise ExtensionError("conflict", f"{task_id} waits on unfinished dependencies")
            else:
                self._require(db, me, str(project), "write")
                top = self._top_ready(db, str(project))
                if top is None:
                    return self._answer(call, f"no ready task in {project}")
                task = top
            claimed = db.execute(
                "UPDATE tasks SET state = 'in_progress', assignee = ?, lease_until = ?, "
                "version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (me.name, call.now + LEASE_S, call.now, task["id"], task["version"]),
            ).rowcount
            if not claimed:
                raise ExtensionError("conflict", f"{task['id']} changed while claiming; try again")
            self._event(db, task["id"], call.now, me.name, "claimed")
            log.info("%s claimed %s", me.name, task["id"])
            text = f"claimed {task['id']}: {task['title']}\nacceptance: {task['acceptance']}"
            return self._answer(call, text, id=task["id"])

    def update(self, call: ToolCall) -> dict[str, object]:
        args = _Args(call.args, {"id", "note", "state"})
        note = args.optional_text("note")
        state = args.optional_choice("state", ["review", "open"])
        if note is None and state is None:
            raise ExtensionError("invalid_args", "update needs a note, a state or both")
        db, me = call.db, call.caller
        with _tx(db):
            self._tick(call)
            task = self._task(db, me, args.text("id"), "write")
            if task["state"] != "in_progress" or task["assignee"] != me.name:
                raise ExtensionError("conflict", f"{task['id']} is not in progress with you")
            if note is not None:
                self._event(db, task["id"], call.now, me.name, "note", note)
            if state == "review":
                self._set_state(db, task["id"], "review", call.now, keep_assignee=True)
                self._event(db, task["id"], call.now, me.name, "to_review")
            elif state == "open":
                self._set_state(db, task["id"], "open", call.now, keep_assignee=False)
                self._event(db, task["id"], call.now, me.name, "released")
            return self._answer(call, f"updated {task['id']}" + (f": now {state}" if state else ""))

    def review(self, call: ToolCall) -> dict[str, object]:
        args = _Args(call.args, {"id", "decision", "note"})
        decision = args.choice("decision", ["approve", "reject"])
        note = args.optional_text("note")
        db, me = call.db, call.caller
        if me.role != REVIEWER_ROLE:
            raise ExtensionError(
                "not_allowed", f"only an agent with role {REVIEWER_ROLE!r} reviews"
            )
        if decision == "reject" and note is None:
            raise ExtensionError("invalid_args", "a rejection needs a note saying what is missing")
        with _tx(db):
            self._tick(call)
            task = self._task(db, me, args.text("id"), "write")
            if task["state"] != "review":
                raise ExtensionError("conflict", f"{task['id']} is {task['state']}, not in review")
            if task["assignee"] == me.name:
                raise ExtensionError("not_allowed", f"{me.name} cannot review its own work")
            if decision == "approve":
                self._set_state(db, task["id"], "done", call.now, keep_assignee=True)
            else:
                db.execute(
                    "UPDATE tasks SET state = 'in_progress', lease_until = ?, "
                    "version = version + 1, updated_at = ? WHERE id = ?",
                    (call.now + LEASE_S, call.now, task["id"]),
                )
                self._notice(
                    db, str(task["assignee"]), f"{task['id']} came back from review: {note}"
                )
            self._event(db, task["id"], call.now, me.name, decision, note)
            log.info("%s %sd %s", me.name, decision, task["id"])
            return self._answer(call, f"{decision}d {task['id']}")

    # Human actions

    def create_project(self, call: HumanCall) -> dict[str, object]:
        args = _Args(call.args, {"name", "title", "default_access"})
        name = args.text("name")
        if PROJECT_RE.fullmatch(name) is None:
            raise ExtensionError("invalid_args", f"project name must match {PROJECT_RE.pattern}")
        access = args.choice("default_access", ACCESS, "none")
        with _tx(call.db):
            if call.db.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
                raise ExtensionError("conflict", f"project {name} exists")
            call.db.execute(
                "INSERT INTO projects(name, title, default_access, created_at) VALUES(?, ?, ?, ?)",
                (name, args.optional_text("title") or name, access, call.now),
            )
        return {"text": f"created project {name}, default access {access}"}

    def archive_project(self, call: HumanCall) -> dict[str, object]:
        name = _Args(call.args, {"name"}).text("name")
        with _tx(call.db):
            if not call.db.execute(
                "UPDATE projects SET archived = 1 WHERE name = ?", (name,)
            ).rowcount:
                raise _not_found(f"project {name}")
        return {"text": f"archived project {name}; agents can still read it"}

    def set_access(self, call: HumanCall) -> dict[str, object]:
        args = _Args(call.args, {"project", "agent", "access"})
        project, agent = args.text("project"), args.optional_text("agent")
        access = args.choice("access", [*ACCESS, "inherit"])
        db = call.db
        with _tx(db):
            if not db.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone():
                raise _not_found(f"project {project}")
            if agent is None:
                if access == "inherit":
                    raise ExtensionError("invalid_args", "the default access cannot inherit")
                db.execute(
                    "UPDATE projects SET default_access = ? WHERE name = ?", (access, project)
                )
                return {"text": f"{project} default access is now {access}"}
            if access == "inherit":
                db.execute("DELETE FROM grants WHERE project = ? AND agent = ?", (project, agent))
            else:
                db.execute(
                    "INSERT INTO grants(project, agent, access) VALUES(?, ?, ?) "
                    "ON CONFLICT(project, agent) DO UPDATE SET access = excluded.access",
                    (project, agent, access),
                )
        return {"text": f"{agent} on {project}: {access}"}

    def move(self, call: HumanCall) -> dict[str, object]:
        args = _Args(call.args, {"id", "state"})
        state = args.choice("state", STATES)
        with _tx(call.db):
            task = self._any_task(call.db, args.text("id"))
            self._set_state(call.db, task["id"], state, call.now, keep_assignee=state != "open")
            self._event(call.db, task["id"], call.now, "user", f"moved_{state}")
        return {"text": f"moved {task['id']} to {state}"}

    def reprioritize(self, call: HumanCall) -> dict[str, object]:
        args = _Args(call.args, {"id", "priority"})
        priority = args.integer("priority", 0)
        with _tx(call.db):
            task = self._any_task(call.db, args.text("id"))
            call.db.execute(
                "UPDATE tasks SET priority = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (priority, call.now, task["id"]),
            )
            self._event(call.db, task["id"], call.now, "user", "reprioritized", str(priority))
        return {"text": f"{task['id']} priority is now {priority}"}

    def reassign(self, call: HumanCall) -> dict[str, object]:
        args = _Args(call.args, {"id", "agent"})
        agent = args.optional_text("agent")
        db = call.db
        with _tx(db):
            task = self._any_task(db, args.text("id"))
            if agent is None:
                self._set_state(db, task["id"], "open", call.now, keep_assignee=False)
            else:
                db.execute(
                    "UPDATE tasks SET assignee = ?, state = CASE state WHEN 'open' THEN "
                    "'in_progress' ELSE state END, lease_until = ?, version = version + 1, "
                    "updated_at = ? WHERE id = ?",
                    (agent, call.now + LEASE_S, call.now, task["id"]),
                )
                self._notice(db, agent, f"your user gave you {task['id']}: {task['title']}")
            self._event(db, task["id"], call.now, "user", "reassigned", agent)
        return {"text": f"{task['id']} is now with {agent or 'nobody'}"}

    def approve(self, call: HumanCall) -> dict[str, object]:
        task_id = _Args(call.args, {"id"}).text("id")
        with _tx(call.db):
            task = self._any_task(call.db, task_id)
            if task["state"] != "review":
                raise ExtensionError("conflict", f"{task_id} is {task['state']}, not in review")
            self._set_state(call.db, task_id, "done", call.now, keep_assignee=True)
            self._event(call.db, task_id, call.now, "user", "approve")
        return {"text": f"approved {task_id}"}

    def cancel(self, call: HumanCall) -> dict[str, object]:
        args = _Args(call.args, {"id", "reason"})
        with _tx(call.db):
            task = self._any_task(call.db, args.text("id"))
            self._set_state(call.db, task["id"], "cancelled", call.now, keep_assignee=True)
            self._event(
                call.db, task["id"], call.now, "user", "cancelled", args.optional_text("reason")
            )
        return {"text": f"cancelled {task['id']}"}

    # Helpers

    def access(self, db: sqlite3.Connection, project: str, agent: str) -> str:
        row = db.execute(
            "SELECT p.default_access, p.archived, g.access FROM projects p "
            "LEFT JOIN grants g ON g.project = p.name AND g.agent = ? WHERE p.name = ?",
            (agent, project),
        ).fetchone()
        if row is None:
            return "none"
        access = str(row["access"] or row["default_access"])
        # An archived project stays readable for whoever could read it.
        return "read" if row["archived"] and access == "write" else access

    def _require(self, db: sqlite3.Connection, me: AgentSpec, project: str, need: str) -> None:
        access = self.access(db, project, me.name)
        # No access looks exactly like no project, so a name gives nothing away.
        if access == "none":
            raise _not_found(f"project {project}")
        if need == "write" and access != "write":
            raise ExtensionError("not_allowed", f"{me.name} may read {project} but not change it")

    def _task(self, db: sqlite3.Connection, me: AgentSpec, task_id: str, need: str) -> sqlite3.Row:
        project, _, _ = task_id.rpartition("-")
        if self.access(db, project, me.name) == "none":
            raise _not_found(f"task {task_id}")
        task = self._any_task(db, task_id)
        self._require(db, me, project, need)
        return task

    @staticmethod
    def _any_task(db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        task: sqlite3.Row | None = db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise _not_found(f"task {task_id}")
        return task

    @staticmethod
    def _ready(db: sqlite3.Connection, task_id: str) -> bool:
        return not db.execute(
            "SELECT 1 FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
            "WHERE d.task = ? AND t.state != 'done'",
            (task_id,),
        ).fetchone()

    def _top_ready(self, db: sqlite3.Connection, project: str) -> sqlite3.Row | None:
        rows = db.execute(
            "SELECT * FROM tasks WHERE project = ? AND state = 'open' ORDER BY priority DESC, n",
            (project,),
        )
        return next((row for row in rows if self._ready(db, row["id"])), None)

    @staticmethod
    def _set_state(
        db: sqlite3.Connection, task_id: str, state: str, now: float, *, keep_assignee: bool
    ) -> None:
        db.execute(
            "UPDATE tasks SET state = ?, assignee = CASE WHEN ? THEN assignee END, "
            "lease_until = CASE WHEN ? = 'in_progress' THEN lease_until END, "
            "version = version + 1, updated_at = ? WHERE id = ?",
            (state, keep_assignee, state, now, task_id),
        )

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        task_id: str,
        now: float,
        actor: str,
        kind: str,
        note: str | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO task_events(task, at, actor, kind, note) VALUES(?, ?, ?, ?, ?)",
            (task_id, now, actor, kind, note),
        )

    @staticmethod
    def _notice(db: sqlite3.Connection, agent: str, text: str) -> None:
        db.execute("INSERT INTO notices(agent, text) VALUES(?, ?)", (agent, text))

    def _tick(self, call: ToolCall) -> None:
        self._reclaim(call.db, call.now)
        # Any board call by the holder keeps its claims alive.
        call.db.execute(
            "UPDATE tasks SET lease_until = ? WHERE assignee = ? AND state = 'in_progress'",
            (call.now + LEASE_S, call.caller.name),
        )

    def _reclaim(self, db: sqlite3.Connection, now: float) -> None:
        expired = db.execute(
            "SELECT id, title, creator, assignee FROM tasks "
            "WHERE state = 'in_progress' AND lease_until < ?",
            (now,),
        ).fetchall()
        for task in expired:
            self._set_state(db, task["id"], "open", now, keep_assignee=False)
            self._event(
                db, task["id"], now, "board", "reclaimed", f"lease of {task['assignee']} expired"
            )
            self._notice(
                db,
                task["creator"],
                f"{task['id']} went back to open: {task['assignee']}'s claim expired",
            )
            log.info("reclaimed %s from %s", task["id"], task["assignee"])

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
            access = self.access(db, project["name"], me.name)
            if access == "none":
                continue
            counts = dict(
                db.execute(
                    "SELECT state, count(*) FROM tasks WHERE project = ? GROUP BY state",
                    (project["name"],),
                ).fetchall()
            )
            summary = ", ".join(f"{counts.get(s, 0)} {s}" for s in ACTIVE)
            lines.append(f"- {project['name']} ({access}): {project['title']}; {summary}")
        return "board projects you can see:\n" + "\n".join(lines) if lines else "no board projects"

    def _task_lines(self, db: sqlite3.Connection, me: AgentSpec, project: str, args: _Args) -> str:
        self._require(db, me, project, "read")
        state = args.optional_choice("state", STATES)
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
            if task["state"] == "open" and self._ready(db, task["id"]):
                marks.append("ready")
            holder = f", with {task['assignee']}" if task["assignee"] else ""
            lines.append(
                f"- {task['id']} [{', '.join(marks)}] p{task['priority']} {task['title']}{holder}"
            )
        return f"{project}:\n" + "\n".join(lines) if lines else f"no matching tasks in {project}"

    def _task_detail(self, db: sqlite3.Connection, task: sqlite3.Row, now: float) -> str:
        deps = db.execute(
            "SELECT t.id, t.state FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
            "WHERE d.task = ? ORDER BY t.n",
            (task["id"],),
        ).fetchall()
        events = db.execute(
            "SELECT actor, kind, note FROM task_events WHERE task = ? ORDER BY id DESC LIMIT ?",
            (task["id"], EVENTS_SHOWN),
        ).fetchall()
        lines = [
            f"{task['id']} [{task['state']}] p{task['priority']} {task['title']}",
            f"created by {task['creator']}"
            + (f", held by {task['assignee']}" if task["assignee"] else ""),
        ]
        if task["lease_until"] is not None:
            lines.append(f"claim expires in {max(0, int((task['lease_until'] - now) // 60))} min")
        if deps:
            lines.append("depends on: " + ", ".join(f"{d['id']} ({d['state']})" for d in deps))
        lines += [
            f"acceptance: {task['acceptance']}",
            "--- body ---",
            task["body"],
            "--- events ---",
        ]
        lines += [
            f"{e['actor']} {e['kind']}" + (f": {e['note']}" if e["note"] else "")
            for e in reversed(events)
        ]
        return "\n".join(lines)


EXTENSION = Board()
