"""DaemonLauncher: connect, spawn once under spawn.lock, respawn an old daemon; daemon env."""

from __future__ import annotations

import fcntl
import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from conftest import PLUGIN
from mz_relay.client import RelayClient
from mz_relay.clock import SystemClock
from mz_relay.errors import DaemonUnavailable, RelayError
from mz_relay.launcher import DaemonLauncher, daemon_command, daemon_env
from mz_relay.paths import Paths
from mz_relay.protocol import PROTOCOL_VERSION
from relay_rig import (
    PROC_WAIT_S,
    WAIT_S,
    FakeProcess,
    FakeRelayd,
    InThread,
    Message,
    Reply,
    close_connection,
    fail,
    hello_then,
    ok,
    wait_until,
)

Serve = Callable[[Reply], FakeRelayd]
answer_hello = hello_then(close_connection)


class FakeSpawn:
    """Stands in for subprocess.Popen; optionally brings a fake relayd up after `delay_s`."""

    def __init__(self, on_spawn: Callable[[], object] | None = None, delay_s: float = 0.1) -> None:
        self.on_spawn = on_spawn
        self.delay_s = delay_s
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self._lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        self._errors: list[Exception] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeProcess:
        command = args[0] if args else kwargs["args"]
        assert isinstance(command, Sequence)
        with self._lock:
            self.calls.append(([str(part) for part in command], kwargs))
        if self.on_spawn is not None:
            timer = threading.Timer(self.delay_s, self._fire)
            timer.daemon = True
            with self._lock:
                self._timers.append(timer)
            timer.start()
        return FakeProcess()

    def _fire(self) -> None:
        assert self.on_spawn is not None
        # A failing callback would otherwise surface only as the launcher's timeout.
        try:
            self.on_spawn()
        except Exception as error:
            with self._lock:
                self._errors.append(error)

    def check(self) -> None:
        """Waits for pending spawn callbacks and re-raises the first one that failed."""
        with self._lock:
            timers = list(self._timers)
        for timer in timers:
            timer.join(PROC_WAIT_S)
        with self._lock:
            errors = list(self._errors)
        if errors:
            raise errors[0]


Spawns = Callable[..., FakeSpawn]


@pytest.fixture
def spawns() -> Iterator[Spawns]:
    """Builds FakeSpawns whose callback errors are raised at teardown."""
    made: list[FakeSpawn] = []

    def make(on_spawn: Callable[[], object] | None = None) -> FakeSpawn:
        spawn = FakeSpawn(on_spawn)
        made.append(spawn)
        return spawn

    yield make
    for spawn in made:
        spawn.check()


def launcher(paths: Paths, spawn: FakeSpawn) -> DaemonLauncher:
    return DaemonLauncher(
        paths,
        clock=SystemClock(),
        spawn=spawn,
        command=["fake-relayd", "daemon"],
        env={"PATH": "/usr/bin", "CLAUDECODE": "1", "HOME": "/elsewhere"},
    )


