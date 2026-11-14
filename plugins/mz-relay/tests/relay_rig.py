"""Shared test rig: the test roster, fakes, builders, bounded waits and relayd helpers.

Production imports are deferred into the helpers that need them, so the pure helpers
load for the early units before the store, engine and service exist.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import itertools
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mz_relay.clock import Clock
    from mz_relay.delivery import (
        DeliveryBinding,
        DeliveryJob,
        DeliveryResult,
        HarnessAdapter,
        Outcome,
    )
    from mz_relay.envelope import Envelope, Status
    from mz_relay.extensions import Extension
    from mz_relay.launcher import DaemonLauncher
    from mz_relay.paths import Paths
    from mz_relay.relayd import RelayDaemon
    from mz_relay.roster import AgentSpec, Harness, Roster, RosterCache
    from mz_relay.service import ConnState, RelayService
    from mz_relay.store import StatusChange, Store, StoredMessage

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin" / "mz-relay"
SRC = PLUGIN / "src"

# A ceiling for a process or thread that must finish; it costs nothing when the code is fast.
PROC_WAIT_S = 10.0
# A ceiling for background workers and presence threads to reach a state.
WAIT_S = 5.0

# Wire frames are parsed JSON, so their values are untyped.
Message = dict[str, Any]
Reply = Callable[[Message], Message | None]

BOB_SOCKET: dict[str, object] = {"socket": "/tmp/mzr-bob.sock", "pid": 4242}
BOB_STALE: dict[str, object] = {"socket": "/tmp/mzr-bob.sock", "pid": 4242, "stale": True}
CAROL_SOCKET: dict[str, object] = {"socket": "/tmp/mzr-carol.sock", "pid": 4243}
NATIVE: dict[str, object] = {"socket": "/tmp/mzr-native.sock", "pid": 4242, "kind": "native"}
REWAKE: dict[str, object] = {"config_dir": "/tmp/mzr-cfg", "session_id": "s-1", "rewake": True}

DAVE_PI = """
[agents.dave]
harness = "pi"
label = "local"
role = "tester"
description = "Runs checks on a local model."
"""

# A second corporate sender, so two senders can hold messages for alice under one id.
ERIN_CORPORATE = """
[agents.erin]
harness = "opencode"
label = "corporate"
role = "auditor"
description = "Audits changes from a second corporate account."
may_send_to = ["alice"]
"""


def pull_roster(home: Path) -> str:
    """The pull-only test roster; alice works in `home / "alice"`, so cwd lookups stay in home."""
    return f"""\
version = 1

[agents.alice]
harness = "opencode"
label = "personal"
role = "planner"
description = "Plans the work and hands tasks to bob."
cwd = "{home / "alice"}"

[agents.bob]
harness = "opencode"
label = "personal"
role = "builder"
description = "Builds what alice plans and reports back."

