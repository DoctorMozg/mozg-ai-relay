"""doctor: the setup checks, their findings, and the `mz-relay doctor` output and exit code."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from codex_rig import index_line, write_index
from mz_relay.cli import main
from mz_relay.doctor import TESTED_VERSIONS, Doctor, Finding
from mz_relay.paths import Paths
from relay_rig import (
    FakeRelayd,
    Message,
    Reply,
    close_connection,
    fail,
    hello_result,
    ok,
    paths_in,
    pull_roster,
    running_daemon,
)

# What each harness prints for `--version` at the tested release.
TESTED_OUTPUT = {
    "claude": "2.1.325 (Claude Code)\n",
    "codex": "codex-cli 0.169.0\n",
    "pi": "1.5.2\n",
    "opencode": "1.19.5\n",
}
CLI_LINE = re.compile(r"(ok|warn|error)  [^:\n]+: \S.*")
BIN_DIR = "/opt/harness/bin"
# Deep enough that a recursive JSON or TOML parser hits Python's recursion limit.
DEEP = 100_000

Serve = Callable[[Reply], FakeRelayd]


@dataclass
class FakeBins:
    """Harness binaries on a fake PATH: each name maps to what its `--version` prints."""

    outputs: dict[str, str] = field(default_factory=lambda: dict(TESTED_OUTPUT))
    exit_codes: dict[str, int] = field(default_factory=dict)
    # Found by which but gone by the time it runs, e.g. a dangling symlink.
    vanished: set[str] = field(default_factory=set)
    # Absolute binaries outside BIN_DIR, such as a second account's codex_bin.
    elsewhere: dict[str, str] = field(default_factory=dict)
    runs: list[list[str]] = field(default_factory=list)

    def which(self, cmd: str) -> str | None:
        if cmd in self.elsewhere:
            return cmd
        on_path = "/" not in cmd or str(Path(cmd).parent) == BIN_DIR
        name = Path(cmd).name
        return f"{BIN_DIR}/{name}" if on_path and name in self.outputs else None

    def run(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        self.runs.append(list(argv))
        found = self.which(argv[0])
        name = Path(argv[0]).name
        if found is None or name in self.vanished:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        if list(argv[1:]) != ["--version"]:
            return subprocess.CompletedProcess(list(argv), 2, "", f"unknown args {argv[1:]}\n")
        output = self.elsewhere.get(found) or self.outputs[name]
        return subprocess.CompletedProcess(list(argv), self.exit_codes.get(name, 0), output, "")


class DoctorSetup:
    """A relay home whose roster has one agent per harness, made healthy or broken per test."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        home = paths.config_dir.resolve()
        self.claude_dir = home / "claude-a"
        self.codex_home = home / "codex-r"
        self.codex_config = self.codex_home / "config.toml"
        self.bin = home / "plugin" / "bin" / "mz-relay"
        self.bins = FakeBins()
        self.uid = os.getuid()

    def roster(self, *, reviewer_tools: str = "read_only", codex_bin: str = "codex") -> str:
        return f"""\
version = 1

[agents.architect]
harness = "claude"
label = "personal"
role = "architect"
description = "Owns design decisions."
claude_config_dir = "{self.claude_dir}"

[agents.reviewer]
harness = "codex"
label = "personal"
role = "reviewer"
description = "Reviews diffs."
codex_home = "{self.codex_home}"
codex_thread = "reviewer"
codex_bin = "{codex_bin}"
tools = "{reviewer_tools}"

[agents.scout]
harness = "pi"
label = "local"
role = "scout"
description = "Searches logs."

[agents.helper]
harness = "opencode"
label = "personal"
role = "helper"
description = "Answers docs questions."
"""

    def write_roster(self, text: str | None = None) -> None:
        self.paths.roster.write_text(self.roster() if text is None else text, encoding="utf-8")

    def live_session(self, pid: int, *, file_name: str | None = None) -> None:
        """A Claude registry entry for pid under the architect's config dir."""
        self.session_file(file_name or f"{pid}.json", json.dumps(session_entry(pid)))

    def session_file(self, name: str, text: str) -> None:
        sessions = self.claude_dir / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / name).write_text(text, encoding="utf-8")

    def sandbox(self, text: str) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.codex_config.write_text(text, encoding="utf-8")

    def install_record(self, *, bin_exists: bool = True) -> None:
        if bin_exists:
            self.bin.parent.mkdir(parents=True, exist_ok=True)
            self.bin.write_text("#!/bin/sh\n", encoding="utf-8")
            self.bin.chmod(0o755)
        record = {"bin": str(self.bin), "version": "0.1.0"}
        self.paths.install_record.write_text(json.dumps(record), encoding="utf-8")

    def healthy(self) -> None:
        self.write_roster()
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        self.live_session(os.getpid())
        write_index(self.codex_home, index_line("t-1", "reviewer"))
        self.sandbox('sandbox_mode = "read-only"\n')
        self.install_record()

    def check(self) -> list[Finding]:
        """Runs doctor; when nothing served the socket before, it must leave no daemon trace."""
        served = self.paths.socket.exists()
        doctor = Doctor(self.paths, run=self.bins.run, which=self.bins.which, uid=self.uid)
        findings = list(doctor.check())
        if not served:
            assert spawn_traces(self.paths) == [], shown(findings)
        return findings

    def with_socket_bytes(self, size: int) -> DoctorSetup:
        """The same setup with a runtime dir padded so the socket path is `size` bytes."""
        home = self.paths.config_dir
        pad = size - len(os.fsencode(home)) - 2 - len(os.fsencode(self.paths.socket.name))
        moved = DoctorSetup(replace(self.paths, runtime_dir=home / ("r" * pad)))
        assert len(os.fsencode(moved.paths.socket)) == size
        return moved


