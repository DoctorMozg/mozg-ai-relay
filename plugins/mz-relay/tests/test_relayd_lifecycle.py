"""relayd as a real process: socket/lock lifecycle, idle exit, recovery, daemon CLI."""

from __future__ import annotations

import fcntl
import os
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import BIN
from mz_relay.client import RelayClient
from mz_relay.clock import SystemClock
from mz_relay.envelope import Status
from mz_relay.errors import DaemonUnavailable
from mz_relay.launcher import DaemonLauncher
from mz_relay.paths import Paths
from mz_relay.store import StatusChange, Store
from relay_rig import PROC_WAIT_S, RawConn, envelope, mid, paths_in, pull_roster, stop_daemon


@dataclass
class Home:
    path: Path
    paths: Paths
    env: dict[str, str]
    procs: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def start(self, *args: str) -> subprocess.Popen[bytes]:
        with open(self.stderr_file(len(self.procs)), "wb") as err:
            proc = subprocess.Popen(
                [str(BIN), "daemon", *args],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
            )
        self.procs.append(proc)
        return proc

    def stderr_file(self, index: int) -> Path:
        return self.path / f"daemon-stderr-{index}.txt"

    def stderr_of(self, proc: subprocess.Popen[bytes]) -> str:
        return self.stderr_file(self.procs.index(proc)).read_text(errors="replace")

    def connect(self, proc: subprocess.Popen[bytes] | None = None) -> RelayClient:
        """Connects and says hello once the daemon serves; fails fast if it exits first."""
        deadline = time.monotonic() + PROC_WAIT_S
        while True:
            if proc is not None and proc.poll() is not None:
                pytest.fail(f"daemon exited {proc.returncode}: {self.stderr_of(proc)}")
            client = RelayClient(self.paths.socket, timeout_s=2.0)
            try:
                client.connect(client="cli")
            except DaemonUnavailable:
                client.close()
                if time.monotonic() > deadline:
                    pytest.fail(f"relayd did not serve within {PROC_WAIT_S} s")
                time.sleep(0.05)
                continue
            return client

    def cleanup(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=PROC_WAIT_S)
        # A daemon the code under test spawned is no child of ours; reach it by its socket.
        stop_daemon(self.paths.socket)


@pytest.fixture
def home(relay_home: Path, relay_env: dict[str, str]) -> Iterator[Home]:
    paths = paths_in(relay_home)
    paths.roster.write_text(pull_roster(relay_home), encoding="utf-8")
    built = Home(path=relay_home, paths=paths, env=dict(relay_env))
    yield built
    built.cleanup()


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def status(client: RelayClient) -> dict[str, Any]:
    return cast(dict[str, Any], client.call("status", {}))


def register(client: RelayClient, home: Home, name: str) -> str:
    args = {"name": name, "harness": "opencode", "cwd": str(home.path), "pid": os.getpid()}
    return str(client.call("register", args)["token"])


def lock_is_free(lock: Path) -> bool:
    with open(lock, "a+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True


def run_cli(home: Home, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN), *args],
        env=home.env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


