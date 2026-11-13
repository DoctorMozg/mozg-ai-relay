"""The demo script: its roster, the seeded workdir, the doctor gate, the tmux windows, the kickoff
and the exit rule, driven through a fake command runner and a fake clock."""

from __future__ import annotations

import io
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mz_relay.ids import new_id
from mz_relay.roster import Harness, Label, load_roster
from relay_rig import PLUGIN, FakeClock

# The script lives outside every package, like bin/_mz_relay_run.py.
sys.path.insert(0, str(PLUGIN / "scripts"))

from demo import Demo, DemoConfig, parse_args

Completed = subprocess.CompletedProcess[str]

DEMO_ROSTER = PLUGIN / "data" / "demo_roster.toml"
SESSION = "mzr-demo"
THREAD = "mzr-demo-reviewer"
AGENTS = ("architect", "builder", "reviewer")
KICKOFF = (
    "Ask builder (mz-relay) to make test_slug.py pass in {workdir}. Builder asks reviewer for a"
    " review before replying. When builder reports done and the tests pass, send kind=done to"
    " builder."
)
WINDOW_KEYS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "MZ_RELAY_HOME", "MZ_RELAY_NAME")
LOG_TIME = "2026-11-13T10:00:00.000Z"
POLL_S = 5.0
ONLINE_WAIT_S = 120.0
# Far past any timeout a test sets, so a demo that never stops fails instead of hanging.
RUNAWAY_S = 100_000.0
MAX_CALLS = 20_000
TMUX_VALUE_FLAGS = frozenset("cefFnNstxy")
TMUX_GLOBAL_VALUE_FLAGS = frozenset({"-L", "-S", "-f", "-T"})
ENTER_KEYS = frozenset({"Enter", "C-m"})
# Approving or denying is the user's call; the demo only prints the command.
RELAY_COMMANDS = frozenset({"doctor", "status", "log"})
# No user or system git config, and no guessed identity: the demo must name its own committer.
GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "user.useConfigOnly",
    "GIT_CONFIG_VALUE_0": "true",
}
GIT_IDENTITY_PREFIXES = ("GIT_AUTHOR_", "GIT_COMMITTER_", "EMAIL")


def isolated_git_env(env: Mapping[str, str]) -> dict[str, str]:
    kept = {key: value for key, value in env.items() if not key.startswith(GIT_IDENTITY_PREFIXES)}
    return {**kept, **GIT_ISOLATION}


@dataclass(frozen=True)
class LogRow:
    """One message as `mz-relay log` prints it, visible from `at_s` seconds into the run."""

    id: str
    source: str
    to: str
    kind: str
    context: str
    status: str = "acked"
    preview: str = "a message"
    at_s: float = 0.0

    def line(self) -> str:
        route = f"{self.source} → {self.to}"
        return f"{LOG_TIME}  {self.id}  {route}  {self.kind}  {self.status}  {self.preview}"


@dataclass
class World:
    """What the faked commands report: doctor, who is online when, the log, pytest, and
    whether a mzr-demo session is already running."""

    doctor_rc: int = 0
    doctor_out: str = "ok  roster: 3 agents: architect, builder, reviewer\nwarn  daemon: down\n"
    online_at: dict[str, float | None] = field(default_factory=lambda: dict.fromkeys(AGENTS, 0.0))
    rows: list[LogRow] = field(default_factory=list)
    pytest_passes_at: float | None = 0.0
    session_exists: bool = False
    log_error: str | None = None
    interrupt_on_log: bool = False
    git_global_config: Path | None = None


@dataclass(frozen=True)
class Call:
    argv: list[str]
    env: dict[str, str]
    at_s: float


@dataclass(frozen=True)
class TmuxCommand:
    name: str
    options: dict[str, list[str]]
    args: list[str]

    def last(self, flag: str) -> str | None:
        values = self.options.get(flag)
        return values[-1] if values else None


@dataclass(frozen=True)
class Key:
    """One send-keys argument; `order` sorts keys across calls and windows."""

    order: tuple[int, int]
    at_s: float
    text: str
    literal: bool

    @property
    def is_enter(self) -> bool:
        return not self.literal and self.text in ENTER_KEYS


@dataclass(frozen=True)
class Window:
    env: dict[str, str]
    cwd: str | None
    words: list[str]

    def runs(self, program: str) -> bool:
        return any(Path(word).name == program for word in self.words)

    def plugin_dirs(self) -> list[Path]:
        found = [after for word, after in itertools.pairwise(self.words) if word == "--plugin-dir"]
        found += [word.partition("=")[2] for word in self.words if word.startswith("--plugin-dir=")]
        return [Path(raw).resolve() for raw in found]


def tmux_socket(argv: Sequence[str]) -> str | None:
    """The `-L` socket name among tmux's global options, before the first subcommand."""
    words = list(argv[1:])
    i = 0
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "-L":
            return words[i + 1]
        i += 2 if words[i] in TMUX_GLOBAL_VALUE_FLAGS else 1
    return None


def shell_commands(text: str) -> list[list[str]]:
    """Each printed command that starts at `MZ_RELAY_HOME=`, split as a shell would."""
    found = []
    for line in text.splitlines():
        at = line.find("MZ_RELAY_HOME=")
        if at < 0:
            continue
        try:
            found.append(shlex.split(line[at:]))
        except ValueError:
            continue
    return found


