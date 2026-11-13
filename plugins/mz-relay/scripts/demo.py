"""Runs the mz-relay demo: two Claude accounts and a Codex thread fix a failing test.

    python3.12 plugins/mz-relay/scripts/demo.py --architect-config DIR --builder-config DIR \\
        --codex-home DIR --workdir DIR [--no-hold] [--timeout 900]

The agents run in tmux session mzr-demo, on tmux server `-L mzr-demo`, against a fresh relay
home in /tmp. Held messages are
printed with their approve command; deciding them stays with the user.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
# Run as a plain script, so the plugin's own sources go on the path, as in bin/_mz_relay_run.py.
sys.path.insert(0, str(PLUGIN / "src"))

from mz_relay.clock import Clock, SystemClock  # noqa: E402
from mz_relay.errors import RelayError  # noqa: E402
from mz_relay.install import toml_string  # noqa: E402
from mz_relay.paths import Paths, user_home  # noqa: E402
from mz_relay.procs import Runner, run_in_group  # noqa: E402
from mz_relay.roster import load_roster  # noqa: E402

log = logging.getLogger("mz_relay.demo")

BIN = PLUGIN / "bin" / "mz-relay"
ROSTER_TEMPLATE = PLUGIN / "data" / "demo_roster.toml"
SESSION = "mzr-demo"
# A server of its own: windows on a running server would inherit that server's environment.
TMUX_SOCKET = "mzr-demo"
THREAD = "mzr-demo-reviewer"
AGENTS = ("architect", "builder", "reviewer")
KICKOFF = (
    "Ask builder (mz-relay) to make test_slug.py pass in {workdir}. Builder asks reviewer for a"
    " review before replying. When builder reports done and the tests pass, send kind=done to"
    " builder."
)
DEFAULT_TIMEOUT_S = 900.0
ONLINE_WAIT_S = 120.0
POLL_S = 5.0
# A TUI may still be taking in typed text or completing a slash command when Enter arrives.
SUBMIT_PAUSE_S = 1.0
COMMAND_TIMEOUT_S = 60.0
PYTEST_TIMEOUT_S = 120.0
LOG_LIMIT = "200"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 130
# The exit code of `mz-relay status` while relayd is not running yet.
EXIT_NOT_RUNNING = 3
# A demo started from inside an agent session must not hand that session's identity on.
AGENT_ENV_PREFIXES = (
    "CLAUDE",
    "CODEX",
    "OPENCODE",
    "PI_CODING_AGENT",
    "MZ_RELAY_HOME",
    "MZ_RELAY_NAME",
)
# The user may have no git identity; their hooks or signing setup must not block the seed.
SEED_COMMIT = (
    "-c",
    "user.name=mz-relay demo",
    "-c",
    "user.email=demo@localhost",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
    "commit",
    "--no-verify",
    "-q",
    "-m",
    "Seed",
)
STATUS_RE = re.compile(r"^  (\S+)  \S+  (online|offline)$", re.MULTILINE)
LOG_ROW_RE = re.compile(r"^\S+  (\S+)  (\S+) → (\S+)  (\S+)  (\S+)")

SLUG_PY = '''"""Turns a title into a URL slug."""


def slugify(text: str) -> str:
    """Lowercase words joined by single hyphens, with punctuation dropped."""
    raise NotImplementedError("slugify")
'''

TEST_SLUG_PY = """from slug import slugify


def test_lowercases_and_joins_words() -> None:
    assert slugify("Hello World") == "hello-world"


def test_drops_punctuation() -> None:
    assert slugify("Hi, there!") == "hi-there"


def test_collapses_runs_of_separators() -> None:
    assert slugify("  a -- b  ") == "a-b"