[agents.carol]
harness = "opencode"
label = "corporate"
role = "auditor"
description = "Audits changes from the corporate side."
may_send_to = ["alice"]
"""


def paths_in(home: Path) -> Paths:
    """Paths for `MZ_RELAY_HOME=home`, with the passwd home inside it too; creates nothing."""
    from mz_relay.paths import Paths

    return Paths.from_env({"MZ_RELAY_HOME": str(home)}, uid=os.getuid(), pw_home=home)


# Builders


def mid(n: int) -> str:
    """A valid message id whose order follows n."""
    return f"01JC{n:022d}"


def token(n: int) -> str:
    return f"{n:032x}"


def envelope(
    msg_id: str,
    *,
    source: str = "alice",
    to: str = "bob",
    kind: str = "request",
    context_id: str | None = None,
    at: float = 10.0,
    label: str = "personal",
    body: str | None = None,
) -> Envelope:
    """A hop-0 envelope stamped at `at`; a request needs an ack."""
    from mz_relay.clock import iso_utc
    from mz_relay.envelope import Envelope, Kind
    from mz_relay.roster import Label

    return Envelope(
        id=msg_id,
        source=source,
        to=to,
        kind=Kind(kind),
        context_id=context_id or msg_id,
        in_reply_to=None,
        hop=0,
        time=iso_utc(at),
        ttl_s=3600,
        label=Label(label),
        ack_required=Kind(kind) is Kind.REQUEST,
        body=body if body is not None else f"body of {msg_id}",
    )


def result(outcome: Outcome, detail: str = "") -> DeliveryResult:
    from mz_relay.delivery import DeliveryResult

    return DeliveryResult(outcome=outcome, detail=detail)


# Bounded waits and threads


def wait_until(predicate: Callable[[], bool], timeout_s: float, interval_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def socket_accepts(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(path))
        except OSError:
            return False
    return True


class InThread[T]:
    """Runs `fn` on a daemon thread; `result` waits a bounded time and re-raises its exception."""

    def __init__(self, fn: Callable[[], T]) -> None:
        self._fn = fn
        self._box: tuple[T] | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._box = (self._fn(),)
        except BaseException as error:  # handed to the test thread by result()
            self._error = error

    def done(self) -> bool:
        return not self._thread.is_alive()

    def result(self, timeout_s: float = PROC_WAIT_S) -> T:
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            raise AssertionError(f"call did not return within {timeout_s} s")
        if self._error is not None:
            raise self._error
        assert self._box is not None
        return self._box[0]


def run_in_thread[T](fn: Callable[[], T], timeout_s: float) -> T:
    return InThread(fn).result(timeout_s)


@contextmanager
def notified_within(changed: threading.Condition, timeout_s: float) -> Iterator[threading.Event]:
    """Parks a thread on `changed`; the block must notify it within `timeout_s`."""
    ready = threading.Event()
    woke = threading.Event()

    def park() -> None:
        with changed:
            ready.set()
            if changed.wait(timeout=timeout_s + 1.0):
                woke.set()

    waiter = InThread(park)
    assert ready.wait(PROC_WAIT_S), "the waiter never parked on changed"
    yield woke
    woke.wait(timeout_s)
    waiter.result(timeout_s + PROC_WAIT_S)


# Fakes


class FakeClock:
    """A clock that moves only when a test sleeps on it or advances it."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, s: float) -> None:
        self.advance(s)

    def advance(self, s: float) -> None:
        if s < 0:
            raise ValueError(f"a clock cannot go back: advance({s})")
        with self._lock:
            self._now += s


class SleepHookClock:
    """Wraps a clock; `on_next_sleep`, when set, runs once as the next sleep starts."""

    def __init__(self, inner: Clock, on_next_sleep: Callable[[], object] | None = None) -> None:
        self._inner = inner
        self.on_next_sleep = on_next_sleep
        self.slept: list[float] = []

    def now(self) -> float:
        return self._inner.now()

    def sleep(self, s: float) -> None:
        hook, self.on_next_sleep = self.on_next_sleep, None
        if hook is not None:
            hook()
        self.slept.append(s)
        self._inner.sleep(s)


class FakeAdapter:
    """A harness adapter that replays scripted outcomes and records every job it was given."""

    def __init__(
        self,
        via: str = "fake",
        exhausted_status: Status | None = None,
        outcomes: list[DeliveryResult | Exception] | None = None,
        confirms: list[DeliveryResult | Exception] | None = None,
        roster_binding: bool = False,
        on_deliver: Callable[[DeliveryJob], None] | None = None,
        validate_error: Exception | None = None,
    ) -> None:
        from mz_relay.envelope import Status

        self.via = via
        self.exhausted_status = (
            exhausted_status if exhausted_status is not None else Status.UNDELIVERABLE
        )
        self.outcomes = list(outcomes or [])
        self.confirms = list(confirms or [])
        self.roster_binding = roster_binding
        self.on_deliver = on_deliver
        self.validate_error = validate_error
        self.delivered: list[DeliveryJob] = []
        self.confirmed: list[DeliveryJob] = []
        self.validate_calls = 0
        # The register op and a delivery worker may call in from different threads.
        self._lock = threading.Lock()

    def validate_binding(
        self, spec: AgentSpec, binding: Mapping[str, object] | None
    ) -> DeliveryBinding | None:
        from mz_relay.delivery import DeliveryBinding

        with self._lock:
            self.validate_calls += 1
        if binding:
            if self.validate_error is not None and binding.get("stale") is True:
                raise self.validate_error
            return DeliveryBinding("socket", dict(binding))
        if self.roster_binding:
            return DeliveryBinding("codex", {})
        return None

    def deliver(self, job: DeliveryJob) -> DeliveryResult:
        if self.on_deliver is not None:
            self.on_deliver(job)
        with self._lock:
            self.delivered.append(job)
            outcome = self.outcomes.pop(0) if self.outcomes else _confirmed()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def confirm(self, job: DeliveryJob) -> DeliveryResult:
        with self._lock:
            self.confirmed.append(job)
            outcome = self.confirms.pop(0) if self.confirms else _confirmed()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _confirmed() -> DeliveryResult:
    from mz_relay.delivery import Outcome

    return result(Outcome.CONFIRMED)