class TestLifecycle:
    def test_serves_on_private_socket_and_reports_its_own_pid(self, home: Home) -> None:
        proc = home.start()
        with closing(home.connect(proc)) as client:
            assert status(client)["pid"] == proc.pid
        assert mode_of(home.paths.socket) == 0o600
        assert mode_of(home.paths.runtime_dir) == 0o700

    def test_shim_execs_so_the_daemon_process_is_python(self, home: Home) -> None:
        proc = home.start()
        home.connect(proc).close()

        comm = Path(f"/proc/{proc.pid}/comm").read_text().strip()

        assert comm.startswith("python")

    def test_second_daemon_exits_zero_while_first_keeps_serving(self, home: Home) -> None:
        first = home.start()
        home.connect(first).close()

        second = home.start()

        assert second.wait(timeout=PROC_WAIT_S) == 0
        with closing(home.connect(first)) as client:
            assert status(client)["pid"] == first.pid

    def test_stale_socket_file_is_unlinked_and_served(self, home: Home) -> None:
        home.paths.ensure_dirs()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(home.paths.socket))
        stale.close()
        assert stat.S_ISSOCK(os.stat(home.paths.socket).st_mode)

        proc = home.start()
        with closing(home.connect(proc)) as client:
            assert status(client)["pid"] == proc.pid

    def test_regular_file_at_socket_path_is_left_intact_and_exits_2(self, home: Home) -> None:
        home.paths.ensure_dirs()
        home.paths.socket.write_text("keep me", encoding="utf-8")

        proc = home.start()

        assert proc.wait(timeout=PROC_WAIT_S) == 2
        assert str(home.paths.socket) in home.stderr_of(proc)
        assert home.paths.socket.read_text(encoding="utf-8") == "keep me"

    def test_invalid_roster_exits_2_naming_it_and_leaves_no_socket_or_lock(
        self, home: Home
    ) -> None:
        home.paths.roster.write_text("version = 2\n", encoding="utf-8")

        proc = home.start()

        assert proc.wait(timeout=PROC_WAIT_S) == 2
        assert str(home.paths.roster) in home.stderr_of(proc)
        assert not home.paths.socket.exists()
        assert lock_is_free(home.paths.lock)

    def test_sigterm_exits_zero_removes_socket_and_frees_lock(self, home: Home) -> None:
        proc = home.start()
        home.connect(proc).close()

        proc.send_signal(signal.SIGTERM)

        assert proc.wait(timeout=PROC_WAIT_S) == 0
        assert not home.paths.socket.exists()
        assert lock_is_free(home.paths.lock)

    def test_idle_exit_without_clients(self, home: Home) -> None:
        proc = home.start("--idle-exit-s", "1")
        home.connect(proc).close()

        assert proc.wait(timeout=PROC_WAIT_S) == 0

    def test_idle_exit_waits_while_a_client_is_connected(self, home: Home) -> None:
        proc = home.start("--idle-exit-s", "1")
        with closing(home.connect(proc)) as client:
            time.sleep(2.0)

            assert proc.poll() is None
            assert status(client)["pid"] == proc.pid

    def test_garbage_line_gets_invalid_request_and_connection_stays_usable(
        self, home: Home
    ) -> None:
        proc = home.start()
        home.connect(proc).close()
        with RawConn(home.paths.socket) as conn:
            assert conn.hello()["ok"] is True

            conn.send_line(b"garbage\n")
            bad = conn.reply()
            good = conn.request("status", {}, rid=2)

        assert (bad["ok"], bad["rid"], bad["error"]["code"]) == (False, None, "invalid_request")
        assert (good["ok"], good["rid"], good["result"]["pid"]) == (True, 2, proc.pid)

    def test_killed_daemon_is_respawned_and_queued_message_survives(self, home: Home) -> None:
        proc = home.start()
        with closing(home.connect(proc)) as client:
            alice = register(client, home, "alice")
            msg_id = mid(1)
            send = {"token": alice, "id": msg_id, "to": "bob", "kind": "request", "body": "hi"}
            assert client.call("send", send)["status"] == "queued"
        proc.kill()
        proc.wait(timeout=PROC_WAIT_S)

        launcher = DaemonLauncher(home.paths, clock=SystemClock(), env=home.env)
        with closing(launcher.connect(client="cli", wait_s=5.0)) as fresh:
            bob = register(fresh, home, "bob")
            inbox = cast(dict[str, Any], fresh.call("inbox", {"token": bob}))

        assert [m["envelope"]["id"] for m in inbox["messages"]] == [msg_id]

    def test_row_left_delivering_is_requeued_at_start(self, home: Home) -> None:
        home.paths.ensure_dirs()
        now = time.time()
        with closing(Store(home.paths.db)) as store:
            msg_id = mid(1)
            store.insert_message(envelope(msg_id, at=now), Status.QUEUED, now, next_attempt_at=now)
            claim = StatusChange(status=Status.DELIVERING, lease_until=now + 120.0)
            assert store.update("alice", msg_id, claim, now, expected=Status.QUEUED)

        proc = home.start()
        with closing(home.connect(proc)) as client:
            counts = cast(dict[str, int], status(client)["counts"])

        assert counts.get("queued") == 1
        assert counts.get("delivering", 0) == 0


class TestDaemonCli:
    def test_stop_prints_pid_and_daemon_exits(self, home: Home) -> None:
        proc = home.start()
        home.connect(proc).close()

        done = run_cli(home, "stop")

        assert (done.returncode, done.stdout.strip()) == (0, f"stopped relayd {proc.pid}")
        assert proc.wait(timeout=PROC_WAIT_S) == 0

    def test_stop_without_daemon_exits_3_and_creates_no_socket(self, home: Home) -> None:
        done = run_cli(home, "stop")

        assert done.returncode == 3
        assert "mz-relay: relayd is not running" in done.stderr
        assert not home.paths.socket.exists()

    def test_daemon_ensure_leaves_a_serving_daemon_and_exits_0(self, home: Home) -> None:
        done = run_cli(home, "daemon", "--ensure")

        assert done.returncode == 0, done.stderr
        home.connect().close()