def session_entry(pid: int) -> dict[str, object]:
    return {
        "pid": pid,
        "sessionId": f"s-{pid}",
        "cwd": "/tmp/x",
        "messagingSocketPath": f"/tmp/mzr-{pid}.sock",
        "version": "2.1.323",
        "peerProtocol": 1,
        "status": "idle",
    }


def spawn_traces(paths: Paths) -> list[Path]:
    """Files a started or starting relayd leaves behind."""
    traces = (paths.socket, paths.spawn_lock, paths.lock, paths.log, paths.db)
    return [path for path in traces if path.exists()]


def hello_with(*, db: str, roster: str) -> Reply:
    """A relayd that says hello with these paths and refuses every other op."""

    def reply(req: Message) -> Message:
        if req["op"] == "hello":
            return ok(req, {**hello_result(), "db": db, "roster": roster})
        return fail(req, "unknown_op", f"unknown op {req['op']!r}")

    return reply


def refusing_hello(req: Message) -> Message:
    return fail(req, "protocol_mismatch", "relayd speaks protocol 2", {"daemon_protocol": 2})


def failing_hello(req: Message) -> Message:
    return fail(req, "internal", "relayd broke")


def of(findings: Sequence[Finding], topic: str, level: str | None = None) -> list[Finding]:
    return [f for f in findings if f.topic == topic and (level is None or f.level == level)]


def text_of(findings: Sequence[Finding], topic: str, level: str) -> str:
    return "\n".join(f.text for f in of(findings, topic, level))


def shown(findings: Sequence[Finding]) -> str:
    return "\n".join(f"{f.level}  {f.topic}: {f.text}" for f in findings)


@pytest.fixture
def blank(relay_paths: Paths) -> DoctorSetup:
    return DoctorSetup(relay_paths)


@pytest.fixture
def healthy(blank: DoctorSetup) -> DoctorSetup:
    blank.healthy()
    return blank


@pytest.fixture
def live_pid() -> Iterator[int]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc.pid
    proc.kill()
    proc.wait()


class TestTestedVersions:
    def test_the_tested_release_of_each_harness_is_pinned(self) -> None:
        assert TESTED_VERSIONS == {
            "claude": "2.1.325",
            "codex": "0.169.0",
            "pi": "1.5.2",
            "opencode": "1.19.5",
        }


class TestHealthySetup:
    def test_a_healthy_setup_with_relayd_running_reports_only_ok(
        self, healthy: DoctorSetup
    ) -> None:
        with running_daemon(healthy.paths, roster_text=healthy.roster()):
            findings = healthy.check()

        assert all(f.level == "ok" for f in findings), shown(findings)
        expected = {"paths", "roster", "daemon", "architect", "reviewer", "install"}
        expected |= {f"{harness} version" for harness in TESTED_VERSIONS}
        assert expected <= {f.topic for f in findings}, shown(findings)