class FakeProcess:
    """What a spawn stand-in returns: a child that never exits on its own."""

    pid = 999_999
    returncode: int | None = None

    def poll(self) -> int | None:
        return None


class NoSpawn:
    """A spawn stand-in that starts nothing and records each call's keyword arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeProcess:
        # The launcher opened relayd.log for the child; nothing else will close it.
        stderr = kwargs.get("stderr")
        if isinstance(stderr, io.IOBase):
            stderr.close()
        self.calls.append(dict(kwargs))
        return FakeProcess()


def launcher_for(paths: Paths, spawn: Callable[..., object]) -> DaemonLauncher:
    from mz_relay.clock import SystemClock
    from mz_relay.launcher import DaemonLauncher

    return DaemonLauncher(paths, clock=SystemClock(), spawn=spawn, command=["/bin/true"], env={})


def ok(req: Message, body: Message) -> Message:
    return {"v": 1, "rid": req["rid"], "ok": True, "result": body}


def fail(req: Message, code: str, message: str, data: Message | None = None) -> Message:
    error: Message = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"v": 1, "rid": req["rid"], "ok": False, "error": error}


def hello_result() -> Message:
    from mz_relay import __version__
    from mz_relay.protocol import PROTOCOL_VERSION

    return {
        "protocol": PROTOCOL_VERSION,
        "daemon_pid": 4242,
        "version": __version__,
        "db": "/tmp/mzr-db",
        "roster": "/tmp/mzr-roster.toml",
    }


def close_connection(_req: Message) -> Message | None:
    return None


def hello_then(rest: Reply) -> Reply:
    """Answers hello like relayd and hands every other request to `rest`."""

    def reply(req: Message) -> Message | None:
        return ok(req, hello_result()) if req["op"] == "hello" else rest(req)

    return reply


class FakeRelayd:
    """A line-JSON server on a Unix socket; `reply` answers each request, None closes the line."""

    def __init__(self, path: Path, reply: Reply) -> None:
        self.path = path
        self.reply = reply
        self._log: list[tuple[socket.socket, Message]] = []
        self._conns: list[socket.socket] = []
        self._hangups = 0
        # A failing `reply` would otherwise end only its server thread and surface as a timeout.
        self._errors: list[Exception] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen()
        self._listener.settimeout(0.05)
        self._acceptor = threading.Thread(target=self._accept_loop, daemon=True)
        self._acceptor.start()

    @property
    def requests(self) -> list[Message]:
        with self._lock:
            return [req for _, req in self._log]

    @property
    def hangups(self) -> int:
        """Connections whose input ended: the client closed them, or drop_connections did."""
        with self._lock:
            return self._hangups

    def ops(self) -> list[str]:
        return [str(req["op"]) for req in self.requests]

    def args_of(self, op: str) -> list[Message]:
        return [dict(req.get("args") or {}) for req in self.requests if req["op"] == op]

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        # A client hanging up mid-line ends only its own connection.
        with contextlib.suppress(OSError), conn, conn.makefile("rb") as lines:
            for line in lines:
                try:
                    req = json.loads(line)
                except ValueError:
                    return
                with self._lock:
                    self._log.append((conn, req))
                try:
                    answer = self.reply(req)
                except Exception as error:
                    with self._lock:
                        self._errors.append(error)
                    return
                if answer is None:
                    return
                conn.sendall(json.dumps(answer).encode() + b"\n")
            with self._lock:
                self._hangups += 1

    def drop_connections(self) -> None:
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)

    def drop_connection_of(self, op: str) -> None:
        """Hangs up the connection that carried the latest `op` request."""
        with self._lock:
            conn = next(c for c, req in reversed(self._log) if req["op"] == op)
        conn.shutdown(socket.SHUT_RDWR)

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._listener.close()
        self.drop_connections()
        self.path.unlink(missing_ok=True)
        self._acceptor.join(PROC_WAIT_S)
        with self._lock:
            errors = list(self._errors)
        if errors:
            raise errors[0]


@contextmanager
def waiter_lock_held(paths: Paths, session_id: str) -> Iterator[None]:
    """Holds the session's waiter lock on its own file description, as a SessionStart waiter."""
    paths.waiter_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.waiter_dir / f"{session_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# A real relayd, in this process or as a child


