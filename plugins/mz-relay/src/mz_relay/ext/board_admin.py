"""The board's user actions: projects, access and overriding any task, from the CLI or the UI."""

from __future__ import annotations

import re
from collections.abc import Sequence

from mz_relay.ext import board_data as data
from mz_relay.ext.board_data import board_args
from mz_relay.extensions import ExtensionError, HumanAction, HumanCall

PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")


class BoardAdmin:
    def actions(self) -> Sequence[HumanAction]:
        return [
            HumanAction("create_project", self.create_project),
            HumanAction("archive_project", self.archive_project),
            HumanAction("set_access", self.set_access),
            HumanAction("move", self.move),
            HumanAction("reprioritize", self.reprioritize),
            HumanAction("reassign", self.reassign),
            HumanAction("approve", self.approve),
            HumanAction("cancel", self.cancel),
            HumanAction("overview", self.overview, mutates=False),
            HumanAction("task", self.task_detail, mutates=False),
        ]

    def create_project(self, call: HumanCall) -> dict[str, object]:
        args = board_args(call.args, {"name", "title", "default_access"})
        name = args.text("name")
        if PROJECT_RE.fullmatch(name) is None:
            raise ExtensionError("invalid_args", f"project name must match {PROJECT_RE.pattern}")
        access = args.choice("default_access", data.ACCESS, "none")
        with data.transaction(call.db):
            if call.db.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
                raise ExtensionError("conflict", f"project {name} exists")
            call.db.execute(
                "INSERT INTO projects(name, title, default_access, created_at) VALUES(?, ?, ?, ?)",
                (name, args.optional_text("title") or name, access, call.now),
            )
        return {"text": f"created project {name}, default access {access}"}

    def archive_project(self, call: HumanCall) -> dict[str, object]:
        name = board_args(call.args, {"name"}).text("name")
        with data.transaction(call.db):
            if not call.db.execute(
                "UPDATE projects SET archived = 1 WHERE name = ?", (name,)
            ).rowcount:
                raise data.not_found(f"project {name}")
        return {"text": f"archived project {name}; agents can still read it"}

    def set_access(self, call: HumanCall) -> dict[str, object]:
        args = board_args(call.args, {"project", "agent", "access"})
        project, agent = args.text("project"), args.optional_text("agent")
        access = args.choice("access", [*data.ACCESS, "inherit"])
        db = call.db
        with data.transaction(db):
            if not db.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone():
                raise data.not_found(f"project {project}")
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
        args = board_args(call.args, {"id", "state"})
        state = args.choice("state", data.STATES)
        with data.transaction(call.db):
            task = data.any_task(call.db, args.text("id"))
            data.set_state(call.db, task["id"], state, call.now, keep_assignee=state != "open")
            data.add_event(call.db, task["id"], call.now, "user", f"moved_{state}")
        return {"text": f"moved {task['id']} to {state}"}

    def reprioritize(self, call: HumanCall) -> dict[str, object]:
        args = board_args(call.args, {"id", "priority"})
        priority = args.integer("priority", 0, data.PRIORITY_MIN, data.PRIORITY_MAX)
        with data.transaction(call.db):
            task = data.any_task(call.db, args.text("id"))
            call.db.execute(
                "UPDATE tasks SET priority = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (priority, call.now, task["id"]),
            )
            data.add_event(call.db, task["id"], call.now, "user", "reprioritized", str(priority))
        return {"text": f"{task['id']} priority is now {priority}"}

    def reassign(self, call: HumanCall) -> dict[str, object]:
        args = board_args(call.args, {"id", "agent"})
        agent = args.optional_text("agent")
        db = call.db
        with data.transaction(db):
            task = data.any_task(db, args.text("id"))
            if agent is None:
                data.set_state(db, task["id"], "open", call.now, keep_assignee=False)
            else:
                db.execute(
                    "UPDATE tasks SET assignee = ?, state = CASE state WHEN 'open' THEN "
                    "'in_progress' ELSE state END, lease_until = ?, version = version + 1, "
                    "updated_at = ? WHERE id = ?",
                    (agent, call.now + data.LEASE_S, call.now, task["id"]),
                )
                data.add_notice(db, agent, f"your user gave you {task['id']}: {task['title']}")
            data.add_event(db, task["id"], call.now, "user", "reassigned", agent)
        return {"text": f"{task['id']} is now with {agent or 'nobody'}"}

    def approve(self, call: HumanCall) -> dict[str, object]:
        task_id = board_args(call.args, {"id"}).text("id")
        with data.transaction(call.db):
            task = data.any_task(call.db, task_id)
            if task["state"] != "review":
                raise ExtensionError("conflict", f"{task_id} is {task['state']}, not in review")
            data.set_state(call.db, task_id, "done", call.now, keep_assignee=True)
            data.add_event(call.db, task_id, call.now, "user", "approve")
        return {"text": f"approved {task_id}"}

    def cancel(self, call: HumanCall) -> dict[str, object]:
        args = board_args(call.args, {"id", "reason"})
        with data.transaction(call.db):
            task = data.any_task(call.db, args.text("id"))
            data.set_state(call.db, task["id"], "cancelled", call.now, keep_assignee=True)
            data.add_event(
                call.db, task["id"], call.now, "user", "cancelled", args.optional_text("reason")
            )
        return {"text": f"cancelled {task['id']}"}

    def overview(self, call: HumanCall) -> dict[str, object]:
        """Every project with its grants and tasks; the user sees all, whatever the access."""
        board_args(call.args, set())
        db = call.db
        with data.transaction(db):
            data.reclaim(db, call.now)
            grants: dict[str, dict[str, str]] = {}
            for row in db.execute("SELECT project, agent, access FROM grants ORDER BY agent"):
                grants.setdefault(row["project"], {})[row["agent"]] = row["access"]
            projects = [
                {
                    "name": row["name"],
                    "title": row["title"],
                    "default_access": row["default_access"],
                    "archived": bool(row["archived"]),
                    "grants": grants.get(row["name"], {}),
                }
                for row in db.execute("SELECT * FROM projects ORDER BY name")
            ]
            tasks = [
                {
                    **{key: row[key] for key in data.TASK_FIELDS},
                    "ready": row["state"] == "open" and data.ready(db, row["id"]),
                }
                for row in db.execute("SELECT * FROM tasks ORDER BY project, priority DESC, n")
            ]
        text = f"{len(projects)} projects, {len(tasks)} tasks"
        return {"text": text, "projects": projects, "tasks": tasks}

    def task_detail(self, call: HumanCall) -> dict[str, object]:
        task_id = board_args(call.args, {"id"}).text("id")
        db = call.db
        task = data.any_task(db, task_id)
        deps = db.execute(
            "SELECT t.id, t.state FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
            "WHERE d.task = ? ORDER BY t.n",
            (task_id,),
        ).fetchall()
        events = db.execute(
            "SELECT at, actor, kind, note FROM task_events WHERE task = ? ORDER BY id", (task_id,)
        ).fetchall()
        return {
            "text": data.task_text(db, task, call.now),
            "task": {key: task[key] for key in (*data.TASK_FIELDS, "body", "acceptance")},
            "deps": [dict(dep) for dep in deps],
            "events": [dict(event) for event in events],
        }
