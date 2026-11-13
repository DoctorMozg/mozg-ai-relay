"""Connects to relayd, spawning it once under spawn.lock and retiring an old-protocol daemon."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from mz_relay.client import CLIENT_KINDS, RelayClient
from mz_relay.clock import Clock
from mz_relay.errors import DaemonUnavailable, PathError, ProtocolMismatch
from mz_relay.paths import Paths

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parents[2]
POLL_S = 0.05
RETIRE_WAIT_S = 2.0
ENV_NAMES = frozenset(
    {"PATH", "LANG", "TZ", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME"}
)
ENV_PREFIXES = ("LC_", "MZ_RELAY_")


def daemon_command() -> list[str]:
    """Runs the plugin's runner directly, bypassing the shell shim's interpreter search."""
    runner = PLUGIN_DIR / "bin" / "_mz_relay_run.py"
    return [sys.executable, "-I", "-B", str(runner), "daemon"]


def daemon_env(env: Mapping[str, str], pw_home: Path) -> dict[str, str]:
    """The allowlisted part of env, so no agent-session variable leaks into the shared daemon."""
    if not pw_home.is_absolute():
        raise PathError(f"passwd home must be absolute, got {pw_home}")
    kept = {
        key: value for key, value in env.items() if key in ENV_NAMES or key.startswith(ENV_PREFIXES)
    }
    kept["HOME"] = str(pw_home)
    return kept


class DaemonLauncher:
    def __init__(
        self,
        paths: Paths,
        *,
        clock: Clock,
        spawn: Callable[..., object] = subprocess.Popen,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if command is not None and not command:
            raise ValueError("command must not be empty")
        self._paths = paths
        self._clock = clock
        self._spawn = spawn
        self._command = list(command) if command is not None else daemon_command()
        self._env = dict(env) if env is not None else dict(os.environ)

    def connect(self, *, client: str, wait_s: float = 3.0) -> RelayClient:
        """Returns a client past hello, starting relayd when nothing serves within wait_s."""
        if client not in CLIENT_KINDS:
            raise ValueError(
                f"client must be one of {', '.join(sorted(CLIENT_KINDS))}, got {client!r}"
            )
        if wait_s <= 0:
            raise ValueError(f"wait_s must be positive, got {wait_s}")
        deadline = self._clock.now() + wait_s
        relay = self._attempt(client)
        if relay is not None:
            return relay
        self._paths.ensure_dirs()
        with self._spawn_lock(deadline):
            # Another launcher may have spawned relayd while this one waited for the lock.
            relay = self._attempt(client)
            if relay is not None:
                return relay
            self._start_daemon()
            return self._poll(client, deadline)

    def _attempt(self, client: str) -> RelayClient | None:
        # A failed connect closes its own socket, so nothing leaks on any error here.
        relay = RelayClient(self._paths.socket)
        try:
            relay.connect(client=client)
        except DaemonUnavailable:
            return None
        except ProtocolMismatch as exc:
            self._retire(exc)
            return None
        return relay

    def _retire(self, mismatch: ProtocolMismatch) -> None:
        log.warning(
            "relayd speaks protocol %s; shutting it down to start the current one",
            mismatch.data.get("daemon_protocol"),
        )
        # shutdown needs no hello, so a fresh connection reaches a daemon that refused ours.
        relay = RelayClient(self._paths.socket)
        try:
            relay.open()
            relay.call("shutdown", {})
        except DaemonUnavailable as exc:
            log.info("old relayd went away during shutdown: %s", exc)
        finally:
            relay.close()
        self._wait_for_daemon_lock()

    def _wait_for_daemon_lock(self) -> None:
        # The daemon holds relayd.lock until its socket is gone; a spawn before then would clash.
        try:
            fd = os.open(self._paths.lock, os.O_RDWR)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("cannot open %s: %s", self._paths.lock, exc)
            raise DaemonUnavailable(
                f"cannot open {self._paths.lock}: {exc.strerror or exc}",
                {"lock": str(self._paths.lock)},
            ) from exc
        try:
            deadline = self._clock.now() + RETIRE_WAIT_S
            while not _try_flock(fd):
                if self._clock.now() >= deadline:
                    log.warning("old relayd still holds %s after shutdown", self._paths.lock)
                    raise DaemonUnavailable(
                        f"old relayd did not release {self._paths.lock} within"
                        f" {RETIRE_WAIT_S:g} s of shutdown",
                        {"lock": str(self._paths.lock)},
                    )
                self._clock.sleep(POLL_S)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _spawn_lock(self, deadline: float) -> Iterator[None]:
        try:
            fd = os.open(self._paths.spawn_lock, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            log.warning("cannot open %s: %s", self._paths.spawn_lock, exc)
            raise DaemonUnavailable(
                f"cannot open {self._paths.spawn_lock}: {exc.strerror or exc}",
                {"lock": str(self._paths.spawn_lock)},
            ) from exc
        try:
            while not _try_flock(fd):
                if self._clock.now() >= deadline:
                    log.warning("another launcher held %s past the wait", self._paths.spawn_lock)
                    raise DaemonUnavailable(
                        f"could not take {self._paths.spawn_lock} before the wait ran out",
                        {"lock": str(self._paths.spawn_lock)},
                    )
                self._clock.sleep(POLL_S)
            yield
        finally:
            # Closing the descriptor drops the flock with it.
            os.close(fd)

    def _start_daemon(self) -> None:
        env = daemon_env(self._env, self._paths.pw_home)
        try:
            log_file = open(self._paths.log, "ab")  # noqa: SIM115
        except OSError as exc:
            log.warning("cannot open %s: %s", self._paths.log, exc)
            raise DaemonUnavailable(
                f"cannot open relayd's log {self._paths.log}: {exc.strerror or exc}",
                {"log": str(self._paths.log)},
            ) from exc
        with log_file as stderr:
            try:
                process = self._spawn(
                    self._command,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    close_fds=True,
                    env=env,
                )
            except OSError as exc:
                raise DaemonUnavailable(
                    f"cannot start relayd with {self._command[0]}: {exc}",
                    {"command": self._command[0]},
                ) from exc
        log.info("spawned relayd pid %s", getattr(process, "pid", None))

    def _poll(self, client: str, deadline: float) -> RelayClient:
        while True:
            relay = RelayClient(self._paths.socket)
            with contextlib.suppress(DaemonUnavailable):
                relay.connect(client=client)
                return relay
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                break
            self._clock.sleep(min(POLL_S, remaining))
        log.warning("spawned relayd did not serve at %s in time", self._paths.socket)
        raise DaemonUnavailable(
            f"relayd did not start serving at {self._paths.socket}; see {self._paths.log}",
            {"socket": str(self._paths.socket), "log": str(self._paths.log)},
        )


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True