class TestPaths:
    def test_the_socket_db_and_roster_paths_are_shown(self, healthy: DoctorSetup) -> None:
        text = text_of(healthy.check(), "paths", "ok")

        for path in (healthy.paths.socket, healthy.paths.db, healthy.paths.roster):
            assert str(path) in text

    def test_a_socket_path_of_107_bytes_is_ok(self, healthy: DoctorSetup) -> None:
        moved = healthy.with_socket_bytes(107)

        findings = moved.check()

        assert of(findings, "paths", "error") == [], shown(findings)
        assert str(moved.paths.socket) in text_of(findings, "paths", "ok")

    def test_a_socket_path_of_108_bytes_is_an_error(self, healthy: DoctorSetup) -> None:
        moved = healthy.with_socket_bytes(108)

        findings = moved.check()

        assert str(moved.paths.socket) in text_of(findings, "paths", "error"), shown(findings)

    def test_state_and_runtime_dirs_owned_by_another_uid_are_errors(
        self, healthy: DoctorSetup
    ) -> None:
        healthy.uid = os.getuid() + 1

        text = text_of(healthy.check(), "paths", "error")

        for directory in (healthy.paths.state_dir, healthy.paths.runtime_dir):
            assert f"{directory} is owned by uid {os.getuid()}" in text

    def test_a_runtime_dir_that_does_not_exist_yet_is_not_an_error(
        self, healthy: DoctorSetup
    ) -> None:
        moved = DoctorSetup(replace(healthy.paths, runtime_dir=healthy.paths.config_dir / "later"))
        moved.healthy()

        findings = moved.check()

        assert of(findings, "paths", "error") == [], shown(findings)


class TestRoster:
    def test_a_valid_roster_is_ok_with_its_agent_count(self, healthy: DoctorSetup) -> None:
        assert "4 agents" in text_of(healthy.check(), "roster", "ok")

    def test_a_missing_roster_is_an_error_naming_the_file(self, healthy: DoctorSetup) -> None:
        healthy.paths.roster.unlink()

        findings = healthy.check()

        assert str(healthy.paths.roster) in text_of(findings, "roster", "error"), shown(findings)

    def test_an_invalid_roster_is_an_error_with_the_reason(self, healthy: DoctorSetup) -> None:
        healthy.write_roster(healthy.roster().replace("version = 1", "version = 2"))

        findings = healthy.check()

        assert "unsupported roster version 2" in text_of(findings, "roster", "error")

    def test_a_roster_broken_while_relayd_runs_is_reported(self, healthy: DoctorSetup) -> None:
        with running_daemon(healthy.paths, roster_text=healthy.roster()):
            healthy.write_roster("version = 1\n[agents.Bad]\n")
            findings = healthy.check()

        assert of(findings, "roster", "error"), shown(findings)
        assert of(findings, "daemon", "ok"), shown(findings)


class TestDaemon:
    def test_a_stopped_relayd_warns_and_is_not_started(self, healthy: DoctorSetup) -> None:
        findings = healthy.check()

        assert "not running" in text_of(findings, "daemon", "warn"), shown(findings)

    def test_a_stale_socket_without_relayd_warns(self, healthy: DoctorSetup) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as dead:
            dead.bind(str(healthy.paths.socket))

        findings = healthy.check()

        assert of(findings, "daemon", "warn"), shown(findings)
        assert of(findings, "roster", "ok"), shown(findings)
        assert spawn_traces(healthy.paths) == [healthy.paths.socket]

    @pytest.mark.parametrize(
        "reply",
        [refusing_hello, failing_hello, close_connection],
        ids=["protocol-mismatch", "error", "hangup"],
    )
    def test_a_relayd_failing_hello_warns(
        self, healthy: DoctorSetup, fake_relayd: Serve, reply: Reply
    ) -> None:
        fake_relayd(reply)

        findings = healthy.check()

        assert of(findings, "daemon", "warn"), shown(findings)
        assert of(findings, "roster", "ok"), shown(findings)
        assert of(findings, "architect", "ok"), shown(findings)

    def test_relayd_on_the_same_db_and_roster_is_ok(self, healthy: DoctorSetup) -> None:
        with running_daemon(healthy.paths, roster_text=healthy.roster()):
            findings = healthy.check()

        assert of(findings, "daemon", "ok"), shown(findings)
        assert "environments differ" not in shown(findings)

    def test_relayd_on_another_db_warns_environments_differ(
        self, healthy: DoctorSetup, fake_relayd: Serve
    ) -> None:
        roster = str(healthy.paths.roster)
        fake_relayd(hello_with(db="/tmp/mzr-other.db", roster=roster))

        text = text_of(healthy.check(), "daemon", "warn")

        assert "environments differ" in text
        assert "/tmp/mzr-other.db" in text

    def test_relayd_on_another_roster_warns_environments_differ(
        self, healthy: DoctorSetup, fake_relayd: Serve
    ) -> None:
        fake_relayd(hello_with(db=str(healthy.paths.db), roster="/tmp/mzr-other.toml"))

        text = text_of(healthy.check(), "daemon", "warn")

        assert "environments differ" in text
        assert "/tmp/mzr-other.toml" in text


