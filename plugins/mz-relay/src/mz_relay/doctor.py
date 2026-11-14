"""`mz-relay doctor`: checks the paths, roster, relayd and agent harnesses; never starts relayd."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from mz_relay.adapters.claude_registry import ClaudeRegistry, pid_alive
from mz_relay.adapters.codex_threads import CodexThreadMissing, lookup_thread
from mz_relay.client import RelayClient
from mz_relay.errors import DaemonUnavailable, PathError, RelayError
from mz_relay.paths import Paths, check_socket_path
from mz_relay.procs import Runner
from mz_relay.roster import AgentSpec, Harness, Roster, ToolMode, load_roster

log = logging.getLogger(__name__)

Level = Literal["ok", "warn", "error"]
LEVELS: frozenset[str] = frozenset(get_args(Level))
TESTED_VERSIONS: Mapping[str, str] = {
    "claude": "2.1.325",
    "codex": "0.169.0",
    "pi": "1.5.2",
    "opencode": "1.19.5",
}
VERSION_RE = re.compile(r"\d+\.\d+\.\d+", re.ASCII)
VERSION_TIMEOUT_S = 10.0
HELLO_TIMEOUT_S = 5.0
READ_ONLY_SANDBOX = "read-only"


@dataclass(frozen=True)
class Finding:
    level: Level
    topic: str
    text: str

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            allowed = ", ".join(sorted(LEVELS))
            raise ValueError(f"level must be one of {allowed}, got {self.level!r}")
        if not self.topic or not self.text:
            raise ValueError(f"topic and text must be non-empty, got {self.topic!r}: {self.text!r}")


class Doctor:
    """Reads the setup and reports what is wrong; never spawns relayd and never writes."""

    def __init__(
        self,
        paths: Paths,
        *,
        run: Runner,
        which: Callable[[str], str | None] = shutil.which,
        uid: int | None = None,
    ) -> None:
        if not isinstance(paths, Paths):
            raise ValueError(f"paths must be a Paths, got {type(paths).__name__}")
        if uid is not None and uid < 0:
            raise ValueError(f"uid must be non-negative, got {uid}")
        self._paths = paths
        self._run = run
        self._which = which
        self._uid = os.getuid() if uid is None else uid

    def check(self) -> list[Finding]:
        findings = list(self._paths_findings())
        roster = self._roster(findings)
        if not any(f.topic == "paths" and f.level == "error" for f in findings):
            findings.append(self._daemon())
        if roster is not None:
            for spec in roster.agents.values():
                findings.extend(self._agent(spec))
        findings.extend(self._versions(roster))
        findings.extend(self._install())
        errors = sum(f.level == "error" for f in findings)
        warns = sum(f.level == "warn" for f in findings)
        log.info("doctor found %d error(s) and %d warning(s)", errors, warns)
        return findings

    def _paths_findings(self) -> Iterator[Finding]:
        paths = self._paths
        try:
            check_socket_path(paths.socket)
        except PathError as exc:
            yield Finding("error", "paths", exc.message)
        else:
            yield Finding("ok", "paths", f"socket {paths.socket}")
        yield Finding("ok", "paths", f"db {paths.db}, roster {paths.roster}")
        # relayd refuses a dir another uid owns; a missing one it creates itself.
        for directory in (paths.state_dir, paths.runtime_dir):
            try:
                owner = directory.stat().st_uid
            except FileNotFoundError:
                continue
            if owner != self._uid:
                text = f"{directory} is owned by uid {owner}, not {self._uid}; relayd refuses it"
                yield Finding("error", "paths", text)

    def _roster(self, findings: list[Finding]) -> Roster | None:
        try:
            roster = load_roster(self._paths.roster, pw_home=self._paths.pw_home)
        except RelayError as exc:
            findings.append(Finding("error", "roster", exc.message))
            return None
        names = ", ".join(sorted(roster.agents))
        findings.append(Finding("ok", "roster", f"{len(roster.agents)} agents: {names}"))
        return roster

    def _daemon(self) -> Finding:
        socket_path = self._paths.socket
        if not socket_path.exists():
            return Finding("warn", "daemon", f"relayd is not running (no socket at {socket_path})")
        try:
            with closing(RelayClient(socket_path, timeout_s=HELLO_TIMEOUT_S)) as client:
                hello = client.connect(client="cli")
        except DaemonUnavailable as exc:
            return Finding("warn", "daemon", f"relayd is not running: {exc.message}")
        except RelayError as exc:
            log.info("relayd at %s refused hello: %s", socket_path, exc.code)
            return Finding("warn", "daemon", f"relayd refused hello: {exc.code}: {exc.message}")
        pid = hello.get("daemon_pid")
        db, roster = str(hello.get("db")), str(hello.get("roster"))
        if db != str(self._paths.db) or roster != str(self._paths.roster):
            return Finding(
                "warn",
                "daemon",
                f"environments differ: relayd pid {pid} uses db {db} and roster {roster}; "
                f"this shell uses db {self._paths.db} and roster {self._paths.roster}",
            )
        return Finding("ok", "daemon", f"relayd pid {pid}, version {hello.get('version')}")

    def _agent(self, spec: AgentSpec) -> Iterator[Finding]:
        if spec.harness is Harness.CLAUDE and spec.claude_config_dir is not None:
            yield self._claude(spec.name, spec.claude_config_dir)
        elif spec.harness is Harness.CODEX and spec.codex_home is not None:
            yield from self._codex(spec, spec.codex_home)

    def _claude(self, name: str, config_dir: Path) -> Finding:
        if not config_dir.is_dir():
            return Finding("warn", name, f"Claude config dir {config_dir} does not exist")
        live = sum(1 for entry in ClaudeRegistry(config_dir).entries() if pid_alive(entry.pid))
        return Finding("ok", name, f"{config_dir}: {live} live session(s)")

    def _codex(self, spec: AgentSpec, home: Path) -> Iterator[Finding]:
        if not home.is_dir():
            yield Finding("warn", spec.name, f"Codex home {home} does not exist")
        else:
            if spec.codex_thread is not None:
                yield self._thread(spec.name, home, spec.codex_thread)
            if spec.tools is ToolMode.READ_ONLY:
                sandbox = self._sandbox(spec.name, home / "config.toml")
                if sandbox is not None:
                    yield sandbox
        if self._which(spec.codex_bin) is None:
            yield Finding("warn", spec.name, f"codex_bin {spec.codex_bin} is not on PATH")

    def _thread(self, name: str, home: Path, thread: str) -> Finding:
        try:
            found = lookup_thread(home, thread)
        except CodexThreadMissing as exc:
            return Finding("warn", name, exc.message)
        except OSError as exc:
            return Finding("warn", name, f"cannot read the Codex session index in {home}: {exc}")
        if found.count > 1:
            return Finding(
                "warn",
                name,
                f"{found.count} Codex threads named {thread}; delivering to the newest, "
                f"{found.thread.id}",
            )
        return Finding("ok", name, f"Codex thread {thread} is {found.thread.id}")

    def _sandbox(self, name: str, config: Path) -> Finding | None:
        # Only the top-level key applies to a plain `codex` start; a profile's needs -p.
        need = f'a read_only agent needs top-level sandbox_mode = "{READ_ONLY_SANDBOX}" in {config}'
        try:
            doc = tomllib.loads(config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Finding("warn", name, f"{need}, which does not exist")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
            return Finding("warn", name, f"{need}, which cannot be read: {exc}")
        mode = doc.get("sandbox_mode")
        if mode != READ_ONLY_SANDBOX:
            return Finding("warn", name, f"{need}; found {mode!r}")
        return None

    def _versions(self, roster: Roster | None) -> Iterator[Finding]:
        # Keyed on the resolved binary, so a codex_bin that is the PATH codex is probed once.
        probes: dict[str, str] = {}
        for harness in TESTED_VERSIONS:
            found = self._which(harness)
            if found is None:
                log.debug("%s is not on PATH; skipping its version check", harness)
            else:
                probes.setdefault(found, harness)
        agents = [] if roster is None else roster.agents.values()
        for spec in agents:
            found = self._which(spec.codex_bin) if spec.harness is Harness.CODEX else None
            if found is not None:
                probes.setdefault(found, Harness.CODEX.value)
        for found, harness in probes.items():
            finding = self._version(harness, found)
            if finding is not None:
                yield finding

    def _version(self, harness: str, found: str) -> Finding | None:
        topic, tested = f"{harness} version", TESTED_VERSIONS[harness]
        try:
            done = self._run([found, "--version"], os.environ, VERSION_TIMEOUT_S)
        except FileNotFoundError:
            log.debug("%s vanished before its version check", found)
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            return Finding("warn", topic, f"cannot run {found} --version: {exc}")
        if done.returncode != 0:
            return Finding("warn", topic, f"{found} --version exited {done.returncode}")
        match = VERSION_RE.search(done.stdout or "")
        if match is None:
            return Finding("warn", topic, f"{found} --version printed no x.y.z version")
        version = match[0]
        if version != tested:
            return Finding("warn", topic, f"{version} at {found}; tested {tested}")
        return Finding("ok", topic, f"{version} at {found}")

    def _install(self) -> Iterator[Finding]:
        record = self._paths.install_record
        try:
            raw = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.debug("no install record at %s", record)
            return
        except (OSError, ValueError, RecursionError) as exc:
            yield Finding("warn", "install", f"cannot read {record}: {exc}")
            return
        bin_path = raw.get("bin") if isinstance(raw, dict) else None
        if not isinstance(bin_path, str) or not bin_path:
            yield Finding("warn", "install", f"{record} has no bin path")
        elif not Path(bin_path).is_file():
            yield Finding(
                "warn",
                "install",
                f"{bin_path} from {record} no longer exists; run mz-relay install again",
            )
        else:
            yield Finding("ok", "install", f"bin {bin_path}")