@contextmanager
def running_daemon(
    paths: Paths,
    adapters: Mapping[Harness, HarnessAdapter] | None = None,
    *,
    roster_text: str | None = None,
) -> Iterator[RelayDaemon]:
    """An in-process relayd on `paths` with `roster_text` (default the pull roster)."""
    from mz_relay.clock import SystemClock
    from mz_relay.relayd import DaemonConfig, RelayDaemon

    paths.ensure_dirs()
    text = pull_roster(paths.config_dir) if roster_text is None else roster_text
    paths.roster.write_text(text, encoding="utf-8")
    daemon = RelayDaemon(DaemonConfig(paths=paths, clock=SystemClock()), dict(adapters or {}))
    runner = InThread(daemon.serve_forever)
    try:
        listening = wait_until(lambda: runner.done() or socket_accepts(paths.socket), PROC_WAIT_S)
        assert listening, f"in-process relayd did not listen within {PROC_WAIT_S} s"
        if runner.done():
            raise AssertionError(f"in-process relayd returned {runner.result()} before serving")
        yield daemon
    finally:
        daemon.request_stop()
        runner.result(PROC_WAIT_S)


class RawConn:
    """One line-JSON connection to relayd that bypasses RelayClient, for wire-level checks."""

    def __init__(self, path: Path, timeout_s: float = PROC_WAIT_S) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout_s)
        self._buffer = bytearray()
        try:
            self.sock.connect(str(path))
        except BaseException:
            self.sock.close()
            raise

    def send_line(self, raw: bytes) -> None:
        self.sock.sendall(raw)

    def send_ignoring_hangup(self, raw: bytes) -> None:
        # relayd may close mid-write (an oversized line); that close is what the test checks.
        with contextlib.suppress(OSError):
            self.sock.sendall(raw)

    def request(self, op: str, args: Message, rid: int) -> Message:
        frame = {"v": 1, "rid": rid, "op": op, "args": args}
        self.sock.sendall(json.dumps(frame).encode() + b"\n")
        return self.reply()

    def hello(self) -> Message:
        from mz_relay.protocol import PROTOCOL_VERSION

        args = {"protocol": PROTOCOL_VERSION, "client": "cli", "pid": os.getpid()}
        return self.request("hello", args, rid=1)

    def reply(self) -> Message:
        while b"\n" not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("relayd closed the connection before replying")
            self._buffer.extend(chunk)
        line, _, rest = bytes(self._buffer).partition(b"\n")
        self._buffer[:] = rest
        reply: Message = json.loads(line)
        return reply

    def closed_by_peer(self) -> bool:
        """True once relayd has closed; a close with unread input arrives as a reset."""
        try:
            return self.sock.recv(65536) == b""
        except ConnectionResetError:
            return True

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> RawConn:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def relayd_call(path: Path, op: str, args: Message) -> Message:
    """One raw exchange (hello, then `op`), so teardown never depends on RelayClient."""
    with RawConn(path, timeout_s=PROC_WAIT_S) as conn:
        conn.hello()
        return conn.request(op, args, rid=2)


def relayd_status(path: Path) -> Message:
    """relayd's `status` result, or {} while nothing answers on `path`."""
    try:
        status: Message = relayd_call(path, "status", {}).get("result", {})
    except (OSError, ValueError):
        return {}
    return status