class TestLauncher:
    def test_a_running_daemon_is_used_without_spawning(
        self, relay_paths: Paths, fake_relayd: Serve, spawns: Spawns
    ) -> None:
        server = fake_relayd(answer_hello)
        spawn = spawns()
        client = launcher(relay_paths, spawn).connect(client="cli")
        client.close()
        assert spawn.calls == []
        assert server.ops() == ["hello"]

    def test_a_missing_socket_spawns_a_detached_daemon_and_connects(
        self, relay_paths: Paths, fake_relayd: Serve, spawns: Spawns
    ) -> None:
        servers: list[FakeRelayd] = []
        spawn = spawns(lambda: servers.append(fake_relayd(answer_hello)))

        client = launcher(relay_paths, spawn).connect(client="mcp")
        try:
            assert isinstance(client, RelayClient)
        finally:
            client.close()

        assert len(spawn.calls) == 1
        command, kwargs = spawn.calls[0]
        assert command == ["fake-relayd", "daemon"]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["close_fds"] is True
        assert kwargs["env"] == {"PATH": "/usr/bin", "HOME": str(relay_paths.pw_home)}
        stderr = kwargs["stderr"]
        assert isinstance(stderr, io.BufferedWriter)
        assert Path(stderr.name) == relay_paths.log
        hello = servers[0].requests[0]
        assert hello["op"] == "hello"
        assert hello["args"] == {"protocol": PROTOCOL_VERSION, "client": "mcp", "pid": os.getpid()}

    def test_a_spawn_that_never_serves_is_daemon_unavailable_after_wait_s(
        self, relay_paths: Paths, spawns: Spawns
    ) -> None:
        spawn = spawns()
        started = time.monotonic()
        with pytest.raises(DaemonUnavailable):
            launcher(relay_paths, spawn).connect(client="cli", wait_s=0.3)
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 2.0
        assert len(spawn.calls) == 1

    def test_two_launchers_racing_spawn_exactly_once(
        self, relay_paths: Paths, fake_relayd: Serve, spawns: Spawns
    ) -> None:
        spawn = spawns(lambda: fake_relayd(answer_hello))
        racers = [
            InThread(lambda: launcher(relay_paths, spawn).connect(client="mcp", wait_s=3.0))
            for _ in range(2)
        ]
        clients = [racer.result() for racer in racers]
        for client in clients:
            client.close()

        assert len(spawn.calls) == 1

    def test_a_protocol_mismatch_shuts_the_old_daemon_down_then_respawns(
        self, relay_paths: Paths, fake_relayd: Serve, spawns: Spawns
    ) -> None:
        # The old daemon holds relayd.lock until it has unlinked its socket, like the real one.
        lock_fd = os.open(relay_paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        guard = threading.Lock()
        lock_open = [True]

        def release_lock() -> None:
            with guard:
                if lock_open[0]:
                    os.close(lock_fd)
                    lock_open[0] = False

        def retire() -> None:
            old.stop()
            release_lock()

        def old_reply(req: Message) -> Message | None:
            if req["op"] == "shutdown":
                # Retiring later than the spawn delay catches a launcher that skips the lock wait.
                threading.Timer(0.3, retire).start()
                return ok(req, {})
            return fail(
                req, "protocol_mismatch", "daemon speaks protocol 0", {"daemon_protocol": 0}
            )

        old = fake_relayd(old_reply)
        fresh: list[FakeRelayd] = []
        spawn = spawns(lambda: fresh.append(fake_relayd(answer_hello)))
        try:
            client = launcher(relay_paths, spawn).connect(client="cli", wait_s=3.0)
            client.close()
        finally:
            release_lock()

        assert "shutdown" in old.ops()
        assert len(spawn.calls) == 1
        assert fresh[0].ops()[0] == "hello"


class TestLauncherFailures:
    def test_a_hello_refused_with_another_error_raises_it_and_closes_the_socket(
        self, relay_paths: Paths, fake_relayd: Serve, spawns: Spawns
    ) -> None:
        server = fake_relayd(lambda req: fail(req, "internal", "relayd is broken"))

        with pytest.raises(RelayError) as caught:
            launcher(relay_paths, spawns()).connect(client="cli", wait_s=0.5)

        assert caught.value.code == "internal"
        hellos = len(server.args_of("hello"))
        assert hellos >= 1
        assert wait_until(lambda: server.hangups == hellos, WAIT_S), (
            "a refused hello kept its socket"
        )

    @pytest.mark.parametrize("blocked", ["spawn_lock", "log"])
    def test_an_os_error_opening_launcher_files_is_daemon_unavailable(
        self, relay_paths: Paths, spawns: Spawns, blocked: str
    ) -> None:
        # A directory where the launcher opens a file makes open() fail with an OSError.
        target: Path = getattr(relay_paths, blocked)
        target.mkdir()
        spawn = spawns()

        with pytest.raises(DaemonUnavailable):
            launcher(relay_paths, spawn).connect(client="cli", wait_s=0.5)

        assert spawn.calls == []

    def test_a_daemon_lock_that_cannot_be_opened_while_retiring_is_daemon_unavailable(
        self, relay_paths: Paths, fake_relayd: Serve, spawns: Spawns
    ) -> None:
        # A directory at relayd.lock makes its open() fail with an OSError other than ENOENT.
        relay_paths.lock.mkdir()

        def old_reply(req: Message) -> Message:
            if req["op"] == "shutdown":
                return ok(req, {})
            return fail(req, "protocol_mismatch", "daemon speaks 0", {"daemon_protocol": 0})

        old = fake_relayd(old_reply)
        spawn = spawns()

        with pytest.raises(DaemonUnavailable) as caught:
            launcher(relay_paths, spawn).connect(client="cli", wait_s=0.5)

        assert str(relay_paths.lock) in caught.value.message
        assert "shutdown" in old.ops()
        assert spawn.calls == []


class TestDaemonEnv:
    def test_only_allowlisted_variables_pass_and_home_is_the_passwd_home(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "MZ_RELAY_HOME": "/tmp/mzr-x",
            "CLAUDE_CODE_MESSAGING_SOCKET": "/run/user/1000/cc-socks/1.sock",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "s-1",
            "CODEX_HOME": "/home/u/.codex",
            "HOME": "/x",
        }
        assert daemon_env(env, Path("/home/u")) == {
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "MZ_RELAY_HOME": "/tmp/mzr-x",
            "HOME": "/home/u",
        }

    def test_locale_timezone_xdg_and_relay_variables_are_kept(self) -> None:
        env = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C",
            "LC_CTYPE": "C.UTF-8",
            "TZ": "UTC",
            "XDG_STATE_HOME": "/s",
            "XDG_CONFIG_HOME": "/c",
            "MZ_RELAY_PYTHON": "/usr/bin/python3.12",
        }
        assert daemon_env(env, Path("/home/u")) == env | {"HOME": "/home/u"}

    def test_home_is_set_when_the_caller_has_none(self) -> None:
        assert daemon_env({}, Path("/home/u")) == {"HOME": "/home/u"}

    def test_daemon_command_runs_the_plugin_runner_isolated(self) -> None:
        command = daemon_command()
        assert command[:3] == [sys.executable, "-I", "-B"]
        assert Path(command[3]).resolve() == (PLUGIN / "bin" / "_mz_relay_run.py").resolve()
        assert command[4:] == ["daemon"]
