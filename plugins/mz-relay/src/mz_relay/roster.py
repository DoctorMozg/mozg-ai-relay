"""roster.toml: the agents relayd knows, their policy, and a cache that reloads on edit."""

from __future__ import annotations

import logging
import re
import threading
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from mz_relay.errors import IdentityError, PathError, RosterError
from mz_relay.paths import expand_home

log = logging.getLogger(__name__)

ROSTER_VERSION = 1
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
# The human behind the CLI and UI; relayd routes it without a roster entry.
USER_NAME = "user"
ROLE_MAX = 40
DESCRIPTION_MAX = 300
DEFAULT_CODEX_BIN = "codex"


class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"
    OPENCODE = "opencode"


class Label(StrEnum):
    PERSONAL = "personal"
    CORPORATE = "corporate"
    LOCAL = "local"


class ToolMode(StrEnum):
    FULL = "full"
    READ_ONLY = "read_only"


@dataclass(frozen=True)
class PolicySettings:
    hold_corporate_to_personal: bool = True
    hop_budget: int = 4
    turn_cap: int = 20
    inbox_cap: int = 50
    default_ttl_s: int = 3600


POLICY_RANGES: Mapping[str, tuple[int, int]] = {
    "hop_budget": (1, 16),
    "turn_cap": (1, 1000),
    "inbox_cap": (1, 1000),
    "default_ttl_s": (60, 604800),
}
POLICY_KEYS = frozenset({"hold_corporate_to_personal", *POLICY_RANGES})
TOP_LEVEL_KEYS = frozenset({"version", "policy", "agents"})
COMMON_AGENT_KEYS = frozenset(
    {"harness", "label", "role", "description", "may_send_to", "tools", "cwd"}
)
HARNESS_KEYS: Mapping[Harness, frozenset[str]] = {
    Harness.CLAUDE: frozenset({"claude_config_dir"}),
    Harness.CODEX: frozenset({"codex_home", "codex_thread", "codex_bin"}),
    Harness.PI: frozenset(),
    Harness.OPENCODE: frozenset(),
}
AGENT_KEYS = COMMON_AGENT_KEYS.union(*HARNESS_KEYS.values())


@dataclass(frozen=True)
class AgentSpec:
    name: str
    harness: Harness
    label: Label
    role: str
    description: str
    may_send_to: frozenset[str]
    tools: ToolMode
    cwd: Path | None
    claude_config_dir: Path | None
    codex_home: Path | None
    codex_thread: str | None
    codex_bin: str

    def card(self) -> dict[str, str]:
        """The fields every peer may see about this agent."""
        return {
            "name": self.name,
            "role": self.role,
            "harness": self.harness.value,
            "label": self.label.value,
            "description": self.description,
            "tools": self.tools.value,
        }


@dataclass(frozen=True)
class Roster:
    agents: Mapping[str, AgentSpec]
    policy: PolicySettings

    def get(self, name: str) -> AgentSpec:
        spec = self.agents.get(name)
        if spec is None:
            raise IdentityError("unknown_agent", f"{name!r} is not in the roster")
        return spec


