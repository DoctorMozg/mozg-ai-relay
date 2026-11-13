"""A session's presence: its own relayd connection holding the registration, renewed on drop."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mz_relay.client import CLIENT_KINDS, RelayClient
from mz_relay.clock import Clock
from mz_relay.errors import DaemonUnavailable, ProtocolError, RelayError
from mz_relay.launcher import DaemonLauncher
from mz_relay.roster import Harness

log = logging.getLogger(__name__)

DEFAULT_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 10.0)
# Backoff sleeps in slices so close() stops a long wait promptly.
SLEEP_SLICE_S = 0.05


@dataclass(frozen=True)
class RegisterArgs:
    name: str | None
    harness: Harness
    cwd: Path
    pid: int
    client: str
    config_dir: Path | None = None
    delivery: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.name is not None and not self.name:
            raise ValueError("name must be None or a non-empty string")
        if not self.cwd.is_absolute():
            raise ValueError(f"cwd must be absolute, got {self.cwd}")
        if self.pid <= 0:
            raise ValueError(f"pid must be positive, got {self.pid}")
        if self.client not in CLIENT_KINDS:
            raise ValueError(
                f"client must be one of {', '.join(sorted(CLIENT_KINDS))}, got {self.client!r}"
            )
        if self.config_dir is not None and not self.config_dir.is_absolute():
            raise ValueError(f"config_dir must be absolute, got {self.config_dir}")

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "name": self.name,
            "harness": self.harness.value,
            "cwd": str(self.cwd),
            "pid": self.pid,
        }
        if self.config_dir is not None:
            wire["config_dir"] = str(self.config_dir)
        if self.delivery is not None:
            wire["delivery"] = dict(self.delivery)
        return wire


@dataclass(frozen=True)
class Registered:
    token: str
    agent: dict[str, object]
    delivery_mode: str | None
    brief: str

    @classmethod
    def from_result(cls, result: Mapping[str, object]) -> Registered:
        token = result.get("token")
        agent = result.get("agent")
        mode = result.get("delivery_mode")
        brief = result.get("brief")
        if not isinstance(token, str) or not token:
            raise ProtocolError("invalid_request", "register result has no token")
        if not isinstance(agent, dict):
            raise ProtocolError("invalid_request", f"register result agent is {type(agent)}")
        if mode is not None and not isinstance(mode, str):
            raise ProtocolError("invalid_request", f"register result delivery_mode is {mode!r}")
        if not isinstance(brief, str):
            raise ProtocolError("invalid_request", f"register result brief is {type(brief)}")
        return cls(token=token, agent=dict(agent), delivery_mode=mode, brief=brief)


class PresenceSession:
    def __init__(
        self,
        launcher: DaemonLauncher,
        args: RegisterArgs,
        *,
        clock: Clock,
        backoff_s: Sequence[float] = DEFAULT_BACKOFF_S,
    ) -> None:
        if not backoff_s:
            raise ValueError("backoff_s must hold at least one delay")
        if any(delay < 0 for delay in backoff_s):
            raise ValueError(f"backoff_s delays must not be negative, got {list(backoff_s)}")
        self._launcher = launcher
        self._args = args
        self._clock = clock
        self._backoff_s = tuple(backoff_s)
        self._wait_s = 0.0
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._relay: RelayClient | None = None
        self._registered: Registered | None = None
        self._monitor: threading.Thread | None = None

    def start(self, *, wait_s: float = 3.0) -> Registered:
        """Registers over a connection of its own, then watches it for a drop."""
        if wait_s <= 0:
            raise ValueError(f"wait_s must be positive, got {wait_s}")
        if self._closed.is_set():
            raise ValueError("presence session is closed")
        if self._monitor is not None:
            raise ValueError("presence session already started")
        self._wait_s = wait_s
        relay, registered = self._register()
        with self._lock:
            self._relay, self._registered = relay, registered
        self._monitor = threading.Thread(
            target=self._watch, name=f"mz-relay-presence-{self._args.pid}", daemon=True
        )
        self._monitor.start()
        return registered

    def current(self) -> Registered:
        """The latest registration; it changes after each re-registration."""
        with self._lock:
            registered = self._registered
        if registered is None:
            raise DaemonUnavailable("this session has not registered with relayd yet")
        return registered

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            relay, self._relay = self._relay, None
        if relay is not None:
            relay.close()
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            # A reconnect in flight may block in launcher.connect for up to wait_s.
            monitor.join(self._wait_s + 1.0)

    def _register(self) -> tuple[RelayClient, Registered]:
        relay = self._launcher.connect(client=self._args.client, wait_s=self._wait_s)
        try:
            registered = Registered.from_result(relay.call("register", self._args.to_wire()))
        except BaseException:
            relay.close()
            raise
        log.info(
            "registered as %s (delivery %s)", registered.agent.get("name"), registered.delivery_mode
        )
        return relay, registered

    def _watch(self) -> None:
        while True:
            with self._lock:
                relay = self._relay
            if relay is None:
                return
            relay.recv_eof()
            if self._closed.is_set():
                return
            log.info("presence connection dropped; re-registering")
            if not self._reconnect():
                return

    def _reconnect(self) -> bool:
        attempt = 0
        while not self._closed.is_set():
            # The last backoff value repeats for every later attempt.
            delay = self._backoff_s[min(attempt, len(self._backoff_s) - 1)]
            if not self._sleep(delay):
                return False
            attempt += 1
            try:
                relay, registered = self._register()
            except RelayError as exc:
                log.warning("re-registration attempt %d failed: %s", attempt, exc)
                continue
            with self._lock:
                if self._closed.is_set():
                    relay.close()
                    return False
                self._relay, self._registered = relay, registered
            log.info("re-registered after %d attempt(s)", attempt)
            return True
        return False

    def _sleep(self, delay: float) -> bool:
        """Sleeps delay seconds on the clock; False when close() came first."""
        deadline = self._clock.now() + delay
        while not self._closed.is_set():
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                return True
            self._clock.sleep(min(SLEEP_SLICE_S, remaining))
        return False