class TestClaudeAgents:
    def test_live_sessions_are_counted(self, healthy: DoctorSetup, dead_pid: int) -> None:
        healthy.live_session(os.getppid())
        healthy.live_session(dead_pid)

        findings = healthy.check()

        assert re.search(r"\b2 live session", text_of(findings, "architect", "ok")), shown(findings)

    def test_unreadable_registry_entries_are_not_counted(
        self, healthy: DoctorSetup, live_pid: int
    ) -> None:
        healthy.session_file("1.json", "{not json")
        no_pid = {k: v for k, v in session_entry(live_pid).items() if k != "pid"}
        healthy.session_file(f"{live_pid}.json", json.dumps(no_pid))
        healthy.live_session(os.getppid(), file_name="notes.json")

        findings = healthy.check()

        assert re.search(r"\b1 live session", text_of(findings, "architect", "ok")), shown(findings)

    def test_a_pid_beyond_any_os_pid_is_not_live(self, healthy: DoctorSetup) -> None:
        healthy.live_session(2**64)

        findings = healthy.check()

        assert re.search(r"\b1 live session", text_of(findings, "architect", "ok")), shown(findings)

    def test_a_config_dir_without_sessions_is_ok_with_none_live(self, healthy: DoctorSetup) -> None:
        (healthy.claude_dir / "sessions" / f"{os.getpid()}.json").unlink()
        (healthy.claude_dir / "sessions").rmdir()

        findings = healthy.check()

        assert re.search(r"\b0 live session", text_of(findings, "architect", "ok")), shown(findings)

    def test_a_missing_config_dir_warns_naming_it(self, blank: DoctorSetup) -> None:
        blank.write_roster()

        findings = blank.check()

        assert str(blank.claude_dir) in text_of(findings, "architect", "warn"), shown(findings)


class TestCodexAgents:
    def test_a_resolved_thread_is_ok_with_its_id(self, healthy: DoctorSetup) -> None:
        assert "t-1" in text_of(healthy.check(), "reviewer", "ok")

    def test_a_missing_thread_warns_with_the_rename_hint(self, healthy: DoctorSetup) -> None:
        write_index(healthy.codex_home, index_line("t-9", "someone-else"))

        findings = healthy.check()

        assert "/rename reviewer" in text_of(findings, "reviewer", "warn"), shown(findings)

    def test_a_missing_codex_home_warns_naming_it(self, blank: DoctorSetup) -> None:
        blank.write_roster(blank.roster(reviewer_tools="full"))
        # The thread warn names <codex_home>/session_index.jsonl; this needs the home itself.
        home_alone = re.escape(str(blank.codex_home)) + r"(?![/\w-])"

        findings = blank.check()

        assert re.search(home_alone, text_of(findings, "reviewer", "warn")), shown(findings)

    def test_duplicate_threads_warn_naming_the_one_used(self, healthy: DoctorSetup) -> None:
        write_index(
            healthy.codex_home,
            index_line("t-old", "reviewer", "2026-11-10T09:00:00Z"),
            index_line("t-new", "reviewer", "2026-11-12T09:00:00Z"),
        )

        text = text_of(healthy.check(), "reviewer", "warn")

        assert "2 Codex threads named reviewer" in text
        assert "t-new" in text

    @pytest.mark.parametrize(
        "config",
        [
            None,
            'model = "gpt-5"\n',
            'sandbox_mode = "workspace-write"\n',
            "sandbox_mode = [unclosed\n",
            '[profiles.safe]\nsandbox_mode = "read-only"\n',
            "sandbox_mode = " + "[" * DEEP + "]" * DEEP + "\n",
        ],
        ids=[
            "no-file",
            "no-key",
            "workspace-write",
            "invalid-toml",
            "only-in-a-profile",
            "deeply-nested",
        ],
    )
    def test_read_only_without_a_read_only_sandbox_warns_naming_the_config(
        self, healthy: DoctorSetup, config: str | None
    ) -> None:
        if config is None:
            healthy.codex_config.unlink()
        else:
            healthy.sandbox(config)

        findings = healthy.check()

        assert str(healthy.codex_config) in text_of(findings, "reviewer", "warn"), shown(findings)

    def test_read_only_with_a_read_only_sandbox_does_not_warn(self, healthy: DoctorSetup) -> None:
        assert of(healthy.check(), "reviewer", "warn") == []

    def test_a_profile_that_loosens_the_sandbox_warns_naming_it(self, healthy: DoctorSetup) -> None:
        profile = healthy.codex_home / "wide.config.toml"
        profile.write_text('sandbox_mode = "workspace-write"\n', encoding="utf-8")

        text = text_of(healthy.check(), "reviewer", "warn")

        assert "--profile wide" in text
        assert str(profile) in text
        assert "workspace-write" in text

    @pytest.mark.parametrize(
        "profile_text", ['sandbox_mode = "read-only"\n', 'model = "gpt-5"\n'], ids=["same", "unset"]
    )
    def test_a_profile_that_keeps_the_sandbox_does_not_warn(
        self, healthy: DoctorSetup, profile_text: str
    ) -> None:
        (healthy.codex_home / "safe.config.toml").write_text(profile_text, encoding="utf-8")

        findings = healthy.check()

        assert of(findings, "reviewer", "warn") == [], shown(findings)

    def test_a_full_tools_agent_needs_no_sandbox(self, healthy: DoctorSetup) -> None:
        healthy.write_roster(healthy.roster(reviewer_tools="full"))
        healthy.codex_config.unlink()

        findings = healthy.check()

        assert of(findings, "reviewer", "warn") == [], shown(findings)

    def test_a_codex_bin_not_on_path_warns_naming_it(self, healthy: DoctorSetup) -> None:
        healthy.write_roster(healthy.roster(codex_bin="codex-acct2"))

        findings = healthy.check()

        assert "codex-acct2" in text_of(findings, "reviewer", "warn"), shown(findings)