def relay_command(text: str, home: Path, *tail: str) -> bool:
    """True when text prints `MZ_RELAY_HOME=<home> <...>/mz-relay <tail>`, shell-quoted."""
    return any(
        words[0] == f"MZ_RELAY_HOME={home}"
        and len(words) == len(tail) + 2
        and Path(words[1]).name == "mz-relay"
        and words[2:] == list(tail)
        for words in shell_commands(text)
    )


def tmux_commands(argv: Sequence[str]) -> list[TmuxCommand]:
    """Each subcommand of one tmux call, split on `;`, with its options parsed like getopt."""
    segments: list[list[str]] = [[]]
    for word in argv[1:]:
        if word in (";", "\\;"):
            segments.append([])
        else:
            segments[-1].append(word)
    commands = []
    for words in segments:
        # Global options such as -L come before the first subcommand.
        i = 0
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in TMUX_GLOBAL_VALUE_FLAGS else 1
        if i < len(words):
            commands.append(_parse_tmux(words[i], words[i + 1 :]))
    return commands


def _parse_tmux(name: str, words: list[str]) -> TmuxCommand:
    options: dict[str, list[str]] = {}
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        word = words[i]
        i += 1
        if word == "--":
            break
        for j, flag in enumerate(word[1:], start=1):
            if flag in TMUX_VALUE_FLAGS:
                value = word[j + 1 :]
                if not value:
                    value, i = words[i], i + 1
                options.setdefault(flag, []).append(value)
                break
            options.setdefault(flag, []).append("")
    return TmuxCommand(name, options, words[i:])


def is_tmux(argv: Sequence[str]) -> bool:
    return Path(argv[0]).name == "tmux"


def is_pytest(argv: Sequence[str]) -> bool:
    return Path(argv[0]).name == "pytest" or "pytest" in argv


def _window(target: str) -> tuple[str, str]:
    session, _, window = target.lstrip("=").partition(":")
    return session, window.split(".")[0]


class GuardedClock(FakeClock):
    """FakeClock that fails a demo which keeps sleeping long past its own timeout."""

    def __init__(self) -> None:
        super().__init__()
        self.start = self.now()

    def sleep(self, s: float) -> None:
        super().sleep(s)
        if self.now() - self.start > RUNAWAY_S:
            raise AssertionError(f"the demo slept past {RUNAWAY_S:g} s without stopping")

    @property
    def elapsed(self) -> float:
        return self.now() - self.start


