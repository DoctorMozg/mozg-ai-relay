"""The board's tables and the rules on them that agent tools, user actions and notes share."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from mz_relay.extensions import ExtArgs, ExtensionError
from mz_relay.roster import AgentSpec

log = logging.getLogger(__name__)

LEASE_S = 1800
ACCESS = ("none", "read", "write")
STATES = ("open", "in_progress", "review", "done", "cancelled")
ACTIVE = ("open", "in_progress", "review")
PRIORITY_MIN, PRIORITY_MAX = -100, 100
LIMITS = {"title": 200, "body": 8000, "acceptance": 4000, "note": 2000, "reason": 2000}
EVENTS_SHOWN = 10
TASK_FIELDS = (
    "id",
    "project",
    "title",
    "state",
    "priority",
    "creator",
    "assignee",
    "lease_until",
    "updated_at",
)

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


def board_args(args: Mapping[str, object], allowed: set[str]) -> ExtArgs:
    return ExtArgs(args, allowed, LIMITS)


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def not_found(what: str) -> ExtensionError:
    return ExtensionError("not_found", f"no {what}")


def access(db: sqlite3.Connection, project: str, agent: str) -> str:
    row = db.execute(
        "SELECT p.default_access, p.archived, g.access FROM projects p "
        "LEFT JOIN grants g ON g.project = p.name AND g.agent = ? WHERE p.name = ?",
        (agent, project),
    ).fetchone()
    if row is None:
        return "none"
    level = str(row["access"] or row["default_access"])
    # An archived project stays readable for whoever could read it.
    return "read" if row["archived"] and level == "write" else level


def require(db: sqlite3.Connection, me: AgentSpec, project: str, need: str) -> None:
    level = access(db, project, me.name)
    # No access looks exactly like no project, so a name gives nothing away.
    if level == "none":
        raise not_found(f"project {project}")
    if need == "write" and level != "write":
        raise ExtensionError("not_allowed", f"{me.name} may read {project} but not change it")


def task_for(db: sqlite3.Connection, me: AgentSpec, task_id: str, need: str) -> sqlite3.Row:
    """The task as the agent may see it; a hidden project reads as a missing task."""
    project, _, _ = task_id.rpartition("-")
    if access(db, project, me.name) == "none":
        raise not_found(f"task {task_id}")
    task = any_task(db, task_id)
    require(db, me, project, need)
    return task


def any_task(db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    task: sqlite3.Row | None = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise not_found(f"task {task_id}")
    return task


def ready(db: sqlite3.Connection, task_id: str) -> bool:
    return not db.execute(
        "SELECT 1 FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
        "WHERE d.task = ? AND t.state != 'done'",
        (task_id,),
    ).fetchone()


def top_ready(db: sqlite3.Connection, project: str) -> sqlite3.Row | None:
    rows = db.execute(
        "SELECT * FROM tasks WHERE project = ? AND state = 'open' ORDER BY priority DESC, n",
        (project,),
    )
    return next((row for row in rows if ready(db, row["id"])), None)


def set_state(
    db: sqlite3.Connection, task_id: str, state: str, now: float, *, keep_assignee: bool
) -> None:
    db.execute(
        "UPDATE tasks SET state = ?, assignee = CASE WHEN ? THEN assignee END, "
        "lease_until = CASE WHEN ? = 'in_progress' THEN lease_until END, "
        "version = version + 1, updated_at = ? WHERE id = ?",
        (state, keep_assignee, state, now, task_id),
    )


def add_event(
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


def add_notice(db: sqlite3.Connection, agent: str, text: str) -> None:
    db.execute("INSERT INTO notices(agent, text) VALUES(?, ?)", (agent, text))


def reclaim(db: sqlite3.Connection, now: float) -> None:
    expired = db.execute(
        "SELECT id, title, creator, assignee FROM tasks "
        "WHERE state = 'in_progress' AND lease_until < ?",
        (now,),
    ).fetchall()
    for task in expired:
        set_state(db, task["id"], "open", now, keep_assignee=False)
        add_event(db, task["id"], now, "board", "reclaimed", f"lease of {task['assignee']} expired")
        add_notice(
            db,
            task["creator"],
            f"{task['id']} went back to open: {task['assignee']}'s claim expired",
        )
        log.info("reclaimed %s from %s", task["id"], task["assignee"])


def task_text(db: sqlite3.Connection, task: sqlite3.Row, now: float) -> str:
    """One task whole as text: state, holder, claim, dependencies, body and recent events."""
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