class _RosterParser:
    """Validates one parsed TOML document; every error names the offending key."""

    def __init__(self, pw_home: Path, source: str) -> None:
        self.pw_home = pw_home
        self.source = source

    def error(self, key: str, message: str) -> RosterError:
        return RosterError(f"{self.source}: {message}", key=key)

    def parse(self, doc: Mapping[str, object]) -> Roster:
        self.check_version(doc)
        unknown = sorted(set(doc) - TOP_LEVEL_KEYS)
        if unknown:
            raise self.error(unknown[0], f"unknown top-level key {unknown[0]!r}")
        policy = self.parse_policy(self.table(doc, "policy", required=False))
        agents_table = self.table(doc, "agents", required=True)
        if not agents_table:
            raise self.error("agents", "the roster needs at least one [agents.<name>] table")
        names = frozenset(agents_table)
        agents = {name: self.parse_agent(name, raw, names) for name, raw in agents_table.items()}
        return Roster(agents=MappingProxyType(agents), policy=policy)

    def check_version(self, doc: Mapping[str, object]) -> None:
        if "version" not in doc:
            raise self.error("version", f"the roster needs version = {ROSTER_VERSION}")
        version = doc["version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise self.error("version", f"version must be an integer, got {version!r}")
        if version != ROSTER_VERSION:
            raise self.error(
                "version", f"unsupported roster version {version}; expected {ROSTER_VERSION}"
            )

    def table(self, doc: Mapping[str, object], key: str, *, required: bool) -> dict[str, object]:
        if key not in doc:
            if required:
                raise self.error(key, f"the roster needs a [{key}] table")
            return {}
        value = doc[key]
        if not isinstance(value, dict):
            raise self.error(key, f"{key} must be a table, got {type(value).__name__}")
        return value

    def parse_policy(self, raw: Mapping[str, object]) -> PolicySettings:
        unknown = sorted(set(raw) - POLICY_KEYS)
        if unknown:
            raise self.error(f"policy.{unknown[0]}", f"unknown [policy] key {unknown[0]!r}")
        defaults = PolicySettings()
        hold = raw.get("hold_corporate_to_personal", defaults.hold_corporate_to_personal)
        if not isinstance(hold, bool):
            key = "policy.hold_corporate_to_personal"
            raise self.error(key, f"{key} must be true or false, got {hold!r}")
        limits = {
            name: self.bounded_int(raw, name, getattr(defaults, name), bounds)
            for name, bounds in POLICY_RANGES.items()
        }
        return PolicySettings(
            hold_corporate_to_personal=hold,
            hop_budget=limits["hop_budget"],
            turn_cap=limits["turn_cap"],
            inbox_cap=limits["inbox_cap"],
            default_ttl_s=limits["default_ttl_s"],
        )

    def bounded_int(
        self, raw: Mapping[str, object], name: str, default: int, bounds: tuple[int, int]
    ) -> int:
        key = f"policy.{name}"
        value = raw.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise self.error(key, f"{key} must be an integer, got {value!r}")
        low, high = bounds
        if not low <= value <= high:
            raise self.error(key, f"{key} must be between {low} and {high}, got {value}")
        return value

    def parse_agent(self, name: str, raw: object, names: frozenset[str]) -> AgentSpec:
        prefix = f"agents.{name}"
        if not NAME_RE.fullmatch(name):
            raise self.error(
                prefix,
                f"agent name {name!r} must be 1-32 characters of a-z, 0-9 and -, "
                "starting with a letter",
            )
        if name == USER_NAME:
            raise self.error(prefix, f"agent name {name!r} is reserved for the human user")
        if not isinstance(raw, dict):
            raise self.error(prefix, f"{prefix} must be a table, got {type(raw).__name__}")
        unknown = sorted(set(raw) - AGENT_KEYS)
        if unknown:
            raise self.error(f"{prefix}.{unknown[0]}", f"unknown agent key {unknown[0]!r}")
        harness = self.choice(raw, prefix, "harness", Harness, default=None)
        foreign = sorted(set(raw) & (AGENT_KEYS - COMMON_AGENT_KEYS - HARNESS_KEYS[harness]))
        if foreign:
            key = f"{prefix}.{foreign[0]}"
            raise self.error(key, f"{key} does not apply to a {harness} agent")
        codex = harness is Harness.CODEX
        return AgentSpec(
            name=name,
            harness=harness,
            label=self.choice(raw, prefix, "label", Label, default=None),
            role=self.text(raw, prefix, "role", max_len=ROLE_MAX, required=True),
            description=self.text(
                raw, prefix, "description", max_len=DESCRIPTION_MAX, required=True
            ),
            may_send_to=self.may_send_to(raw, prefix, name, names),
            tools=self.choice(raw, prefix, "tools", ToolMode, default=ToolMode.FULL),
            cwd=self.real_path(raw, prefix, "cwd"),
            claude_config_dir=self.real_path(
                raw, prefix, "claude_config_dir", required=harness is Harness.CLAUDE
            ),
            codex_home=self.path(
                raw, prefix, "codex_home", default=self.pw_home / ".codex" if codex else None
            ),
            codex_thread=self.text(raw, prefix, "codex_thread", required=codex) or None,
            codex_bin=self.codex_bin(raw, prefix),
        )

    def choice[E: StrEnum](
        self, raw: Mapping[str, object], prefix: str, key: str, enum: type[E], *, default: E | None
    ) -> E:
        full = f"{prefix}.{key}"
        value = raw.get(key)
        if value is None:
            if default is None:
                raise self.error(full, f"{full} is required")
            return default
        allowed = ", ".join(member.value for member in enum)
        if not isinstance(value, str) or value not in {member.value for member in enum}:
            raise self.error(full, f"{full} must be one of {allowed}, got {value!r}")
        return enum(value)

    def text(
        self,
        raw: Mapping[str, object],
        prefix: str,
        key: str,
        *,
        required: bool,
        max_len: int | None = None,
    ) -> str:
        full = f"{prefix}.{key}"
        value = raw.get(key)
        if value is None:
            if required:
                raise self.error(full, f"{full} is required")
            return ""
        if not isinstance(value, str) or not value:
            raise self.error(full, f"{full} must be a non-empty string, got {value!r}")
        if max_len is not None and len(value) > max_len:
            raise self.error(full, f"{full} must be at most {max_len} characters, got {len(value)}")
        return value

    def path(
        self,
        raw: Mapping[str, object],
        prefix: str,
        key: str,
        *,
        default: Path | None,
        required: bool = False,
    ) -> Path | None:
        full = f"{prefix}.{key}"
        value = self.text(raw, prefix, key, required=required)
        if not value:
            return default
        try:
            expanded = expand_home(value, self.pw_home)
        except PathError as exc:
            raise self.error(full, f"{full}: {exc.message}") from exc
        # Relative paths would resolve against relayd's cwd, which is never the agent's.
        if not expanded.is_absolute():
            raise self.error(full, f"{full} must be absolute or start with ~/, got {value!r}")
        return expanded

    def real_path(
        self, raw: Mapping[str, object], prefix: str, key: str, *, required: bool = False
    ) -> Path | None:
        # Identity compares these with a session's real paths; resolving here makes a symlink
        # loop fail the roster once instead of every later registration.
        path = self.path(raw, prefix, key, default=None, required=required)
        if path is None:
            return None
        full = f"{prefix}.{key}"
        try:
            return path.resolve()
        except (RuntimeError, OSError) as exc:
            raise self.error(full, f"{full}: cannot resolve {path}: {exc}") from exc

    def codex_bin(self, raw: Mapping[str, object], prefix: str) -> str:
        value = self.text(raw, prefix, "codex_bin", required=False) or DEFAULT_CODEX_BIN
        if "/" in value and not value.startswith("/"):
            key = f"{prefix}.codex_bin"
            raise self.error(key, f"{key} must be an absolute path or a PATH name, got {value!r}")
        return value

    def may_send_to(
        self, raw: Mapping[str, object], prefix: str, name: str, names: frozenset[str]
    ) -> frozenset[str]:
        key = f"{prefix}.may_send_to"
        value = raw.get("may_send_to")
        if value is None:
            return names - {name}
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise self.error(key, f"{key} must be a list of agent names, got {value!r}")
        missing = sorted(set(value) - names)
        if missing:
            raise self.error(key, f"{key} names agents not in the roster: {', '.join(missing)}")
        return frozenset(value)


def parse_roster(text: str, *, pw_home: Path, source: str = "<string>") -> Roster:
    """Parses and validates roster TOML; `~` paths expand against pw_home."""
    if not pw_home.is_absolute():
        raise PathError(f"passwd home must be absolute, got {pw_home}")
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RosterError(f"{source}: invalid TOML: {exc}") from exc
    return _RosterParser(pw_home, source).parse(doc)


def load_roster(path: Path, *, pw_home: Path) -> Roster:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RosterError(f"cannot read roster {path}: {exc}", path=path) from exc
    try:
        return parse_roster(text, pw_home=pw_home, source=str(path))
    except RosterError as exc:
        raise RosterError(exc.message, path=path, key=exc.key) from exc


class RosterCache:
    """The last good roster at `path`, reloaded when the file's mtime changes."""

    def __init__(self, path: Path, pw_home: Path) -> None:
        self._path = path
        self._pw_home = pw_home
        self._lock = threading.Lock()
        self._seen_mtime: int | None = self._mtime()
        self._roster = load_roster(path, pw_home=pw_home)
        log.info("roster loaded from %s: %d agents", path, len(self._roster.agents))

    def _mtime(self) -> int:
        try:
            return self._path.stat().st_mtime_ns
        except OSError as exc:
            raise RosterError(f"cannot read roster {self._path}: {exc}", path=self._path) from exc

    def current(self) -> Roster:
        with self._lock:
            try:
                mtime = self._mtime()
            except RosterError as exc:
                if self._seen_mtime is not None:
                    log.warning("%s; keeping the last good roster", exc.message)
                    self._seen_mtime = None
                return self._roster
            # Remembering a failed mtime too means a broken edit warns once, not on every request.
            if mtime != self._seen_mtime:
                self._seen_mtime = mtime
                try:
                    self._roster = load_roster(self._path, pw_home=self._pw_home)
                except RosterError as exc:
                    log.warning("roster reload failed, keeping the last good roster: %s", exc)
                else:
                    count = len(self._roster.agents)
                    log.info("roster reloaded from %s: %d agents", self._path, count)
            return self._roster
