"""Extensions: in-process add-ons with an SQLite file, agent tools, human actions and events."""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mz_relay.errors import PolicyViolation, RelayError
from mz_relay.roster import EXTENSION_NAME_RE, AgentSpec, ToolMode
from mz_relay.store import Event, Store

log = logging.getLogger(__name__)

TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
EVENT_BATCH = 200
EVENT_WAIT_S = 1.0
CURSOR_DDL = (
    "CREATE TABLE IF NOT EXISTS mz_ext_cursor("
    "id INTEGER PRIMARY KEY CHECK (id = 1), seq INTEGER NOT NULL)"
)


class ExtensionError(RelayError):
    """An extension refused a call; its code is the extension's own, e.g. not_found or conflict."""


@dataclass(frozen=True)
class ToolCall:
    """One agent tool call: the caller's roster entry, its arguments, the time and the DB."""

    caller: AgentSpec
    args: Mapping[str, object]
    now: float
    db: sqlite3.Connection


@dataclass(frozen=True)
class HumanCall:
    """One action of the user, from the CLI or the UI; never reachable by an agent."""

    args: Mapping[str, object]
    now: float
    db: sqlite3.Connection


def object_schema(properties: dict[str, object], required: Sequence[str] = ()) -> dict[str, object]:
    """A closed JSON object schema for a tool's input."""
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