def online_names(status: Message) -> set[str]:
    """The agents a relayd `status` result lists as online."""
    return {agent["name"] for agent in status.get("agents", []) if agent.get("online")}


def daemon_pid(path: Path) -> int | None:
    pid = relayd_status(path).get("pid")
    return pid if isinstance(pid, int) else None


def pid_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    # A killed daemon stays a zombie until its spawning process reaps it.
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def stop_daemon(path: Path) -> None:
    """Shuts down whatever relayd serves `path`, killing it if it lingers."""
    pid = daemon_pid(path)
    if pid is None:
        return
    with contextlib.suppress(OSError, ValueError):
        relayd_call(path, "shutdown", {})
    if not wait_until(lambda: not pid_running(pid), PROC_WAIT_S):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def reap(proc: subprocess.Popen[bytes], timeout_s: float = PROC_WAIT_S) -> int:
    """Closes the child's stdin and waits `timeout_s` for it to exit, then kills it."""
    if proc.stdin is not None and not proc.stdin.closed:
        with contextlib.suppress(OSError):
            proc.stdin.close()
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=PROC_WAIT_S)


class McpProc:
    """One `bin/mz-relay mcp --harness <harness>` child over stdio pipes; `name` None leaves
    MZ_RELAY_NAME unset, so the agent is resolved from the rest of `env`."""

    def __init__(
        self,
        name: str | None,
        env: Mapping[str, str],
        *,
        cwd: Path,
        log: Path,
        harness: str = "opencode",
    ) -> None:
        self.name = name or harness
        self.seen: list[bytes] = []
        self.log = log
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._rid = 0
        self._log = log.open("wb")
        child_env = dict(env)
        if name is not None:
            child_env["MZ_RELAY_NAME"] = name
        try:
            self.proc = subprocess.Popen(
                [str(BIN), "mcp", "--harness", harness],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                env=child_env,
                cwd=cwd,
            )
        except BaseException:
            self._log.close()
            raise
        stdout = self.proc.stdout
        self._pump = threading.Thread(target=self._pump_lines, args=(stdout,), daemon=True)
        self._pump.start()

    def _pump_lines(self, stream: IO[bytes] | None) -> None:
        assert stream is not None
        for line in iter(stream.readline, b""):
            self._lines.put(line)

    def write(self, raw: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def read(self, timeout_s: float = 15.0) -> Message:
        try:
            raw = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            raise AssertionError(f"{self.name}: no stdout line within {timeout_s} s") from None
        self.seen.append(raw)
        frame: Message = json.loads(raw)
        return frame

    def notify(self, method: str) -> None:
        self.write(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")

    def request(self, method: str, params: Message) -> Message:
        self._rid += 1
        frame = {"jsonrpc": "2.0", "id": self._rid, "method": method, "params": params}
        self.write(json.dumps(frame).encode() + b"\n")
        reply = self.read()
        assert reply["id"] == self._rid, reply
        return reply

    def initialize(self) -> Message:
        params: Message = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "t"},
        }
        init: Message = self.request("initialize", params)["result"]
        self.notify("notifications/initialized")
        return init

    def tool_result(self, name: str, **arguments: object) -> Message:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        tool: Message = reply["result"]
        return tool

    def tool(self, name: str, **arguments: object) -> str:
        tool = self.tool_result(name, **arguments)
        text = str(tool["content"][0]["text"])
        assert tool.get("isError", False) is False, f"{self.name} {name}: {text}"
        return text

    def finish(self) -> int:
        """Closes stdin, as the harness does at shutdown, and returns the exit code."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(timeout=PROC_WAIT_S)

    def drain(self) -> None:
        self._pump.join(PROC_WAIT_S)
        while not self._lines.empty():
            self.seen.append(self._lines.get_nowait())

    def stderr_text(self) -> str:
        self._log.flush()
        return self.log.read_text(encoding="utf-8", errors="replace")

    def close(self) -> None:
        try:
            reap(self.proc)
        finally:
            self._log.close()


# Engine and service rigs

# Ids grow across rigs, so rows of one test always sort in insertion order.
_ids = itertools.count(1)


class EngineRig:
    """Real Store and roster, a FakeClock, and the given adapters (usually FakeAdapters)."""

    def __init__(
        self, home: Path, adapters: Mapping[Harness, HarnessAdapter], extra_agents: str = ""
    ) -> None:
        from mz_relay.delivery import DeliveryEngine, EngineDeps
        from mz_relay.roster import parse_roster
        from mz_relay.store import Store

        self.clock = FakeClock()
        self.t0 = self.clock.now()
        self.roster: Roster = parse_roster(pull_roster(home) + extra_agents, pw_home=home)
        self.store: Store = Store(home / "relay.db")
        self.engine = DeliveryEngine(
            EngineDeps(
                store=self.store,
                roster=lambda: self.roster,
                adapters=dict(adapters),
                clock=self.clock,
            )
        )
        self._tokens = itertools.count(1)
        self.acked_during_delivery: list[bool] = []

    def at(self, offset: float) -> None:
        self.clock.advance(self.t0 + offset - self.clock.now())

    def queue(
        self,
        to: str = "bob",
        *,
        source: str = "alice",
        status: Status | None = None,
        due_in: float | None = 0.0,
    ) -> str:
        """Inserts a request from `source`, QUEUED unless `status` says otherwise."""
        from mz_relay.envelope import Status

        msg_id = mid(next(_ids))
        now = self.clock.now()
        env = envelope(
            msg_id,
            source=source,
            to=to,
            at=now,
            label=self.roster.get(source).label,
            body="please review src/app.py",
        )
        due = None if due_in is None else now + due_in
        self.store.insert_message(env, status or Status.QUEUED, now, next_attempt_at=due)
        return msg_id

    def register(
        self,
        name: str,
        binding: dict[str, object] | None,
        *,
        mode: str | None = "socket",
        age_s: float = 0.0,
        token: str | None = None,
    ) -> str:
        """Writes the registration straight into the Store, bypassing register-time checks."""
        from mz_relay.store import Registration

        token = token or f"{next(self._tokens):032x}"
        created = self.clock.now() - age_s
        self.store.upsert_registration(
            Registration(
                token=token,
                name=name,
                harness=self.roster.get(name).harness,
                client="opencode",
                delivery_mode=mode,
                binding=binding,
                pid=os.getpid(),
                created_at=created,
                last_seen=created,
                online=True,
            )
        )
        return token

    def online(self, token: str) -> bool:
        reg = self.store.registration(token)
        assert reg is not None, f"registration {token} missing"
        return reg.online

    def row(self, msg_id: str, source: str = "alice") -> StoredMessage:
        got = self.store.get(source, msg_id)
        assert got is not None, f"row {source}/{msg_id} missing"
        return got

    def move(self, msg_id: str, change: StatusChange, expected: Status) -> None:
        assert self.store.update("alice", msg_id, change, self.clock.now(), expected=expected)

    def ack(self, msg_id: str, source: str = "alice") -> bool:
        from mz_relay.envelope import Status
        from mz_relay.store import StatusChange

        ackable = frozenset({Status.DELIVERING, Status.HANDED_OFF, Status.DELIVERED, Status.LEASED})
        change = StatusChange(status=Status.ACKED)
        return self.store.update(source, msg_id, change, self.clock.now(), expected=ackable)

    def ack_job(self, job: DeliveryJob) -> None:
        # Plays the recipient's ack op landing while the adapter is still delivering.
        self.acked_during_delivery.append(self.ack(job.message.envelope.id))

    def handed_off(self) -> str:
        """A row the adapter handed off at t0; its first confirm is due at t0 + 1."""
        from mz_relay.envelope import Status
        from mz_relay.roster import Harness

        msg_id = self.queue()
        self.engine.run_once(Harness.OPENCODE)
        assert self.row(msg_id).status == Status.HANDED_OFF
        self.at(1)
        return msg_id


MakeRig = Callable[..., EngineRig]


class ServiceRig:
    """A RelayService over a Store, a roster file, a FakeClock and a FakeAdapter for opencode.

    Calls go straight to `dispatch`, so tests run without sockets or a daemon.
    """

    store: Store
    roster: RosterCache
    service: RelayService
    paths: Paths

    def __init__(
        self,
        home: Path,
        *,
        extra_agents: str = "",
        adapter: FakeAdapter | None = None,
        extensions: Sequence[Extension] = (),
    ) -> None:
        from mz_relay import __version__
        from mz_relay.extensions import Extensions
        from mz_relay.roster import Harness, RosterCache
        from mz_relay.service import RelayService, ServiceDeps
        from mz_relay.store import Store

        self.home = home
        self.paths = paths_in(home)
        self.paths.ensure_dirs()
        self.roster_path = self.paths.roster
        self.roster_path.write_text(pull_roster(home) + extra_agents, encoding="utf-8")
        self.clock = FakeClock()
        self.adapter = adapter if adapter is not None else FakeAdapter()
        self.roster = RosterCache(self.paths.roster, home)
        self.changed = threading.Condition()
        self.store = Store(self.paths.db)
        self.extensions = Extensions(extensions, self.paths.state_dir / "ext")
        try:
            self.service = RelayService(
                ServiceDeps(
                    store=self.store,
                    roster=self.roster,
                    clock=self.clock,
                    adapters={Harness.OPENCODE: self.adapter},
                    paths=self.paths,
                    changed=self.changed,
                    version=__version__,
                    extensions=self.extensions,
                )
            )
        except BaseException:
            self.extensions.close()
            self.store.close()
            raise
        self._conn_ids = itertools.count(1)
        self._rids = itertools.count(1)

    def new_conn(self) -> ConnState:
        from mz_relay.service import ConnState

        return ConnState(conn_id=next(self._conn_ids), peer_uid=os.getuid())

    def call(self, conn: ConnState, op: str, /, **args: object) -> dict[str, Any]:
        from mz_relay.protocol import Request

        return self.service.dispatch(conn, Request(rid=next(self._rids), op=op, args=args))

    def hello(self, *, client: str = "mcp") -> ConnState:
        """A new connection that has said hello."""
        from mz_relay.protocol import PROTOCOL_VERSION

        conn = self.new_conn()
        self.call(conn, "hello", protocol=PROTOCOL_VERSION, client=client, pid=os.getpid())
        return conn

    def register(self, conn: ConnState, name: str | None, **extra: object) -> dict[str, Any]:
        """Registers an opencode agent from this process; `extra` overrides any argument."""
        args: dict[str, object] = {
            "name": name,
            "harness": "opencode",
            "cwd": str(self.home),
            "pid": os.getpid(),
        }
        args.update(extra)
        return self.call(conn, "register", **args)

    def connect(
        self, name: str, delivery: Mapping[str, object] | None = None
    ) -> tuple[ConnState, str]:
        """Opens a connection for roster agent `name`: hello, then register."""
        conn = self.hello()
        spec = self.roster.current().get(name)
        extra: dict[str, object] = {"harness": str(spec.harness)}
        if spec.cwd is not None:
            extra["cwd"] = str(spec.cwd)
        if delivery is not None:
            extra["delivery"] = dict(delivery)
        issued = self.register(conn, name, **extra)["token"]
        assert isinstance(issued, str), f"register returned token {issued!r}"
        return conn, issued

    def send(
        self,
        conn: ConnState,
        token: str,
        to: str,
        body: str = "please review src/app.py",
        **extra: object,
    ) -> dict[str, Any]:
        """Sends a request under a fresh id unless `extra` names the id or kind."""
        from mz_relay.ids import new_id

        args: dict[str, object] = {"token": token, "id": new_id(), "to": to, "kind": "request"}
        args.update(body=body, **extra)
        return self.call(conn, "send", **args)

    def write_roster(self, text: str) -> None:
        """Replaces the roster file and moves its mtime forward, so the cache sees a change."""
        later = self.roster_path.stat().st_mtime_ns + 1_000_000_000
        self.roster_path.write_text(text, encoding="utf-8")
        os.utime(self.roster_path, ns=(later, later))

    def close(self) -> None:
        self.extensions.close()
        self.store.close()

    def __enter__(self) -> ServiceRig:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