"""


class DemoError(Exception):
    """A step failed in a way the user has to fix before running the demo again."""


@dataclass(frozen=True)
class DemoConfig:
    architect_config: Path
    builder_config: Path
    codex_home: Path
    workdir: Path
    hold: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class LogRow:
    """One line of `mz-relay log`; the preview is not kept."""

    msg_id: str
    source: str
    to: str
    kind: str
    status: str

    @classmethod
    def parse(cls, line: str) -> LogRow | None:
        found = LOG_ROW_RE.match(line)
        return None if found is None else cls(*found.groups())


@dataclass(frozen=True)
class AgentWindow:
    name: str
    env: Mapping[str, str]
    argv: Sequence[str]


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise DemoError(f"{ROSTER_TEMPLATE} holds a value the demo cannot write: {value!r}")


def _toml_table(header: str, table: Mapping[str, object]) -> list[str]:
    return [f"[{header}]", *(f"{key} = {_toml_value(value)}" for key, value in table.items())]


class Demo:
    """Writes the roster, seeds the workdir, starts the agents and watches the conversation."""

    def __init__(self, config: DemoConfig, *, relay_home: Path, run: Runner, clock: Clock) -> None:
        if not relay_home.is_absolute():
            raise ValueError(f"relay_home must be absolute, got {relay_home}")
        if not math.isfinite(config.timeout_s) or config.timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive seconds, got {config.timeout_s}")
        self._config = config
        self._home = relay_home
        self._run = run
        self._clock = clock
        kept = {k: v for k, v in os.environ.items() if not k.startswith(AGENT_ENV_PREFIXES)}
        self._env = {**kept, "MZ_RELAY_HOME": str(relay_home)}
        self._held_shown: set[str] = set()
        self._last_pytest_summary = ""
        self._last_log_error: str | None = None

    def run(self) -> int:
        print(f"MZ_RELAY_HOME={self._home}", flush=True)
        try:
            self._write_roster()
            self._seed_workdir()
            self._check_doctor()
            self._start_windows()
            self._wait_online()
            self._type("reviewer", f"/rename {THREAD}")
            self._type("architect", KICKOFF.format(workdir=self._config.workdir))
            print("kickoff sent to architect", flush=True)
            return self._watch()
        except DemoError as exc:
            print(f"demo: {exc}", file=sys.stderr, flush=True)
            return EXIT_FAILED
        except KeyboardInterrupt:
            print("\ndemo: interrupted", file=sys.stderr, flush=True)
            return EXIT_INTERRUPTED
        finally:
            self._print_cleanup()

    def _relay_command(self, *args: str) -> str:
        """A shell-ready mz-relay command line against the demo's relay home."""
        words = shlex.join([str(BIN), *args])
        return f"MZ_RELAY_HOME={shlex.quote(str(self._home))} {words}"

    def _print_cleanup(self) -> None:
        print(
            "to clean up when you are finished:\n"
            f"  tmux -L {TMUX_SOCKET} kill-session -t {SESSION}\n"
            f"  {self._relay_command('stop')}\n"
            f"Codex thread {THREAD} stays in your CODEX_HOME; delete it there if you no longer"
            " need it",
            flush=True,
        )

    def _call(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(argv, self._env if env is None else env, timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise DemoError(f"{' '.join(argv[:2])} took longer than {timeout_s:g} s") from exc
        except OSError as exc:
            raise DemoError(f"cannot run {argv[0]}: {exc.strerror or exc}") from exc

    def _relay(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._call([str(BIN), *args])

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._call(["tmux", "-L", TMUX_SOCKET, *args])

    def _git(self, *args: str) -> None:
        done = self._call(["git", "-C", str(self._config.workdir), *args])
        if done.returncode != 0:
            failed = " ".join(args)
            raise DemoError(f"git {failed} failed in {self._config.workdir}: {done.stderr.strip()}")

    def _write_roster(self) -> None:
        try:
            paths = Paths.from_env(self._env, uid=os.getuid(), pw_home=user_home())
            doc = tomllib.loads(ROSTER_TEMPLATE.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise DemoError(f"cannot read {ROSTER_TEMPLATE}: {exc}") from exc
        except RelayError as exc:
            raise DemoError(f"relay home {self._home} is not usable: {exc}") from exc
        config = self._config
        doc["policy"]["hold_corporate_to_personal"] = config.hold
        agents = doc["agents"]
        agents["architect"]["claude_config_dir"] = str(config.architect_config)
        agents["builder"]["claude_config_dir"] = str(config.builder_config)
        agents["reviewer"]["codex_home"] = str(config.codex_home)
        lines = [
            f"version = {_toml_value(doc['version'])}",
            "",
            *_toml_table("policy", doc["policy"]),
        ]
        for name, table in agents.items():
            lines += ["", *_toml_table(f"agents.{name}", table)]
        try:
            self._home.mkdir(parents=True, exist_ok=True)
            paths.roster.write_text("\n".join(lines) + "\n", encoding="utf-8")
            load_roster(paths.roster, pw_home=paths.pw_home)
        except OSError as exc:
            raise DemoError(f"cannot write {paths.roster}: {exc.strerror or exc}") from exc
        except UnicodeError as exc:
            raise DemoError(
                f"a path given to the demo cannot be stored in the roster: {exc}"
            ) from exc
        except RelayError as exc:
            raise DemoError(f"the written roster is invalid: {exc}") from exc
        hold = "on" if config.hold else "off"
        print(f"roster written to {paths.roster} (holding {hold})", flush=True)

    def _seed_workdir(self) -> None:
        workdir = self._config.workdir
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "slug.py").write_text(SLUG_PY, encoding="utf-8")
            (workdir / "test_slug.py").write_text(TEST_SLUG_PY, encoding="utf-8")
        except OSError as exc:
            raise DemoError(f"cannot seed {workdir}: {exc.strerror or exc}") from exc
        self._git("init", "-q")
        self._git("add", "slug.py", "test_slug.py")
        self._git(*SEED_COMMIT)
        print(f"seeded {workdir} with slug.py and three failing tests", flush=True)

    def _check_doctor(self) -> None:
        done = self._relay("doctor")
        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr)
        sys.stdout.flush()
        if done.returncode != 0:
            raise DemoError(f"doctor found errors (exit {done.returncode}); fix them and rerun")

    def _windows(self) -> list[AgentWindow]:
        config, home = self._config, str(self._home)
        claude = ["claude", "--plugin-dir", str(PLUGIN)]
        return [
            AgentWindow(
                "architect",
                {"CLAUDE_CONFIG_DIR": str(config.architect_config), "MZ_RELAY_HOME": home},
                claude,
            ),
            AgentWindow(
                "builder",
                {"CLAUDE_CONFIG_DIR": str(config.builder_config), "MZ_RELAY_HOME": home},
                claude,
            ),
            AgentWindow(
                "reviewer", {"CODEX_HOME": str(config.codex_home), "MZ_RELAY_HOME": home}, ["codex"]
            ),
        ]

    def _start_windows(self) -> None:
        if self._tmux("has-session", "-t", f"={SESSION}").returncode == 0:
            raise DemoError(
                f"tmux session {SESSION} already exists; close it"
                f" (tmux -L {TMUX_SOCKET} kill-session -t {SESSION}) and run the demo again"
            )
        for index, window in enumerate(self._windows()):
            env = {**window.env, "MZ_RELAY_NAME": window.name}
            env_flags = [flag for key, value in env.items() for flag in ("-e", f"{key}={value}")]
            where = ["-d", "-s", SESSION] if index == 0 else ["-d", "-t", f"{SESSION}:"]
            command = "new-session" if index == 0 else "new-window"
            # tmux expands #{...} formats in -c; ## is a literal #.
            cwd = ["-c", str(self._config.workdir).replace("#", "##")]
            done = self._tmux(command, *where, "-n", window.name, *cwd, *env_flags, *window.argv)
            if done.returncode != 0:
                raise DemoError(
                    f"tmux {command} for {window.name} in session {SESSION} failed:"
                    f" {done.stderr.strip()}"
                )
            log.debug("started window %s", window.name)
        print(f"agents started; watch them with: tmux -L {TMUX_SOCKET} attach", flush=True)
        print(
            f"if Claude or Codex asks whether to trust {self._config.workdir}, accept it in that"
            " window, or the agent never comes online",
            flush=True,
        )

    def _online(self) -> set[str]:
        done = self._relay("status")
        if done.returncode == EXIT_NOT_RUNNING:
            return set()
        if done.returncode != 0:
            raise DemoError(f"mz-relay status failed: {done.stderr.strip()}")
        return {name for name, state in STATUS_RE.findall(done.stdout) if state == "online"}

    def _wait_online(self) -> None:
        print(f"waiting up to {ONLINE_WAIT_S:g} s for {', '.join(AGENTS)}", flush=True)
        start = self._clock.now()
        while True:
            missing = sorted(set(AGENTS) - self._online())
            if not missing:
                return
            if self._clock.now() - start >= ONLINE_WAIT_S:
                raise DemoError(
                    f"still offline after {ONLINE_WAIT_S:g} s: {', '.join(missing)};"
                    f" see their windows with tmux -L {TMUX_SOCKET} attach"
                )
            self._clock.sleep(POLL_S)

    def _type(self, window: str, text: str) -> None:
        target = f"{SESSION}:{window}"
        for keys in (["-l", "--", text], ["Enter"]):
            done = self._tmux("send-keys", "-t", target, *keys)
            if done.returncode != 0:
                raise DemoError(f"tmux send-keys to {target} failed: {done.stderr.strip()}")
            if keys != ["Enter"]:
                self._clock.sleep(SUBMIT_PAUSE_S)

    def _log(self, *args: str) -> tuple[list[LogRow], str | None]:
        """The parsed rows and the printed log, or no rows and None when log failed."""
        done = self._relay("log", *args, "--limit", LOG_LIMIT)
        if done.returncode != 0:
            error = done.stderr.strip() or f"exit {done.returncode}"
            # Polled every few seconds, so a lasting failure is reported once.
            if error != self._last_log_error:
                print(f"demo: mz-relay log failed: {error}", file=sys.stderr, flush=True)
            self._last_log_error = error
            return [], None
        self._last_log_error = None
        rows = [row for line in done.stdout.splitlines() if (row := LogRow.parse(line))]
        return rows, done.stdout

    def _show_held(self, rows: Sequence[LogRow]) -> None:
        for row in rows:
            if row.status != "held" or row.msg_id in self._held_shown:
                continue
            self._held_shown.add(row.msg_id)
            print(
                f"held: {row.msg_id} {row.source} → {row.to} ({row.kind}); to deliver it, run in"
                f" your terminal: {self._relay_command('approve', row.msg_id)}",
                flush=True,
            )

    def _tests_pass(self) -> bool:
        workdir = str(self._config.workdir)
        # Neither a pytest config around the workdir nor PYTEST_* may change the verdict.
        env = {key: value for key, value in self._env.items() if not key.startswith("PYTEST_")}
        argv = [sys.executable, "-m", "pytest", "-q", "-c", os.devnull, "--rootdir", workdir]
        argv += ["-o", "addopts=", "-p", "no:cacheprovider", workdir]
        done = self._call(argv, env=env, timeout_s=PYTEST_TIMEOUT_S)
        if "No module named pytest" in done.stderr:
            raise DemoError(f"{sys.executable} has no pytest; run the demo with one that has it")
        lines = done.stdout.strip().splitlines()
        summary = lines[-1] if lines else f"pytest exit {done.returncode}"
        if done.returncode != 0 and summary != self._last_pytest_summary:
            print(f"done arrived, but the tests do not pass yet: {summary}", flush=True)
        self._last_pytest_summary = summary
        return done.returncode == 0

    def _watch(self) -> int:
        start = self._clock.now()
        kickoff: str | None = None
        while True:
            rows, printed = self._log()
            self._show_held(rows)
            if kickoff is None:
                kickoff = next(
                    (
                        row.msg_id
                        for row in rows
                        if (row.source, row.to, row.kind) == ("architect", "builder", "request")
                    ),
                    None,
                )
                if kickoff is not None:
                    print(f"kickoff context: {kickoff}", flush=True)
            if kickoff is not None:
                closed = any(row.kind == "done" for row in self._log("--context", kickoff)[0])
                if closed and self._tests_pass():
                    print(f"context {kickoff} is done and the tests pass", flush=True)
                    return EXIT_OK
            if self._clock.now() - start >= self._config.timeout_s:
                print(f"timed out after {self._config.timeout_s:g} s; the relay log:", flush=True)
                sys.stdout.write("log unavailable\n" if printed is None else printed)
                return EXIT_FAILED
            self._clock.sleep(POLL_S)


def _existing_dir(raw: str) -> Path:
    path = Path(raw).expanduser().absolute()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"{path} is not a directory")
    return path


def _fresh_workdir(raw: str) -> Path:
    path = Path(raw).expanduser().absolute()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise argparse.ArgumentTypeError(f"{path} must be missing or an empty directory")
    return path


def _seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected seconds, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive seconds, got {raw!r}")
    return value


def parse_args(argv: Sequence[str]) -> DemoConfig:
    parser = argparse.ArgumentParser(
        prog="demo.py", description="Two Claude accounts and a Codex thread fix a failing test."
    )
    parser.add_argument(
        "--architect-config",
        type=_existing_dir,
        required=True,
        metavar="DIR",
        help="CLAUDE_CONFIG_DIR of the architect (personal) account",
    )
    parser.add_argument(
        "--builder-config",
        type=_existing_dir,
        required=True,
        metavar="DIR",
        help="CLAUDE_CONFIG_DIR of the builder (corporate) account",
    )
    parser.add_argument(
        "--codex-home",
        type=_existing_dir,
        required=True,
        metavar="DIR",
        help="CODEX_HOME of the reviewer",
    )
    parser.add_argument(
        "--workdir",
        type=_fresh_workdir,
        required=True,
        metavar="DIR",
        help="a missing or empty directory to seed with the failing tests",
    )
    parser.add_argument(
        "--no-hold",
        action="store_true",
        help="deliver corporate-to-personal messages without holding them",
    )
    parser.add_argument(
        "--timeout",
        type=_seconds,
        default=DEFAULT_TIMEOUT_S,
        metavar="S",
        help=f"seconds to wait for done after the kickoff (default {DEFAULT_TIMEOUT_S:g})",
    )
    args = parser.parse_args(list(argv))
    return DemoConfig(
        architect_config=args.architect_config,
        builder_config=args.builder_config,
        codex_home=args.codex_home,
        workdir=args.workdir,
        hold=not args.no_hold,
        timeout_s=args.timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="demo: %(message)s")
    config = parse_args(sys.argv[1:] if argv is None else argv)
    # Under /tmp, so the relay socket path stays within the Unix limit; kept for approve and log.
    relay_home = Path(tempfile.mkdtemp(prefix="mzr-demo-", dir="/tmp"))
    return Demo(config, relay_home=relay_home, run=run_in_group, clock=SystemClock()).run()


if __name__ == "__main__":
    sys.exit(main())