class ExtArgs:
    """One call's arguments; a bad one is invalid_args naming the field."""

    def __init__(
        self,
        args: Mapping[str, object],
        allowed: set[str],
        limits: Mapping[str, int] | None = None,
    ) -> None:
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ExtensionError("invalid_args", f"unknown arguments: {', '.join(unknown)}")
        self.args = args
        self.limits: Mapping[str, int] = limits or {}

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
        limit = self.limits.get(name)
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

    def integer(self, name: str, default: int, low: int, high: int) -> int:
        value = self.args.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ExtensionError("invalid_args", f"{name} must be an integer in {low}..{high}")
        return value

    def strings(self, name: str) -> list[str]:
        """A list of strings, duplicates dropped in order."""
        value = self.args.get(name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ExtensionError("invalid_args", f"{name} must be a list of strings")
        return list(dict.fromkeys(value))

    def flag(self, name: str) -> bool:
        value = self.args.get(name, False)
        if not isinstance(value, bool):
            raise ExtensionError("invalid_args", f"{name} must be true or false")
        return value


@dataclass(frozen=True)
class ExtTool:
    name: str
    description: str
    input_schema: Mapping[str, object]
    # Mutating tools are hidden from and refused for read_only agents.
    mutates: bool
    handler: Callable[[ToolCall], dict[str, object]]

    def __post_init__(self) -> None:
        if TOOL_NAME_RE.fullmatch(self.name) is None:
            raise ValueError(f"tool name must match {TOOL_NAME_RE.pattern}, got {self.name!r}")


@dataclass(frozen=True)
class HumanAction:
    name: str
    handler: Callable[[HumanCall], dict[str, object]]
    # Reads leave no event, so a UI that reloads on events does not loop.
    mutates: bool = True


class Extension(Protocol):
    name: str
    schema_version: int

    def migrate(self, db: sqlite3.Connection, from_version: int) -> None: ...
    def tools(self) -> Sequence[ExtTool]: ...
    def human_actions(self) -> Sequence[HumanAction]: ...
    # One line for the caller's brief on how to use the tools its role sees; "" adds none.
    def brief(self, caller: AgentSpec) -> str: ...
    def on_event(self, db: sqlite3.Connection, event: Event, now: float) -> None: ...


class _Loaded:
    """One running extension: its DB connection, behind a lock so its calls run one at a time."""

    def __init__(self, ext: Extension, db_path: Path) -> None:
        self.ext = ext
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self.tools = {tool.name: tool for tool in ext.tools()}
        self.actions = {action.name: action for action in ext.human_actions()}

    def _migrate(self) -> None:
        ext = self.ext
        current = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if current > ext.schema_version:
            raise RuntimeError(
                f"extension {ext.name} DB is at version {current}, newer than {ext.schema_version}"
            )
        self.db.execute(CURSOR_DDL)
        if current == ext.schema_version:
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ext.migrate(self.db, current)
            self.db.execute(f"PRAGMA user_version = {int(ext.schema_version)}")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")
        log.info("extension %s migrated from %d to %d", ext.name, current, ext.schema_version)

    def cursor(self) -> int | None:
        row = self.db.execute("SELECT seq FROM mz_ext_cursor WHERE id = 1").fetchone()
        return None if row is None else int(row[0])

    def save_cursor(self, seq: int) -> None:
        self.db.execute(
            "INSERT INTO mz_ext_cursor(id, seq) VALUES(1, ?) "
            "ON CONFLICT(id) DO UPDATE SET seq = excluded.seq",
            (seq,),
        )


class Extensions:
    """The extensions relayd runs; fixed at start, as harnesses fix their tool list at connect."""

    def __init__(self, extensions: Iterable[Extension], db_dir: Path) -> None:
        self._loaded: dict[str, _Loaded] = {}
        for ext in extensions:
            if EXTENSION_NAME_RE.fullmatch(ext.name) is None:
                raise ValueError(
                    f"extension name must match {EXTENSION_NAME_RE.pattern}: {ext.name!r}"
                )
            if ext.name in self._loaded:
                raise ValueError(f"extension {ext.name} is listed twice")
            if not self._loaded:
                db_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._loaded[ext.name] = _Loaded(ext, db_dir / f"{ext.name}.db")

    @classmethod
    def none(cls) -> Extensions:
        return cls((), Path())

    def names(self) -> list[str]:
        return sorted(self._loaded)

    def close(self) -> None:
        for loaded in self._loaded.values():
            with loaded.lock:
                loaded.db.close()

    def brief_lines(self, caller: AgentSpec) -> list[str]:
        """Each extension's brief line for this agent, in name order; empty ones are left out."""
        lines = (self._loaded[name].ext.brief(caller) for name in self.names())
        return [f"- {line}" for line in lines if line]

    def tools_for(self, caller: AgentSpec) -> list[dict[str, object]]:
        """The MCP tool entries this agent may call, named <extension>_<tool>."""
        listed: list[dict[str, object]] = []
        for name in self.names():
            for tool in self._loaded[name].tools.values():
                if tool.mutates and caller.tools is ToolMode.READ_ONLY:
                    continue
                listed.append(
                    {
                        "name": f"{name}_{tool.name}",
                        "description": tool.description,
                        "inputSchema": dict(tool.input_schema),
                    }
                )
        return listed

    def mutates(self, full_name: str) -> bool:
        return self._tool(full_name)[1].mutates

    def human_mutates(self, ext_name: str, action_name: str) -> bool:
        loaded = self._loaded.get(ext_name)
        action = None if loaded is None else loaded.actions.get(action_name)
        return action is not None and action.mutates

    def call(
        self, caller: AgentSpec, full_name: str, args: Mapping[str, object], now: float
    ) -> dict[str, object]:
        loaded, tool = self._tool(full_name)
        if tool.mutates and caller.tools is ToolMode.READ_ONLY:
            raise PolicyViolation(
                "not_allowed", f"{full_name} changes state; {caller.name} has read_only tools"
            )
        with loaded.lock:
            return self._run(
                full_name, lambda: tool.handler(ToolCall(caller, args, now, loaded.db))
            )

    def human(
        self, ext_name: str, action_name: str, args: Mapping[str, object], now: float
    ) -> dict[str, object]:
        loaded = self._loaded.get(ext_name)
        action = None if loaded is None else loaded.actions.get(action_name)
        if loaded is None or action is None:
            raise ExtensionError(
                "not_found", f"no action {action_name!r} in extension {ext_name!r}"
            )
        with loaded.lock:
            label = f"{ext_name}.{action_name}"
            return self._run(label, lambda: action.handler(HumanCall(args, now, loaded.db)))

    def pump(self, store: Store, now: float) -> int:
        """Hands every extension the events after its cursor; returns how many it handed over."""
        handed = 0
        for name in self.names():
            loaded = self._loaded[name]
            with loaded.lock:
                handed += self._pump_one(loaded, store, now)
        return handed

    def _pump_one(self, loaded: _Loaded, store: Store, now: float) -> int:
        since = loaded.cursor()
        page = store.events_after(since, EVENT_BATCH)
        if since is None:
            # A new extension starts at the head; it does not replay the history before it.
            loaded.save_cursor(page.last_seq)
            return 0
        if page.gap:
            log.warning("extension %s missed events before seq %d", loaded.ext.name, page.last_seq)
        for event in page.events:
            try:
                loaded.ext.on_event(loaded.db, event, now)
            except Exception:
                log.exception("extension %s failed on event %d", loaded.ext.name, event.seq)
            # The cursor moves past a failing event too, so one bad event cannot wedge the feed.
            loaded.save_cursor(event.seq)
        if page.gap and not page.events:
            loaded.save_cursor(page.last_seq)
        return len(page.events)

    def _tool(self, full_name: str) -> tuple[_Loaded, ExtTool]:
        ext_name, _, tool_name = full_name.partition("_")
        loaded = self._loaded.get(ext_name)
        tool = None if loaded is None else loaded.tools.get(tool_name)
        if loaded is None or tool is None:
            raise ExtensionError("not_found", f"no extension tool {full_name!r}")
        return loaded, tool

    @staticmethod
    def _run(label: str, run: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            result = run()
        except RelayError:
            raise
        except Exception as exc:
            log.exception("extension call %s raised", label)
            raise ExtensionError("ext_failed", f"{label} failed with {type(exc).__name__}") from exc
        if not isinstance(result.get("text"), str):
            raise ExtensionError("ext_failed", f"{label} returned no text")
        return result


def discover(enabled: Sequence[str]) -> list[Extension]:
    """The enabled extensions found under mz_relay.ext; each module exports EXTENSION."""
    import mz_relay.ext as package

    found = {info.name for info in pkgutil.iter_modules(package.__path__)}
    extensions: list[Extension] = []
    for name in enabled:
        if name not in found:
            log.error("roster enables extension %r, which does not exist; skipped", name)
            continue
        module = importlib.import_module(f"mz_relay.ext.{name}")
        extensions.append(module.EXTENSION)
    return extensions


def open_extensions(enabled: Sequence[str], state_dir: Path) -> Extensions:
    return Extensions(discover(enabled), state_dir / "ext")
