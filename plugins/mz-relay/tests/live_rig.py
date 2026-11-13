"""Shared pieces of the live Pi, OpenCode and Codex smokes: a TUI in a private tmux server,
a tools-only peer, and one Store reader for the rows the tests wait on."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

from mz_relay.client import RelayClient
from mz_relay.envelope import Status
from mz_relay.ids import new_id
from mz_relay.store import Store, StoredMessage
from relay_rig import wait_until

START_S = 120.0
TURN_S = 240.0
# A peer turn that would touch a file has done so well within this window.
SETTLE_S = 60.0
POLL_S = 0.5
ARRIVED = frozenset({Status.DELIVERED, Status.ACKED})
TMUX_TIMEOUT_S = 30.0
# A pane started from inside tmux would otherwise talk to the user's own server.
TMUX_VARIABLES = ("TMUX", "TMUX_PANE")


class TmuxPane:
    """Runs argv in a fresh tmux server; env goes to the server, so no secret shows up in a
    command line. The pane stays after its process exits, so its output can be read."""

    def __init__(
        self, tmux: str, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> None:
        self._tmux = tmux
        self._server = f"mzr-{uuid.uuid4().hex[:8]}"
        self._env = {key: value for key, value in env.items() if key not in TMUX_VARIABLES}
        # One invocation: remain-on-exit is set before the pane's process can exit.
        setup = ["start-server", ";", "set-option", "-g", "remain-on-exit", "on", ";"]
        session = ["new-session", "-d", "-s", "main", "-x", "200", "-y", "50", "-c", str(cwd)]
        self._run(*setup, *session, shlex.join(argv))
        self._socket: Path | None = None
        try:
            self._socket = Path(self._run("display-message", "-p", "#{socket_path}").strip())
        except BaseException:
            self.close()
            raise

    def _run(self, *args: str) -> str:
        try:
            done = subprocess.run(
                [self._tmux, "-L", self._server, *args],
                env=self._env,
                capture_output=True,
                text=True,
                timeout=TMUX_TIMEOUT_S,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise AssertionError(f"tmux {args[0]} failed: {exc.stderr.strip()}") from exc
        return done.stdout

    def send_line(self, text: str, *, pause_s: float = 0.0) -> None:
        """Types text literally, waits pause_s, then presses Enter."""
        self._run("send-keys", "-t", "main", "-l", "--", text)
        # A TUI may still be completing a slash command when Enter arrives too soon.
        if pause_s > 0:
            time.sleep(pause_s)
        self._run("send-keys", "-t", "main", "Enter")

    def screen(self) -> str:
        """The pane with its whole scrollback, wrapped lines joined."""
        return self._run("capture-pane", "-p", "-J", "-S", "-", "-t", "main")

    def _check_alive(self) -> None:
        dead = self._run("display-message", "-p", "-t", "main", "#{pane_dead}").strip()
        if dead == "1":
            output = self.screen().rstrip()
            raise AssertionError(f"the pane's process exited; its output:\n{output}")

    def wait_for(self, needle: str, timeout_s: float) -> bool:
        """True once needle is on screen; raises with the output when the process exited."""

        def shown() -> bool:
            if needle in self.screen():
                return True
            self._check_alive()
            return False

        return wait_until(shown, timeout_s, POLL_S)

    def wait_quiet(self, quiet_s: float, timeout_s: float) -> bool:
        """True once the screen has shown something and stayed unchanged for quiet_s."""
        deadline = time.monotonic() + timeout_s
        last, since = "", time.monotonic()
        while time.monotonic() < deadline:
            self._check_alive()
            now_screen = self.screen()
            if now_screen != last:
                last, since = now_screen, time.monotonic()
            elif last.strip() and time.monotonic() - since >= quiet_s:
                return True
            time.sleep(POLL_S)
        return False

    def close(self) -> None:
        subprocess.run(
            [self._tmux, "-L", self._server, "kill-server"],
            env=self._env,
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
            check=False,
        )
        # kill-server leaves the socket file behind.
        if self._socket is not None:
            self._socket.unlink(missing_ok=True)

    def __enter__(self) -> TmuxPane:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Architect:
    """A tools-only agent: a RelayClient registered without a wake-up channel, sending to one
    target. `harness` must be one with no session in the test, so replies stay pull-only."""

    def __init__(self, socket: Path, cwd: Path, *, harness: str, target: str) -> None:
        self._target = target
        self._client = RelayClient(socket)
        self._client.connect(client="cli")
        args = {"name": "architect", "harness": harness, "cwd": str(cwd), "pid": os.getpid()}
        self._token = str(self._client.call("register", args)["token"])

    def send(self, body: str, *, kind: str = "request") -> str:
        msg_id = new_id()
        args = {"token": self._token, "id": msg_id, "to": self._target, "kind": kind, "body": body}
        self._client.call("send", args)
        return msg_id

    def __enter__(self) -> Architect:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()


class RelayRows:
    """One Store kept open on relayd's database while a test polls it."""

    def __init__(self, db: Path) -> None:
        self._store = Store(db)

    def row(self, source: str, msg_id: str) -> StoredMessage | None:
        return self._store.get(source, msg_id)

    def arrived_via(self, source: str, msg_id: str, via: str) -> bool:
        row = self.row(source, msg_id)
        return row is not None and row.status in ARRIVED and row.via == via

    def sent_with(self, source: str, needle: str) -> bool:
        return any(needle in row.envelope.body for row in self._store.sent_by(source))

    def __enter__(self) -> RelayRows:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._store.close()
