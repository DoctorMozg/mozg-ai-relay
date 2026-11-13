"""Fixtures and constants shared by the mz-relay plugin tests.

The suite is Linux-only by design: it reads /proc, runs /bin/bash and keeps sockets under /tmp.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

TESTS = Path(__file__).resolve().parent
# The suite-wide pytest config lists no plugin tests folder, so `relay_rig` needs this.
sys.path.insert(0, str(TESTS))

from fakes_claude import SESSION_ID, TranscriptWriter, transcript_in  # noqa: E402
from relay_rig import (  # noqa: E402
    BIN,
    PLUGIN,
    SRC,
    EngineRig,
    FakeAdapter,
    FakeRelayd,
    Reply,
    ServiceRig,
    paths_in,
    running_daemon,
)

if TYPE_CHECKING:
    from mz_relay.paths import Paths
    from mz_relay.store import Store

__all__ = ["BIN", "PLUGIN", "SRC", "TESTS"]

# Variables of a surrounding agent session would leak its identity into the code under test.
FOREIGN_PREFIXES = ("CLAUDE", "CODEX", "OPENCODE", "PI_CODING_AGENT", "MZ_RELAY")


@pytest.fixture
def relay_home() -> Iterator[Path]:
    """A fresh MZ_RELAY_HOME directly under /tmp, short enough for Unix socket paths."""
    home = Path(tempfile.mkdtemp(prefix="mzr-", dir="/tmp"))
    yield home
    # A daemon the test started may still be removing its socket while this runs.
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def relay_env(relay_home: Path) -> dict[str, str]:
    """The test process environment without agent-session variables, pointed at relay_home."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(FOREIGN_PREFIXES)}
    env["MZ_RELAY_HOME"] = str(relay_home)
    env["MZ_RELAY_PYTHON"] = sys.executable
    return env


@pytest.fixture
def agent_free_env(relay_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Makes this process's own environment equal relay_env, for code that reads os.environ."""
    for key in list(os.environ):
        if key not in relay_env:
            monkeypatch.delenv(key)
    for key, value in relay_env.items():
        monkeypatch.setenv(key, value)
    return relay_env


@pytest.fixture
def relay_paths(relay_home: Path) -> Paths:
    """Paths under relay_home with the state and runtime dirs created."""
    paths = paths_in(relay_home)
    paths.ensure_dirs()
    return paths


@pytest.fixture
def daemon_paths(relay_paths: Paths) -> Iterator[Paths]:
    """relay_paths with an in-process relayd serving the pull roster and no adapters."""
    with running_daemon(relay_paths):
        yield relay_paths


@pytest.fixture
def fake_relayd(relay_paths: Paths) -> Iterator[Callable[[Reply], FakeRelayd]]:
    """Starts FakeRelayd servers on relay_paths.socket; each is stopped after the test."""
    servers: list[FakeRelayd] = []

    def start(reply: Reply) -> FakeRelayd:
        server = FakeRelayd(relay_paths.socket, reply)
        servers.append(server)
        return server

    yield start
    # Every server is stopped before the first recorded reply error is raised.
    errors: list[Exception] = []
    for server in servers:
        try:
            server.stop()
        except Exception as error:
            errors.append(error)
    if errors:
        raise errors[0]


@pytest.fixture
def db(relay_home: Path) -> Path:
    return relay_home / "relay.db"


@pytest.fixture
def store(db: Path) -> Iterator[Store]:
    from mz_relay.store import Store

    opened = Store(db)
    yield opened
    opened.close()


@pytest.fixture
def rig(relay_home: Path) -> Iterator[ServiceRig]:
    """A ServiceRig over the pull roster."""
    with ServiceRig(relay_home) as built:
        yield built


@pytest.fixture
def make_rig(relay_home: Path) -> Iterator[Callable[..., EngineRig]]:
    """Builds an EngineRig with `adapter` for opencode and, optionally, `pi` for pi."""
    from mz_relay.roster import Harness

    rigs: list[EngineRig] = []

    def make(
        adapter: FakeAdapter, *, pi: FakeAdapter | None = None, extra_agents: str = ""
    ) -> EngineRig:
        adapters = {Harness.OPENCODE: adapter}
        if pi is not None:
            adapters[Harness.PI] = pi
        built = EngineRig(relay_home, adapters, extra_agents)
        rigs.append(built)
        return built

    yield make
    for built in rigs:
        built.store.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A Claude config dir with an empty sessions/ registry folder."""
    (tmp_path / "cfg" / "sessions").mkdir(parents=True)
    return tmp_path / "cfg"


@pytest.fixture
def writer(config_dir: Path) -> TranscriptWriter:
    """Writes the transcript of session SESSION_ID under config_dir."""
    return TranscriptWriter(transcript_in(config_dir), session_id=SESSION_ID)