class FakeSystem:
    """The runner seam: answers tmux, mz-relay and pytest from a World; git runs for real,
    isolated from the user's git config."""

    def __init__(self, world: World, clock: GuardedClock, relay_home: Path) -> None:
        self.world = world
        self.clock = clock
        self.relay_home = relay_home
        self.calls: list[Call] = []
        self.roster_at_doctor: bool | None = None
        self.sessions: set[str] = {SESSION} if world.session_exists else set()

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> Completed:
        self.calls.append(Call(list(argv), dict(env), self.clock.elapsed))
        if len(self.calls) > MAX_CALLS:
            raise AssertionError(f"the demo ran more than {MAX_CALLS} commands")
        program = Path(argv[0]).name
        if program == "git":
            git_env = isolated_git_env(env)
            if self.world.git_global_config is not None:
                git_env["GIT_CONFIG_GLOBAL"] = str(self.world.git_global_config)
            return subprocess.run(
                list(argv),
                env=git_env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        if program == "tmux":
            return self._tmux(argv)
        if program == "mz-relay":
            return self._relay(argv)
        if is_pytest(argv):
            return self._pytest(argv)
        raise AssertionError(f"the demo ran an unexpected command: {list(argv)}")

    def _done(self, argv: Sequence[str], rc: int, out: str = "", err: str = "") -> Completed:
        return subprocess.CompletedProcess(list(argv), rc, out, err)

    def _tmux(self, argv: Sequence[str]) -> Completed:
        for command in tmux_commands(argv):
            target = _window(command.last("t") or "")[0]
            if command.name == "new-session":
                session = command.last("s") or ""
                if session in self.sessions:
                    return self._done(argv, 1, err=f"duplicate session: {session}\n")
                self.sessions.add(session)
            elif command.name == "has-session" and target not in self.sessions:
                return self._done(argv, 1, err=f"can't find session: {target}\n")
        return self._done(argv, 0)

    def _relay(self, argv: Sequence[str]) -> Completed:
        words = list(argv[1:])
        if words[:1] == ["doctor"]:
            self.roster_at_doctor = (self.relay_home / "roster.toml").is_file()
            return self._done(argv, self.world.doctor_rc, self.world.doctor_out)
        if words[:1] == ["status"]:
            return self._status(argv)
        if words[:1] == ["log"]:
            return self._log(argv, words[1:])
        raise AssertionError(f"the demo ran mz-relay {words}; it may only run doctor, status, log")

    def _status(self, argv: Sequence[str]) -> Completed:
        now = self.clock.elapsed
        online = {name for name, at in self.world.online_at.items() if at is not None and at <= now}
        if not online:
            return self._done(argv, 3, err="mz-relay: relayd is not running\n")
        harness = {"architect": "claude", "builder": "claude", "reviewer": "codex"}
        lines = [
            "relayd pid 4242, protocol 1, version 0.1.0, 2 connection(s)",
            f"db: {self.relay_home}/state/relay.db",
            f"roster: {self.relay_home}/roster.toml",
            "agents:",
            *(
                f"  {name}  {harness[name]}  {'online' if name in online else 'offline'}"
                for name in AGENTS
            ),
            "counts: none",
        ]
        return self._done(argv, 0, "\n".join(lines) + "\n")

    def _log(self, argv: Sequence[str], words: list[str]) -> Completed:
        if self.world.interrupt_on_log:
            raise KeyboardInterrupt
        if self.world.log_error is not None:
            return self._done(argv, 1, err=self.world.log_error)
        context = words[words.index("--context") + 1] if "--context" in words else None
        rows = [
            row
            for row in self.world.rows
            if row.at_s <= self.clock.elapsed and context in (None, row.context)
        ]
        out = "".join(row.line() + "\n" for row in rows) or "no messages\n"
        return self._done(argv, 0, out)

    def _pytest(self, argv: Sequence[str]) -> Completed:
        passes_at = self.world.pytest_passes_at
        if passes_at is not None and passes_at <= self.clock.elapsed:
            return self._done(argv, 0, "...\n3 passed in 0.01s\n")
        return self._done(argv, 1, "FFF\n3 failed in 0.01s\n")

    # Views over the recorded calls.

    def relay_calls(self, subcommand: str | None = None) -> list[Call]:
        return [
            call
            for call in self.calls
            if Path(call.argv[0]).name == "mz-relay"
            and (subcommand is None or call.argv[1:2] == [subcommand])
        ]

    def relay_subcommands(self) -> set[str]:
        return {call.argv[1] if len(call.argv) > 1 else "" for call in self.relay_calls()}

    def tmux(self) -> list[tuple[int, Call, TmuxCommand]]:
        return [
            (index, call, command)
            for index, call in enumerate(self.calls)
            if Path(call.argv[0]).name == "tmux"
            for command in tmux_commands(call.argv)
        ]

    def tmux_names(self) -> list[str]:
        return [command.name for _, _, command in self.tmux()]

    def windows(self) -> dict[str, Window]:
        found = {}
        for _, _, command in self.tmux():
            if command.name in ("new-session", "new-window"):
                pairs = (value.partition("=") for value in command.options.get("e", []))
                args = command.args
                words = shlex.split(args[0]) if len(args) == 1 else list(args)
                env = {key: value for key, _, value in pairs}
                found[command.last("n") or ""] = Window(env, command.last("c"), words)
        return found

    def keys_sent(self, window: str) -> list[Key]:
        """Every send-keys argument for one window of the demo session, in order."""
        return [
            Key((index, position), call.at_s, text, "l" in command.options)
            for index, call, command in self.tmux()
            if command.name == "send-keys" and _window(command.last("t") or "") == (SESSION, window)
            for position, text in enumerate(command.args)
        ]

    def launched(self) -> list[str]:
        started = ("new-session", "new-window", "send-keys")
        return [name for name in self.tmux_names() if name in started]


def submitted_after(keys: Sequence[Key], needle: str) -> tuple[Key, Key | None]:
    """The key carrying needle and the Enter that follows it before any other text, if any."""
    at = next((i for i, key in enumerate(keys) if needle in key.text), None)
    assert at is not None, f"{needle!r} was never typed: {[key.text for key in keys]}"
    for key in keys[at + 1 :]:
        if key.is_enter:
            return keys[at], key
        if key.text:
            break
    return keys[at], None


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str
    system: FakeSystem
    clock: GuardedClock


@dataclass(frozen=True)
class Dirs:
    architect: Path
    builder: Path
    codex: Path
    workdir: Path

    def config(self, *, hold: bool = True, timeout_s: float = 900.0) -> DemoConfig:
        return DemoConfig(
            architect_config=self.architect,
            builder_config=self.builder,
            codex_home=self.codex,
            workdir=self.workdir,
            hold=hold,
            timeout_s=timeout_s,
        )

    def argv(self, *extra: str) -> list[str]:
        return [
            "--architect-config",
            str(self.architect),
            "--builder-config",
            str(self.builder),
            "--codex-home",
            str(self.codex),
            "--workdir",
            str(self.workdir),
            *extra,
        ]

    @property
    def kickoff(self) -> str:
        return KICKOFF.format(workdir=self.workdir)


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    """Two Claude config dirs and a Codex home that exist, and a workdir that does not yet."""
    made = Dirs(tmp_path / "arch", tmp_path / "build", tmp_path / "codex", tmp_path / "work")
    for directory in (made.architect, made.builder, made.codex):
        directory.mkdir()
    return made


RunDemo = Callable[..., Run]


@pytest.fixture
def run_demo(dirs: Dirs, relay_home: Path, capsys: pytest.CaptureFixture[str]) -> RunDemo:
    def run(world: World | None = None, *, hold: bool = True, timeout_s: float = 900.0) -> Run:
        clock = GuardedClock()
        system = FakeSystem(world or finished_world(), clock, relay_home)
        config = dirs.config(hold=hold, timeout_s=timeout_s)
        try:
            code = Demo(config, relay_home=relay_home, run=system, clock=clock).run()
        except KeyboardInterrupt:
            # Left to propagate, it would stop the whole pytest session, not fail one test.
            pytest.fail("KeyboardInterrupt escaped Demo.run(); Ctrl-C must end it with exit 130")
        printed = capsys.readouterr()
        return Run(code, printed.out, printed.err, system, clock)

    return run


@dataclass(frozen=True)
class Ids:
    kickoff: str = field(default_factory=new_id)
    review: str = field(default_factory=new_id)
    verdict: str = field(default_factory=new_id)
    report: str = field(default_factory=new_id)
    done: str = field(default_factory=new_id)


def conversation(ids: Ids, *, review_status: str = "acked") -> list[LogRow]:
    """Kickoff, the builder's review round in its own context, and the builder's report."""
    return [
        LogRow(ids.kickoff, "architect", "builder", "request", ids.kickoff, preview="fix slug"),
        LogRow(
            ids.review,
            "builder",
            "reviewer",
            "request",
            ids.review,
            status=review_status,
            preview="please review slug.py",
        ),
        LogRow(ids.verdict, "reviewer", "builder", "reply", ids.review, preview="approve"),
        LogRow(ids.report, "builder", "architect", "reply", ids.kickoff, preview="tests pass"),
    ]


def finished_world(ids: Ids | None = None, *, done_at: float = 0.0) -> World:
    ids = ids or Ids()
    done = LogRow(ids.done, "architect", "builder", "done", ids.kickoff, at_s=done_at)
    return World(rows=[*conversation(ids), done])


class TestDemoRosterFile:
    """data/demo_roster.toml is a valid roster for the three demo agents."""

    @pytest.fixture
    def roster_agents(self, tmp_path: Path) -> dict[str, tuple[Harness, Label]]:
        roster = load_roster(DEMO_ROSTER, pw_home=tmp_path)
        return {name: (spec.harness, spec.label) for name, spec in roster.agents.items()}

    def test_lists_architect_and_builder_on_claude_and_reviewer_on_codex(
        self, roster_agents: dict[str, tuple[Harness, Label]]
    ) -> None:
        assert roster_agents == {
            "architect": (Harness.CLAUDE, Label.PERSONAL),
            "builder": (Harness.CLAUDE, Label.CORPORATE),
            "reviewer": (Harness.CODEX, Label.PERSONAL),
        }

    def test_reviewer_uses_the_demo_codex_thread(self, tmp_path: Path) -> None:
        roster = load_roster(DEMO_ROSTER, pw_home=tmp_path)

        assert roster.get("reviewer").codex_thread == THREAD

    def test_the_demo_routes_are_allowed_and_holding_is_on(self, tmp_path: Path) -> None:
        roster = load_roster(DEMO_ROSTER, pw_home=tmp_path)
        allowed = {(name, to) for name, spec in roster.agents.items() for to in spec.may_send_to}

        assert {
            ("architect", "builder"),
            ("builder", "architect"),
            ("builder", "reviewer"),
            ("reviewer", "builder"),
        } <= allowed
        assert roster.policy.hold_corporate_to_personal is True


class TestArguments:
    """The command line becomes a DemoConfig; a bad argument is a usage error (exit 2)."""

    def test_defaults_hold_on_and_a_900_s_timeout(self, dirs: Dirs) -> None:
        assert parse_args(dirs.argv()) == dirs.config(hold=True, timeout_s=900.0)

    def test_no_hold_and_timeout_are_taken(self, dirs: Dirs) -> None:
        config = parse_args(dirs.argv("--no-hold", "--timeout", "60"))

        assert (config.hold, config.timeout_s) == (False, 60.0)

    @pytest.mark.parametrize("flag", ["--architect-config", "--builder-config", "--codex-home"])
    def test_a_missing_dir_is_a_usage_error_naming_it(
        self, dirs: Dirs, flag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = dirs.argv()
        missing = tmp_path / "nowhere"
        argv[argv.index(flag) + 1] = str(missing)

        with pytest.raises(SystemExit) as usage:
            parse_args(argv)

        assert usage.value.code == 2
        assert str(missing) in capsys.readouterr().err

    @pytest.mark.parametrize(
        "flag", ["--architect-config", "--builder-config", "--codex-home", "--workdir"]
    )
    def test_a_missing_flag_is_a_usage_error(
        self, dirs: Dirs, flag: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = dirs.argv()
        at = argv.index(flag)
        del argv[at : at + 2]

        with pytest.raises(SystemExit) as usage:
            parse_args(argv)

        assert usage.value.code == 2
        assert flag in capsys.readouterr().err

    def test_a_workdir_with_files_is_refused_and_left_alone(
        self, dirs: Dirs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dirs.workdir.mkdir()
        (dirs.workdir / "keep.txt").write_text("mine\n", encoding="utf-8")

        with pytest.raises(SystemExit) as usage:
            parse_args(dirs.argv())

        assert usage.value.code == 2
        assert str(dirs.workdir) in capsys.readouterr().err
        assert sorted(p.name for p in dirs.workdir.iterdir()) == ["keep.txt"]

    def test_an_empty_existing_workdir_is_accepted(self, dirs: Dirs) -> None:
        dirs.workdir.mkdir()

        assert parse_args(dirs.argv()).workdir == dirs.workdir

    @pytest.mark.parametrize("timeout", ["0", "-5", "soon"])
    def test_a_timeout_that_is_not_positive_seconds_is_a_usage_error(
        self, dirs: Dirs, timeout: str
    ) -> None:
        with pytest.raises(SystemExit) as usage:
            parse_args(dirs.argv("--timeout", timeout))

        assert usage.value.code == 2


class TestWrittenRoster:
    """The demo writes its roster to <relay home>/roster.toml with the given accounts."""

    @pytest.mark.parametrize("hold", [True, False], ids=["hold", "no-hold"])
    def test_holding_follows_the_config(
        self, run_demo: RunDemo, relay_home: Path, hold: bool
    ) -> None:
        run_demo(hold=hold)

        roster = load_roster(relay_home / "roster.toml", pw_home=relay_home)
        assert roster.policy.hold_corporate_to_personal is hold

    def test_agents_point_at_the_given_config_dirs_and_codex_home(
        self, run_demo: RunDemo, relay_home: Path, dirs: Dirs
    ) -> None:
        run_demo()

        roster = load_roster(relay_home / "roster.toml", pw_home=relay_home)
        reviewer = roster.get("reviewer")
        assert roster.get("architect").claude_config_dir == dirs.architect.resolve()
        assert roster.get("builder").claude_config_dir == dirs.builder.resolve()
        assert reviewer.codex_home is not None
        assert reviewer.codex_home.resolve() == dirs.codex.resolve()
        assert reviewer.codex_thread == THREAD
        assert roster.get("builder").label is Label.CORPORATE

    def test_the_relay_home_is_announced_for_approve_and_log(
        self, run_demo: RunDemo, relay_home: Path
    ) -> None:
        assert f"MZ_RELAY_HOME={relay_home}" in run_demo().out

    def test_every_relay_command_targets_the_demo_relay_home(
        self, run_demo: RunDemo, relay_home: Path
    ) -> None:
        system = run_demo().system

        homes = {call.env.get("MZ_RELAY_HOME") for call in system.relay_calls()}
        assert homes == {str(relay_home)}


class TestSeededWorkdir:
    """The workdir becomes a git repo whose first commit holds a stub slugify and three tests
    that fail on it; the demo names its own committer, since the fake git has none."""

    @pytest.fixture
    def seeded(self, run_demo: RunDemo, dirs: Dirs) -> Path:
        run_demo()
        return dirs.workdir

    @staticmethod
    def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=isolated_git_env(os.environ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_slug_and_its_tests_are_committed(self, seeded: Path) -> None:
        tracked = self.git(seeded, "ls-tree", "--name-only", "HEAD")

        assert tracked.returncode == 0, tracked.stderr
        assert {"slug.py", "test_slug.py"} <= set(tracked.stdout.split())
        assert self.git(seeded, "status", "--porcelain").stdout == ""

    def test_slug_defines_slugify(self, seeded: Path) -> None:
        assert "def slugify" in (seeded / "slug.py").read_text(encoding="utf-8")

    def test_pytest_reports_three_failures(self, seeded: Path) -> None:
        # Neither this repo's pytest config nor PYTEST_* from the shell may reach the seeded run.
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-c",
                os.devnull,
                "--rootdir",
                str(seeded),
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                str(seeded),
            ],
            cwd=seeded,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        summary = done.stdout.strip().splitlines()[-1]
        assert re.search(r"\b3 failed\b", summary), done.stdout
        assert "passed" not in summary
        assert "error" not in summary


class TestDoctorGate:
    """doctor runs on the written roster; any error stops the demo before tmux starts."""

    def test_a_doctor_error_exits_non_zero_and_launches_nothing(self, run_demo: RunDemo) -> None:
        world = World(doctor_rc=1, doctor_out="error  roster: agents.builder.label is required\n")

        run = run_demo(world)

        assert run.code != 0
        assert run.system.launched() == []
        assert "error  roster: agents.builder.label is required" in run.out

    def test_doctor_sees_the_written_roster(self, run_demo: RunDemo) -> None:
        system = run_demo().system

        assert len(system.relay_calls("doctor")) == 1
        assert system.roster_at_doctor is True

    def test_warnings_alone_let_the_demo_go_on(self, run_demo: RunDemo) -> None:
        run = run_demo(World(doctor_out="warn  daemon: relayd is not running\n"))

        assert run.system.launched() != []


class TestTmuxWindows:
    """Session mzr-demo gets one window per agent, each with its own account and relay name."""

    def test_one_session_named_mzr_demo(self, run_demo: RunDemo) -> None:
        system = run_demo().system

        sessions = [c.last("s") for _, _, c in system.tmux() if c.name == "new-session"]
        assert sessions == [SESSION]

    def test_an_existing_mzr_demo_session_is_refused_and_left_alone(
        self, run_demo: RunDemo
    ) -> None:
        run = run_demo(World(session_exists=True))

        names = run.system.tmux_names()
        assert run.code != 0
        assert SESSION in run.out + run.err
        assert not {"new-window", "send-keys", "kill-session", "kill-server"} & set(names)

    def test_each_window_carries_its_account_relay_home_and_name(
        self, run_demo: RunDemo, dirs: Dirs, relay_home: Path
    ) -> None:
        windows = run_demo().system.windows()

        shown = {
            name: {key: windows[name].env.get(key) for key in WINDOW_KEYS}
            for name in AGENTS
            if name in windows
        }
        home = str(relay_home)
        assert shown == {
            "architect": {
                "CLAUDE_CONFIG_DIR": str(dirs.architect),
                "CODEX_HOME": None,
                "MZ_RELAY_HOME": home,
                "MZ_RELAY_NAME": "architect",
            },
            "builder": {
                "CLAUDE_CONFIG_DIR": str(dirs.builder),
                "CODEX_HOME": None,
                "MZ_RELAY_HOME": home,
                "MZ_RELAY_NAME": "builder",
            },
            "reviewer": {
                "CLAUDE_CONFIG_DIR": None,
                "CODEX_HOME": str(dirs.codex),
                "MZ_RELAY_HOME": home,
                "MZ_RELAY_NAME": "reviewer",
            },
        }

    @pytest.mark.parametrize("name", ["architect", "builder"])
    def test_claude_windows_run_claude_with_this_plugin_in_the_workdir(
        self, run_demo: RunDemo, dirs: Dirs, name: str
    ) -> None:
        window = run_demo().system.windows()[name]

        assert window.runs("claude"), window.words
        assert window.plugin_dirs() == [PLUGIN.resolve()], window.words
        assert window.cwd == str(dirs.workdir)

    def test_the_reviewer_window_runs_codex_in_the_workdir(
        self, run_demo: RunDemo, dirs: Dirs
    ) -> None:
        window = run_demo().system.windows()["reviewer"]

        assert window.runs("codex"), window.words
        assert not window.runs("claude"), window.words
        assert window.cwd == str(dirs.workdir)

    def test_the_rename_is_submitted_before_the_kickoff(
        self, run_demo: RunDemo, dirs: Dirs
    ) -> None:
        system = run_demo().system

        _, renamed = submitted_after(system.keys_sent("reviewer"), f"/rename {THREAD}")
        kickoff, _ = submitted_after(system.keys_sent("architect"), dirs.kickoff)
        assert renamed is not None, "no Enter followed /rename in the reviewer window"
        assert renamed.order < kickoff.order

    def test_the_exact_kickoff_is_typed_into_architect_and_submitted(
        self, run_demo: RunDemo, dirs: Dirs
    ) -> None:
        typed, entered = submitted_after(run_demo().system.keys_sent("architect"), dirs.kickoff)

        assert dirs.kickoff in typed.text
        assert entered is not None, "no Enter followed the kickoff in the architect window"

    @pytest.mark.parametrize(
        "online_at",
        [
            {"architect": 0.0, "builder": 10.0, "reviewer": 30.0},
            dict.fromkeys(AGENTS, 20.0),
        ],
        ids=["one-by-one", "relayd-down-until-20s"],
    )
    def test_the_kickoff_waits_until_all_three_are_online(
        self, run_demo: RunDemo, dirs: Dirs, online_at: dict[str, float | None]
    ) -> None:
        world = finished_world(done_at=60.0)
        world.online_at = online_at

        run = run_demo(world)

        typed, _ = submitted_after(run.system.keys_sent("architect"), dirs.kickoff)
        assert run.code == 0
        assert typed.at_s >= max(at for at in online_at.values() if at is not None)

    def test_an_agent_still_offline_after_120_s_fails_without_a_kickoff(
        self, run_demo: RunDemo, dirs: Dirs
    ) -> None:
        world = finished_world()
        world.online_at["reviewer"] = None

        run = run_demo(world)

        assert run.code == 1
        assert not any(dirs.kickoff in key.text for key in run.system.keys_sent("architect"))
        assert ONLINE_WAIT_S - POLL_S <= run.clock.elapsed <= ONLINE_WAIT_S + POLL_S
        assert run.system.relay_calls("log") == []


class TestOutcome:
    """Exit 0 once the kickoff context has a done and pytest passes; exit 1 at the timeout."""

    def test_done_in_the_kickoff_context_and_passing_tests_exit_0(
        self, run_demo: RunDemo, dirs: Dirs
    ) -> None:
        run = run_demo(finished_world(), timeout_s=300.0)

        pytest_runs = [call.argv for call in run.system.calls if is_pytest(call.argv)]
        assert run.code == 0
        assert run.clock.elapsed < 300.0
        assert any(str(dirs.workdir) in argv for argv in pytest_runs)

    def test_a_late_done_is_waited_for(self, run_demo: RunDemo) -> None:
        run = run_demo(finished_world(done_at=40.0), timeout_s=300.0)

        assert run.code == 0
        assert 40.0 <= run.clock.elapsed < 300.0

    def test_failing_tests_after_done_time_out_with_exit_1(self, run_demo: RunDemo) -> None:
        world = finished_world()
        world.pytest_passes_at = None

        assert run_demo(world, timeout_s=60.0).code == 1

    def test_passing_tests_without_a_done_time_out_with_exit_1(self, run_demo: RunDemo) -> None:
        assert run_demo(World(rows=conversation(Ids())), timeout_s=60.0).code == 1

    def test_a_done_in_another_context_does_not_count(self, run_demo: RunDemo) -> None:
        ids = Ids()
        elsewhere = LogRow(ids.done, "builder", "reviewer", "done", ids.review)

        run = run_demo(World(rows=[*conversation(ids), elsewhere]), timeout_s=60.0)

        assert run.code == 1

    def test_the_timeout_ends_the_run_and_prints_the_log(self, run_demo: RunDemo) -> None:
        ids = Ids()

        run = run_demo(World(rows=conversation(ids)), timeout_s=60.0)

        assert run.code == 1
        assert 60.0 - POLL_S <= run.clock.elapsed <= 60.0 + POLL_S
        assert ids.review in run.out
        assert "please review slug.py" in run.out

    def test_the_log_is_polled_every_5_s(self, run_demo: RunDemo) -> None:
        run = run_demo(World(rows=conversation(Ids())), timeout_s=60.0)

        polled = sorted({call.at_s for call in run.system.relay_calls("log")})
        gaps = [later - earlier for earlier, later in itertools.pairwise(polled)]
        assert len(polled) >= 10
        assert gaps == pytest.approx([POLL_S] * len(gaps))


class TestHeldMessages:
    """Held messages are shown with their approve command; deciding stays with the user."""

    def test_a_held_message_is_printed_once_with_its_approve_command(
        self, run_demo: RunDemo
    ) -> None:
        ids = Ids()

        run = run_demo(World(rows=conversation(ids, review_status="held")), timeout_s=60.0)

        assert run.out.count(f"mz-relay approve {ids.review}") == 1
        assert f"mz-relay approve {ids.kickoff}" not in run.out

    @pytest.mark.parametrize("held", [True, False], ids=["held-review", "nothing-held"])
    def test_the_demo_never_approves_or_denies_on_its_own(
        self, run_demo: RunDemo, held: bool
    ) -> None:
        ids = Ids()
        rows = conversation(ids, review_status="held" if held else "acked")
        done = LogRow(ids.done, "architect", "builder", "done", ids.kickoff, at_s=30.0)

        run = run_demo(World(rows=[*rows, done]), timeout_s=60.0)

        assert run.system.relay_subcommands() <= RELAY_COMMANDS


class TestTmuxSocket:
    """Every tmux call goes to the demo's own server, so no user server's env reaches a window."""

    def test_every_tmux_call_uses_the_mzr_demo_socket(self, run_demo: RunDemo) -> None:
        system = run_demo().system

        sockets = {tmux_socket(call.argv) for call in system.calls if is_tmux(call.argv)}
        assert sockets == {SESSION}

    def test_the_session_check_uses_the_socket_too(self, run_demo: RunDemo) -> None:
        run = run_demo(World(session_exists=True))

        sockets = {tmux_socket(call.argv) for call in run.system.calls if is_tmux(call.argv)}
        assert run.code != 0
        assert sockets == {SESSION}

    def test_the_attach_hint_names_the_socket(self, run_demo: RunDemo) -> None:
        assert f"tmux -L {SESSION} attach" in run_demo().out

    def test_a_hint_to_accept_the_folder_trust_prompts_follows_the_start(
        self, run_demo: RunDemo
    ) -> None:
        assert "trust" in run_demo().out.lower()


class TestWorkdirWithHash:
    """tmux expands `#` formats in -c, so the demo passes the workdir with `#` doubled."""

    @pytest.fixture
    def dirs(self, tmp_path: Path) -> Dirs:
        made = Dirs(tmp_path / "arch", tmp_path / "build", tmp_path / "codex", tmp_path / "w#1")
        for directory in (made.architect, made.builder, made.codex):
            directory.mkdir()
        return made

    def test_each_window_gets_the_escaped_workdir(self, run_demo: RunDemo, dirs: Dirs) -> None:
        windows = run_demo().system.windows()

        cwds = {name: windows[name].cwd for name in AGENTS if name in windows}
        escaped = str(dirs.workdir).replace("#", "##")
        assert cwds == dict.fromkeys(AGENTS, escaped)

    def test_the_seed_and_the_kickoff_use_the_real_workdir(
        self, run_demo: RunDemo, dirs: Dirs
    ) -> None:
        run = run_demo()

        typed, _ = submitted_after(run.system.keys_sent("architect"), dirs.kickoff)
        assert run.code == 0
        assert (dirs.workdir / "slug.py").is_file()
        assert "##" not in typed.text


class TestRosterEncoding:
    """Paths reach the roster through install.toml_string; one that cannot be written fails
    the demo with a message, not a traceback."""

    @pytest.fixture
    def dirs(self, tmp_path: Path, request: pytest.FixtureRequest) -> Dirs:
        made = Dirs(
            tmp_path / f"arch{request.param}",
            tmp_path / "build",
            tmp_path / "codex",
            tmp_path / "w",
        )
        for directory in (made.architect, made.builder, made.codex):
            directory.mkdir()
        return made

    @pytest.mark.parametrize("dirs", ["🚀", ' "q" \\ x'], indirect=True, ids=["rocket", "quotes"])
    def test_a_config_dir_name_round_trips_through_load_roster(
        self, run_demo: RunDemo, dirs: Dirs, relay_home: Path
    ) -> None:
        run = run_demo()

        assert run.code == 0, run.err
        roster = load_roster(relay_home / "roster.toml", pw_home=relay_home)
        assert roster.get("architect").claude_config_dir == dirs.architect.resolve()

    # A name read from bytes that are not UTF-8 holds a lone surrogate, which no file can store.
    @pytest.mark.parametrize("dirs", ["\udcff"], indirect=True, ids=["undecodable"])
    def test_an_unencodable_config_dir_fails_with_exit_1_and_a_message(
        self, run_demo: RunDemo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Python's own stream settings: stdout is strict, stderr escapes what it cannot encode.
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="backslashreplace")
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        run = run_demo()

        printed = {name: _drain(stream) for name, stream in (("out", out), ("err", err))}
        assert run.code == 1
        assert run.system.launched() == []
        assert "demo:" in printed["err"]
        assert "Traceback" not in printed["out"] + printed["err"]


def _drain(stream: io.TextIOWrapper) -> str:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue().decode("utf-8")


class TestSeedCommitHooks:
    """The seed commit runs no hooks of the user's, global or local."""

    @staticmethod
    def commit_argv(system: FakeSystem) -> list[str]:
        commits = [c.argv for c in system.calls if c.argv[0] == "git" and "commit" in c.argv]
        assert len(commits) == 1, commits
        return commits[0]

    def test_the_commit_skips_hooks(self, run_demo: RunDemo) -> None:
        argv = self.commit_argv(run_demo().system)

        assert "--no-verify" in argv
        assert ("-c", "core.hooksPath=/dev/null") in set(itertools.pairwise(argv))

    def test_a_failing_global_hook_does_not_stop_the_seed(
        self, run_demo: RunDemo, dirs: Dirs, tmp_path: Path
    ) -> None:
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        # --no-verify skips pre-commit but not prepare-commit-msg, so both rules are needed.
        for hook in ("pre-commit", "prepare-commit-msg", "commit-msg"):
            (hooks / hook).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (hooks / hook).chmod(0o755)
        config = tmp_path / "gitconfig"
        config.write_text(f"[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
        world = finished_world()
        world.git_global_config = config

        run = run_demo(world)

        assert run.code == 0, run.err
        assert (dirs.workdir / ".git").is_dir()


class TestCleanupHint:
    """Every way out prints how to close the windows and relayd, and that the thread stays."""

    @staticmethod
    def interrupted() -> World:
        world = World(rows=conversation(Ids()))
        world.interrupt_on_log = True
        return world

    @staticmethod
    def offline() -> World:
        world = finished_world()
        world.online_at["reviewer"] = None
        return world

    @pytest.fixture(
        params=["success", "timeout", "doctor-error", "offline", "interrupt"],
    )
    def ended(self, request: pytest.FixtureRequest, run_demo: RunDemo) -> Run:
        worlds = {
            "success": finished_world,
            "timeout": lambda: World(rows=conversation(Ids())),
            "doctor-error": lambda: World(doctor_rc=1, doctor_out="error  roster: broken\n"),
            "offline": self.offline,
            "interrupt": self.interrupted,
        }
        return run_demo(worlds[request.param](), timeout_s=60.0)

    def test_the_kill_session_command_is_printed(self, ended: Run) -> None:
        assert f"tmux -L {SESSION} kill-session -t {SESSION}" in ended.out + ended.err

    def test_the_relay_stop_command_is_printed(self, ended: Run, relay_home: Path) -> None:
        assert relay_command(ended.out + ended.err, relay_home, "stop"), ended.out

    def test_the_codex_thread_is_said_to_stay(self, ended: Run) -> None:
        lines = (ended.out + ended.err).splitlines()

        assert any(THREAD in line and "CODEX_HOME" in line for line in lines), ended.out

    def test_ctrl_c_exits_130_without_a_traceback(self, run_demo: RunDemo) -> None:
        run = run_demo(self.interrupted(), timeout_s=60.0)

        assert run.code == 130
        assert "Traceback" not in run.out + run.err


class TestCommandQuoting:
    """The printed approve and stop commands survive a relay home with a space in it."""

    @pytest.fixture
    def relay_home(self) -> Iterator[Path]:
        home = Path(tempfile.mkdtemp(prefix="mzr demo-", dir="/tmp"))
        yield home
        shutil.rmtree(home, ignore_errors=True)

    def test_the_approve_command_splits_back_into_home_bin_and_id(
        self, run_demo: RunDemo, relay_home: Path
    ) -> None:
        ids = Ids()

        run = run_demo(World(rows=conversation(ids, review_status="held")), timeout_s=60.0)

        assert relay_command(run.out, relay_home, "approve", ids.review), run.out

    def test_the_stop_command_splits_back_into_home_and_bin(
        self, run_demo: RunDemo, relay_home: Path
    ) -> None:
        run = run_demo()

        assert relay_command(run.out + run.err, relay_home, "stop"), run.out


class TestFailingLog:
    """A log that cannot be read is reported once and shown as unavailable at the timeout."""

    ERROR = "mz-relay: protocol_mismatch: relayd speaks protocol 9\n"

    def test_the_timeout_says_log_unavailable(self, run_demo: RunDemo) -> None:
        run = run_demo(World(log_error=self.ERROR), timeout_s=60.0)

        printed = run.out + run.err
        assert run.code == 1
        assert "log unavailable" in printed
        assert "no messages" not in printed

    def test_a_repeated_failure_is_printed_once(self, run_demo: RunDemo) -> None:
        run = run_demo(World(log_error=self.ERROR), timeout_s=60.0)

        # Once as it happens; the final "log unavailable" line may repeat it.
        assert (run.out + run.err).count("protocol 9") <= 2
