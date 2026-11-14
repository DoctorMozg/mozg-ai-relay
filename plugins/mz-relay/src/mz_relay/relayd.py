"""relayd: the broker process serving the wire protocol, delivery workers and the sweep thread."""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import logging
import math
import os
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import FrameType

from mz_relay import __version__
from mz_relay.adapters import AdapterContext, build_adapters
from mz_relay.clock import Clock, SystemClock
from mz_relay.delivery import DeliveryEngine, EngineDeps, HarnessAdapter
from mz_relay.errors import PathError, ProtocolError, RelayError, RosterError, StoreError
from mz_relay.extensions import EVENT_WAIT_S, Extensions, open_extensions
from mz_relay.paths import Paths
from mz_relay.protocol import LineReader, Response, decode_request, encode
from mz_relay.roster import Harness, RosterCache
from mz_relay.service import ConnState, RelayService, ServiceDeps
from mz_relay.store import Store

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG = 2
DEFAULT_IDLE_EXIT_S = 600.0
SOCKET_MODE = 0o600
LOCK_MODE = 0o600
POLL_S = 0.1
MIN_WAIT_S = 0.05
MAX_WAIT_S = 1.0
# Bounded so a stuck `wait` op or adapter call cannot keep a stopping daemon alive.
DRAIN_S = 2.0
JOIN_S = 5.0
_PEERCRED = struct.Struct("iII")
LOG_FORMAT = "%(asctime)s relayd[%(process)d] %(levelname)s %(name)s: %(message)s"


def worker_timeout(next_wakeup: float | None, now: float) -> float:
    """Seconds a delivery worker waits: the gap to its next due row, clamped to 50 ms..1 s."""
    if not math.isfinite(now):
        raise ValueError(f"now must be a finite number, got {now!r}")
    if next_wakeup is None:
        return MAX_WAIT_S
    return min(MAX_WAIT_S, max(MIN_WAIT_S, next_wakeup - now))


@dataclass(frozen=True)
class DaemonConfig:
    paths: Paths
    clock: Clock
    idle_exit_s: float = DEFAULT_IDLE_EXIT_S
    sweep_s: float = 5.0
    version: str = __version__

    def __post_init__(self) -> None:
        if not self.idle_exit_s > 0:
            raise ValueError(f"idle_exit_s must be positive, got {self.idle_exit_s}")
        if not self.sweep_s > 0:
            raise ValueError(f"sweep_s must be positive, got {self.sweep_s}")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    # Connection threads are woken and drained by RelayDaemon, with a bound, not joined here.
    block_on_close = False

    def __init__(self, cfg: DaemonConfig, on_connection: Callable[[socket.socket], None]) -> None:
        self._socket_path = cfg.paths.socket
        self.on_connection = on_connection
        super().__init__(str(self._socket_path), _Handler)

    def server_bind(self) -> None:
        super().server_bind()
        os.chmod(self._socket_path, SOCKET_MODE)

    def handle_error(self, request: object, client_address: object) -> None:
        log.exception("connection handler failed")


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _Server) or not isinstance(self.request, socket.socket):
            raise TypeError("relayd handler runs only under its own Unix stream server")
        server.on_connection(self.request)