class TestVersions:
    @pytest.mark.parametrize("harness", sorted(TESTED_OUTPUT))
    def test_the_tested_version_is_ok(self, healthy: DoctorSetup, harness: str) -> None:
        findings = healthy.check()

        assert TESTED_VERSIONS[harness] in text_of(findings, f"{harness} version", "ok")

    @pytest.mark.parametrize("version", ["2.1.400", "2.1.300"])
    def test_another_version_warns_naming_the_tested_one(
        self, healthy: DoctorSetup, version: str
    ) -> None:
        healthy.bins.outputs["claude"] = f"{version} (Claude Code)\n"

        text = text_of(healthy.check(), "claude version", "warn")

        assert version in text
        assert f"tested {TESTED_VERSIONS['claude']}" in text

    @pytest.mark.parametrize("how", ["not-on-path", "vanished"])
    def test_an_absent_harness_gets_no_version_finding(
        self, healthy: DoctorSetup, how: str
    ) -> None:
        if how == "vanished":
            healthy.bins.vanished.add("codex")
        else:
            del healthy.bins.outputs["codex"]

        findings = healthy.check()

        assert of(findings, "codex version") == [], shown(findings)
        assert of(findings, "claude version", "ok"), shown(findings)

    def test_a_codex_bin_off_path_is_probed_too(self, healthy: DoctorSetup) -> None:
        other = "/opt/codex-acct2/bin/codex"
        healthy.bins.elsewhere[other] = "codex-cli 0.170.0\n"
        healthy.write_roster(healthy.roster(codex_bin=other))

        findings = healthy.check()

        assert [other, "--version"] in healthy.bins.runs
        assert other in text_of(findings, "codex version", "warn"), shown(findings)
        assert of(findings, "codex version", "ok"), shown(findings)

    @pytest.mark.parametrize("codex_bin", ["codex", f"{BIN_DIR}/codex"])
    def test_a_codex_bin_on_path_is_probed_once(self, healthy: DoctorSetup, codex_bin: str) -> None:
        healthy.write_roster(healthy.roster(codex_bin=codex_bin))

        findings = healthy.check()

        probes = [
            argv for argv in healthy.bins.runs if healthy.bins.which(argv[0]) == f"{BIN_DIR}/codex"
        ]
        assert len(probes) == 1, healthy.bins.runs
        assert len(of(findings, "codex version")) == 1, shown(findings)

    @pytest.mark.parametrize(
        ("output", "code"), [("Claude Code\n", 0), ("2.1.323 (Claude Code)\n", 1)]
    )
    def test_an_unreadable_version_warns(
        self, healthy: DoctorSetup, output: str, code: int
    ) -> None:
        healthy.bins.outputs["claude"] = output
        healthy.bins.exit_codes["claude"] = code

        findings = healthy.check()

        assert of(findings, "claude version", "warn"), shown(findings)
        assert of(findings, "claude version", "ok") == []


