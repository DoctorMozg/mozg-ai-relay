"""Live demo: two Claude accounts and a Codex thread fix a failing test over mz-relay.
Run by hand: MZ_RELAY_LIVE_DEMO="<architect cfg>:<builder cfg>:<codex home>" uv run make live-demo
Each config dir needs a Claude login and the Codex home the mz-relay snippet and a login."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from live_rig import delete_codex_thread
from mz_relay.adapters.codex_threads import CodexThreadMissing, resolve_thread
from mz_relay.envelope import Envelope, Kind
from mz_relay.store import Store
from relay_rig import PLUGIN, paths_in, stop_daemon

DEMO = PLUGIN / "scripts" / "demo.py"
SESSION = "mzr-demo"
# The demo runs its own tmux server, so the user's server env never reaches a window.
SOCKET = "mzr-demo"
THREAD = "mzr-demo-reviewer"
DEMO_TIMEOUT_S = 900
# The demo may spend up to 120 s waiting for the agents before its own timeout starts.
RUN_TIMEOUT_S = DEMO_TIMEOUT_S + 300
TMUX_TIMEOUT_S = 30.0
MAX_MESSAGES = 20
MAX_HOP = 4
FOREIGN_PREFIXES = ("CLAUDE", "CODEX", "OPENCODE", "PI_CODING_AGENT", "MZ_RELAY")
RELAY_HOME_RE = re.compile(r"MZ_RELAY_HOME=(/\S+)")


@dataclass(frozen=True)
class DemoAccounts:
    architect: Path
    builder: Path
    codex_home: Path


@dataclass(frozen=True)
class DemoRun:
    """The exit code (None after a timeout), all output, and the relay home it announced."""

    code: int | None
    out: str
    relay_home: Path | None


@pytest.fixture
def accounts() -> DemoAccounts:
    """The two Claude config dirs and the Codex home from MZ_RELAY_LIVE_DEMO."""
    raw = os.environ.get("MZ_RELAY_LIVE_DEMO", "")
    if not raw:
        pytest.skip("MZ_RELAY_LIVE_DEMO is not set")
    parts = raw.split(":")
    if len(parts) != 3 or not all(parts):
        pytest.fail(
            f"MZ_RELAY_LIVE_DEMO must be <architect cfg>:<builder cfg>:<codex home>, got {raw!r}"
        )
    dirs = [Path(part).expanduser() for part in parts]
    for directory in dirs:
        if not directory.is_dir():
            pytest.fail(f"{directory} from MZ_RELAY_LIVE_DEMO is not a directory")
    if dirs[0].resolve() == dirs[1].resolve():
        pytest.fail("the architect and builder must use two different Claude config dirs")
    return DemoAccounts(*dirs)


def _guarded(what: str, step: Callable[[], object]) -> None:
    """Runs one cleanup step; a failure only warns, so it neither skips the next step nor
    hides the test's own failure."""
    try:
        step()
    except Exception as exc:
        warnings.warn(f"cleanup: {what} failed: {exc}", stacklevel=2)


def _delete_thread(codex: str, codex_home: Path) -> None:
    try:
        thread_id = resolve_thread(codex_home, THREAD).id
    except CodexThreadMissing:
        return
    delete_codex_thread(codex, thread_id, {**os.environ, "CODEX_HOME": str(codex_home)})


def _relay_home(out: str) -> Path | None:
    found = RELAY_HOME_RE.search(out)
    return None if found is None else Path(found[1])


@pytest.fixture
def demo_run(
    accounts: DemoAccounts, tmux_bin: str, codex_bin: str, tmp_path: Path
) -> Iterator[DemoRun]:
    """Runs the demo with holding off; afterwards closes its windows, relayd and thread."""
    has = subprocess.run(
        [tmux_bin, "-L", SOCKET, "has-session", "-t", f"={SESSION}"],
        capture_output=True,
        timeout=TMUX_TIMEOUT_S,
        check=False,
    )
    if has.returncode == 0:
        pytest.fail(f"a tmux session {SESSION} is already running; close it first")
    env = {key: value for key, value in os.environ.items() if not key.startswith(FOREIGN_PREFIXES)}
    argv = [
        sys.executable,
        str(DEMO),
        "--architect-config",
        str(accounts.architect),
        "--builder-config",
        str(accounts.builder),
        "--codex-home",
        str(accounts.codex_home),
        "--workdir",
        str(tmp_path / "work"),
        "--no-hold",
        "--timeout",
        str(DEMO_TIMEOUT_S),
    ]
    # Output goes to a file, so the relay home is known even when the run times out.
    output = tmp_path / "demo.out"
    try:
        with output.open("w", encoding="utf-8") as sink:
            try:
                code: int | None = subprocess.run(
                    argv,
                    env=env,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    timeout=RUN_TIMEOUT_S,
                    check=False,
                ).returncode
            except subprocess.TimeoutExpired:
                code = None
        out = output.read_text(encoding="utf-8")
        yield DemoRun(code, out, _relay_home(out))
    finally:
        out = output.read_text(encoding="utf-8") if output.exists() else ""
        home = _relay_home(out)
        _guarded(
            f"tmux -L {SOCKET} kill-session -t {SESSION}",
            lambda: subprocess.run(
                [tmux_bin, "-L", SOCKET, "kill-session", "-t", f"={SESSION}"],
                capture_output=True,
                timeout=TMUX_TIMEOUT_S,
                check=False,
            ),
        )
        if home is not None:
            _guarded("stopping relayd", lambda: stop_daemon(paths_in(home).socket))
        _guarded(
            f"deleting Codex thread {THREAD}",
            lambda: _delete_thread(codex_bin, accounts.codex_home),
        )


def _messages(relay_home: Path) -> list[Envelope]:
    """Every message the three agents sent, oldest first."""
    store = Store(paths_in(relay_home).db)
    try:
        rows = [
            row
            for name in ("architect", "builder", "reviewer")
            for row in store.sent_by(name, 1000)
        ]
    finally:
        store.close()
    # Rows sharing a timestamp stay in reply order: a reply's hop is one above its parent's.
    rows.sort(key=lambda row: (row.created_at, row.envelope.hop))
    return [row.envelope for row in rows]


@pytest.mark.live_demo
@pytest.mark.usefixtures("claude_bin")
def test_the_agents_fix_the_tests_and_close_the_context(demo_run: DemoRun) -> None:
    assert demo_run.code == 0, demo_run.out
    assert demo_run.relay_home is not None, f"no MZ_RELAY_HOME=<path> line:\n{demo_run.out}"
    envelopes = _messages(demo_run.relay_home)
    trail = [(e.source, e.to, e.kind.value, e.hop) for e in envelopes]
    routes = {(e.source, e.to, e.kind) for e in envelopes}
    kickoff = next(
        (
            e
            for e in envelopes
            if (e.source, e.to, e.kind) == ("architect", "builder", Kind.REQUEST)
        ),
        None,
    )
    assert kickoff is not None, trail
    last = [e for e in envelopes if e.context_id == kickoff.context_id][-1]

    assert ("builder", "reviewer", Kind.REQUEST) in routes, trail
    assert (last.source, last.to, last.kind) == ("architect", "builder", Kind.DONE), trail
    assert len(envelopes) <= MAX_MESSAGES, trail
    assert max(e.hop for e in envelopes) <= MAX_HOP, trail