class RelayDaemon:
    """One relayd: holds relayd.lock, serves relayd.sock and runs the background threads."""

    def __init__(self, cfg: DaemonConfig, adapters: Mapping[Harness, HarnessAdapter]) -> None:
        if not isinstance(cfg, DaemonConfig):
            raise ValueError(f"cfg must be a DaemonConfig, got {type(cfg).__name__}")
        for harness in adapters:
            if not isinstance(harness, Harness):
                raise ValueError(f"adapter keys must be Harness values, got {harness!r}")
        self._cfg = cfg
        self._paths = cfg.paths
        self._clock = cfg.clock
        self._adapters = dict(adapters)
        self._stop = threading.Event()
        self._signalled: int | None = None
        self._changed = threading.Condition()
        self._conn_ids = itertools.count(1)
        self._conn_lock = threading.Lock()
        self._open: set[socket.socket] = set()
        self._idle_since = cfg.clock.now()
        self._drained = threading.Condition(self._conn_lock)
        self.worker_passes: dict[Harness, int] = dict.fromkeys(self._adapters, 0)

    def serve_forever(self) -> int:
        """Serves until stopped; 0 on a clean stop or when another relayd already serves."""
        try:
            self._paths.ensure_dirs()
        except PathError as exc:
            log.error("relayd cannot start: %s", exc.message)
            return EXIT_CONFIG
        lock_fd = os.open(self._paths.lock, os.O_RDWR | os.O_CREAT, LOCK_MODE)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.info("another relayd holds %s; exiting", self._paths.lock)
                return EXIT_OK
            return self._serve_locked()
        finally:
            # Closing the descriptor releases the flock, after the socket is gone.
            os.close(lock_fd)

    def request_stop(self) -> None:
        self._stop.set()
        self._notify()

    def _serve_locked(self) -> int:
        try:
            if not self._socket_path_free():
                return EXIT_OK
            roster = RosterCache(self._paths.roster, self._paths.pw_home)
            store = Store(self._paths.db)
        except (PathError, RosterError, StoreError) as exc:
            log.error("relayd cannot start: %s", exc.message)
            return EXIT_CONFIG
        try:
            extensions = open_extensions(roster.current().extensions, self._paths.state_dir)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            store.close()
            log.error("relayd cannot start its extensions: %s", exc)
            return EXIT_CONFIG
        try:
            return self._run(roster, store, extensions)
        finally:
            extensions.close()
            store.close()

    def _socket_path_free(self) -> bool:
        """True when the path is free or a dead socket; False when a relayd answers there."""
        path = self._paths.socket
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return True
        if not stat.S_ISSOCK(info.st_mode):
            raise PathError(f"{path} exists and is not a socket; move it away and start again")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            log.info("removing stale socket %s", path)
            path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            raise PathError(f"cannot probe socket {path}: {exc.strerror or exc}") from exc
        finally:
            probe.close()
        log.warning("a relayd without %s already serves %s; exiting", self._paths.lock, path)
        return False

    def _run(self, roster: RosterCache, store: Store, extensions: Extensions) -> int:
        self._service = RelayService(
            ServiceDeps(
                store=store,
                roster=roster,
                clock=self._clock,
                adapters=self._adapters,
                paths=self._paths,
                changed=self._changed,
                version=self._cfg.version,
                extensions=extensions,
            )
        )
        self._store, self._extensions = store, extensions
        self._engine = DeliveryEngine(
            EngineDeps(
                store=store, roster=roster.current, adapters=self._adapters, clock=self._clock
            )
        )
        store.mark_all_offline()
        requeued = self._engine.recover()
        if requeued:
            log.info("requeued %d messages a previous relayd left in delivery", requeued)
        # Handlers go in before the socket exists: a client may connect and send SIGTERM at once.
        with self._signals_stop():
            server = _Server(self._cfg, self._serve_connection)
            threads = self._background_threads(server)
            try:
                for thread in threads:
                    thread.start()
                log.info("relayd pid %d serving %s", os.getpid(), self._paths.socket)
                self._wait_for_stop()
            finally:
                self._shut_down(server, threads)
        return EXIT_OK

    def _background_threads(self, server: _Server) -> list[threading.Thread]:
        accept = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": POLL_S}, name="relayd-accept"
        )
        workers = [
            threading.Thread(target=self._work, args=(harness,), name=f"relayd-worker-{harness}")
            for harness in self._adapters
        ]
        sweep = threading.Thread(target=self._sweep, name="relayd-sweep")
        pumps = []
        if self._extensions.names():
            pumps.append(threading.Thread(target=self._pump, name="relayd-extensions"))
        return [accept, *workers, sweep, *pumps]

    @contextlib.contextmanager
    def _signals_stop(self) -> Iterator[None]:
        # Python only installs handlers from the main thread; in-process tests use request_stop.
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {
            sig: signal.signal(sig, self._on_signal) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        # A handler that took a lock could deadlock the main thread, so it only records.
        self._signalled = signum

    def _wait_for_stop(self) -> None:
        while not self._stop.wait(POLL_S):
            if self._signalled is not None:
                log.info("stopping on signal %s", signal.Signals(self._signalled).name)
                return
            if self._service.stop_requested.is_set():
                log.info("stopping on the shutdown op")
                return
            idle_for = self._idle_for()
            if idle_for >= self._cfg.idle_exit_s:
                log.info("no connections for %.0f s; exiting", idle_for)
                return
        log.info("stop requested")

    def _idle_for(self) -> float:
        with self._conn_lock:
            if self._open:
                return 0.0
            return self._clock.now() - self._idle_since

    def _shut_down(self, server: _Server, threads: list[threading.Thread]) -> None:
        self._stop.set()
        # Parked `wait` ops see this and return before the drain ends and the store closes.
        self._service.stop_requested.set()
        self._notify()
        server.shutdown()
        server.server_close()
        self._paths.socket.unlink(missing_ok=True)
        with self._conn_lock:
            # SHUT_RD ends each reader's recv while a reply already being written still goes out.
            for sock in self._open:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RD)
            if not self._drained.wait_for(lambda: not self._open, DRAIN_S):
                log.warning("%d connections still open after %.0f s", len(self._open), DRAIN_S)
        for thread in threads:
            thread.join(JOIN_S)
            if thread.is_alive():
                log.warning("%s did not stop within %.0f s", thread.name, JOIN_S)
        log.info("relayd pid %d stopped", os.getpid())

    def _serve_connection(self, sock: socket.socket) -> None:
        uid = _peer_uid(sock)
        if uid != os.getuid():
            log.warning("refused a connection from uid %d", uid)
            return
        conn = ConnState(conn_id=next(self._conn_ids), peer_uid=uid)
        with self._conn_lock:
            self._open.add(sock)
        try:
            self._converse(conn, sock)
        finally:
            self._service.connection_closed(conn)
            with self._conn_lock:
                self._open.discard(sock)
                if not self._open:
                    self._idle_since = self._clock.now()
                    self._drained.notify_all()

    def _converse(self, conn: ConnState, sock: socket.socket) -> None:
        reader = LineReader(sock)
        try:
            while True:
                try:
                    line = reader.next_line()
                except ProtocolError as exc:
                    log.warning("connection %d: %s; closing", conn.conn_id, exc.message)
                    sock.sendall(encode(Response(rid=None, ok=False, error=exc.to_wire())))
                    return
                if line is None:
                    return
                sock.sendall(encode(self._respond(conn, line)))
        except OSError as exc:
            log.info("connection %d ended: %s", conn.conn_id, exc)

    def _respond(self, conn: ConnState, line: bytes) -> Response:
        try:
            req = decode_request(line)
        except ProtocolError as exc:
            return Response(rid=None, ok=False, error=exc.to_wire())
        try:
            result = self._service.dispatch(conn, req)
        except RelayError as exc:
            return Response(rid=req.rid, ok=False, error=exc.to_wire())
        except Exception as exc:
            log.exception("op %s on connection %d raised", req.op, conn.conn_id)
            message = f"{req.op} failed with {type(exc).__name__}; see {self._paths.log}"
            return Response(rid=req.rid, ok=False, error={"code": "internal", "message": message})
        return Response(rid=req.rid, ok=True, result=result)

    def _work(self, harness: Harness) -> None:
        while not self._stop.is_set():
            try:
                self._engine.run_once(harness)
            except Exception:
                log.exception("delivery pass for %s failed", harness)
            self.worker_passes[harness] += 1
            # Computing the wait under the condition means a notify after it is never lost.
            with self._changed:
                if self._stop.is_set():
                    return
                self._changed.wait(self._worker_wait(harness))

    def _worker_wait(self, harness: Harness) -> float:
        try:
            next_wakeup = self._engine.next_wakeup(harness)
        except Exception:
            log.exception("next wakeup for %s failed", harness)
            return MAX_WAIT_S
        return worker_timeout(next_wakeup, self._clock.now())

    def _sweep(self) -> None:
        while not self._stop.wait(self._cfg.sweep_s):
            try:
                swept = self._engine.sweep()
            except Exception:
                log.exception("sweep failed")
                continue
            if swept:
                self._notify()

    def _pump(self) -> None:
        """Hands committed events to the extensions, after the write that made them."""
        while not self._stop.is_set():
            generation = self._store.generation()
            try:
                handed = self._extensions.pump(self._store, self._clock.now())
            except Exception:
                log.exception("extension event pass failed")
                handed = 0
            if not handed:
                self._store.wait_commit(generation, EVENT_WAIT_S)

    def _notify(self) -> None:
        with self._changed:
            self._changed.notify_all()


def _peer_uid(sock: socket.socket) -> int:
    _pid, uid, _gid = _PEERCRED.unpack(
        sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
    )
    return int(uid)


def run_daemon(paths: Paths, idle_exit_s: float = DEFAULT_IDLE_EXIT_S) -> int:
    """Runs relayd in this process with the harness adapters, logging to stderr."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
    clock = SystemClock()
    ctx = AdapterContext(paths=paths, clock=clock, pw_home=paths.pw_home, base_env=dict(os.environ))
    cfg = DaemonConfig(paths=paths, clock=clock, idle_exit_s=idle_exit_s)
    return RelayDaemon(cfg, build_adapters(ctx)).serve_forever()