class TestInstallRecord:
    def test_an_existing_bin_is_ok(self, healthy: DoctorSetup) -> None:
        assert str(healthy.bin) in text_of(healthy.check(), "install", "ok")

    def test_a_stale_bin_warns_naming_it(self, healthy: DoctorSetup) -> None:
        healthy.bin.unlink()

        findings = healthy.check()

        assert str(healthy.bin) in text_of(findings, "install", "warn"), shown(findings)

    @pytest.mark.parametrize(
        "record",
        ["{not json", '{"version": "0.1.0"}', '["bin"]', "[" * DEEP + "]" * DEEP],
        ids=["invalid-json", "no-bin", "not-an-object", "deeply-nested"],
    )
    def test_an_unreadable_record_warns_naming_it(self, healthy: DoctorSetup, record: str) -> None:
        healthy.paths.install_record.write_text(record, encoding="utf-8")

        findings = healthy.check()

        install_warns = text_of(findings, "install", "warn")
        assert str(healthy.paths.install_record) in install_warns, shown(findings)

    def test_no_record_is_not_a_problem(self, healthy: DoctorSetup) -> None:
        healthy.paths.install_record.unlink()

        findings = healthy.check()

        assert [f for f in of(findings, "install") if f.level != "ok"] == []


@pytest.fixture
def cli_home(
    relay_home: Path,
    agent_free_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """The CLI's relay home, with a PATH that has no harness binaries on it."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return relay_home


@pytest.fixture
def long_home(cli_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An MZ_RELAY_HOME whose socket path is 110 bytes, over the 108-byte limit."""
    tail = len(os.fsencode(Path("/run/relayd.sock")))
    home = cli_home / ("h" * (110 - len(os.fsencode(cli_home)) - 1 - tail))
    home.mkdir()
    monkeypatch.setenv("MZ_RELAY_HOME", str(home))
    assert len(os.fsencode(home / "run" / "relayd.sock")) == 110
    return home


class TestCli:
    def test_a_broken_roster_prints_an_error_and_exits_1(
        self, cli_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (cli_home / "roster.toml").write_text("version = 2\n", encoding="utf-8")

        code = main(["doctor"])

        lines = capsys.readouterr().out.splitlines()
        assert code == 1
        assert any(line.startswith("error  roster: ") for line in lines), lines
        assert spawn_traces(paths_in(cli_home)) == []

    def test_warnings_only_exit_0_with_one_line_per_finding(
        self, cli_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (cli_home / "roster.toml").write_text(pull_roster(cli_home), encoding="utf-8")

        code = main(["doctor"])

        lines = capsys.readouterr().out.splitlines()
        assert code == 0
        assert lines
        assert all(CLI_LINE.fullmatch(line) for line in lines), lines
        assert any(line.startswith("warn  daemon: ") for line in lines), lines
        assert not any(line.startswith("error  ") for line in lines), lines
        assert spawn_traces(paths_in(cli_home)) == []

    def test_a_too_long_socket_path_is_an_error_line_and_exits_1(
        self, long_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        socket_path = long_home / "run" / "relayd.sock"

        code = main(["doctor"])

        lines = capsys.readouterr().out.splitlines()
        assert code == 1
        errors = [line for line in lines if line.startswith("error  paths: ")]
        assert any(str(socket_path) in line for line in errors), lines
        home = long_home
        assert spawn_traces(Paths(home / "state", home / "run", home, home)) == []

    def test_other_commands_still_refuse_a_too_long_socket_path_with_exit_2(
        self, long_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["status"]) == 2
        assert "socket path" in capsys.readouterr().err

    @pytest.mark.parametrize("source", ["thread", "install"])
    def test_control_characters_in_findings_are_escaped(
        self, cli_home: Path, capsys: pytest.CaptureFixture[str], source: str
    ) -> None:
        doctor_home = DoctorSetup(paths_in(cli_home))
        doctor_home.healthy()
        if source == "thread":
            write_index(doctor_home.codex_home, index_line("t-\x1b[2J", "reviewer"))
        else:
            record = {"bin": "/tmp/mzr-\x1b[2J-bin", "version": "0.1.0"}
            doctor_home.paths.install_record.write_text(json.dumps(record), encoding="utf-8")

        main(["doctor"])

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\ufffd[2J" in out
