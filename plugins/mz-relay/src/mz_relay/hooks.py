"""Claude Code hooks: `hook wait`, the asyncRewake waiter, and `hook gate`, the read-only gate."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from mz_relay.adapters.claude_registry import (
    ClaudeRegistry,
    RegistryEntry,
    claude_config_dir,
    pid_alive,
)
from mz_relay.adapters.claude_transcript import Transcript, TranscriptWatcher
from mz_relay.client import RelayClient
from mz_relay.clock import Clock, SystemClock
from mz_relay.errors import (
    BindingError,
    EnvelopeError,
    IdentityError,
    PathError,
    ProtocolError,
    RelayError,
    RosterError,
)
from mz_relay.launcher import DaemonLauncher
from mz_relay.paths import PRIVATE_MODE, Paths, user_home
from mz_relay.presence import DEFAULT_BACKOFF_S, RegisterArgs, Registered, configured_name
from mz_relay.roster import Harness, RosterCache, ToolMode

log = logging.getLogger(__name__)

WaitEvent = Literal["SessionStart", "Stop"]
WAIT_EVENTS: tuple[WaitEvent, ...] = ("SessionStart", "Stop")
# One relayd wait per second, so the session check below runs every second too.
WAIT_SLICE_S = 1
# A registry rewrite caught mid-replace reads as missing once; two misses in a row mean gone.
MAX_SESSION_MISSES = 2
# relayd refuses these for good, so retrying the registration cannot help.
REFUSED_CODES = IdentityError.CODES | {BindingError.CODE}
EXIT_QUIET = 0
# asyncRewake: exit 2 wakes Claude with stderr as the prompt.
EXIT_WAKE = 2
USAGE = "usage: mz-relay hook wait <SessionStart|Stop> | mz-relay hook gate"
LOG_FORMAT = "%(asctime)s mz-relay-hook[%(process)d] %(levelname)s %(name)s: %(message)s"


@dataclass(frozen=True)
class HookInput:
    session_id: str
    cwd: Path
    transcript_path: Path | None
    tool_name: str | None
    event: str
    source: str | None
    # The turn's id; Claude Code sends it to every hook from about 2.1.3xx on.
    prompt_id: str | None = None


def parse_hook_input(raw: str) -> HookInput:
    """The hook JSON Claude writes to stdin; the session id names run files, so it is checked."""
    if not isinstance(raw, str):
        raise EnvelopeError(f"hook input must be text, got {type(raw).__name__}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"hook input is not JSON: {exc.msg} at char {exc.pos}") from exc
    if not isinstance(data, dict):
        raise EnvelopeError(f"hook input must be a JSON object, got {type(data).__name__}")
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _plain_name(session_id):
        raise EnvelopeError(f"hook input session_id must be a plain file name, got {session_id!r}")
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise EnvelopeError(f"hook input cwd must be an absolute path, got {cwd!r}")
    event = data.get("hook_event_name")
    if not isinstance(event, str) or not event:
        raise EnvelopeError(f"hook input hook_event_name must be a string, got {event!r}")
    transcript = _optional_text(data, "transcript_path")
    return HookInput(
        session_id=session_id,
        cwd=Path(cwd),
        transcript_path=None if transcript is None else Path(transcript),
        tool_name=_optional_text(data, "tool_name"),
        event=event,
        source=_optional_text(data, "source"),
        prompt_id=_optional_text(data, "prompt_id"),
    )


def _plain_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\0" not in name and name not in {".", ".."}


def _optional_text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"hook input {key} must be a non-empty string, got {value!r}")
    return value


def _file_size(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def read_offset(path: Path) -> int | None:
    """A turn-end or marker offset file; None when it does not exist or is corrupt."""
    try:
        raw = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raw = "<not ascii>"
    if raw.isdigit():
        return int(raw)
    # Dropped so a corrupt file costs one warning, not one traceback per tool call.
    log.warning("dropping %s: it must hold a byte offset, got %r", path, raw[:20])
    path.unlink(missing_ok=True)
    return None


def _write_offset(path: Path, offset: int) -> None:
    """Replaces `path` with the offset as ASCII digits, so the gate never reads half a number."""
    path.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    scratch.write_text(str(offset), encoding="ascii")
    os.replace(scratch, path)


@dataclass
class _Link:
    """A relayd connection that holds the waiter's rewake-only registration."""

    client: RelayClient
    token: str
    read_only: bool


@dataclass
class _Run:
    """One waiter's state: whom it registers as, its relayd link and its retry counters."""

    hook: HookInput
    register: RegisterArgs
    config_dir: Path
    registry: ClaudeRegistry
    stderr: TextIO
    link: _Link | None = None
    pid: int | None = None
    misses: int = 0
    failures: int = 0
    retry_at: float = 0.0


class Waiter:
    """Waits for FALLBACK messages of one Claude session and hands one over by exiting 2."""

    def __init__(
        self, paths: Paths, launcher: DaemonLauncher, *, env: Mapping[str, str], clock: Clock
    ) -> None:
        self._paths = paths
        self._launcher = launcher
        self._env = dict(env)
        self._clock = clock

    def run(self, event: WaitEvent, hook: HookInput, stderr: TextIO) -> int:
        """Exit 2 with the message on stderr wakes Claude; 0 ends quietly."""
        if event not in WAIT_EVENTS:
            raise ValueError(f"event must be one of {', '.join(WAIT_EVENTS)}, got {event!r}")
        if event == "Stop":
            # Before the lock: the SessionStart waiter usually holds it for the whole session.
            self._record_turn_end(hook)
        lock = self._try_lock(hook.session_id)
        if lock is None:
            log.info("%s waiter for session %s: another waiter is running", event, hook.session_id)
            return EXIT_QUIET
        try:
            return self._serve(hook, stderr)
        finally:
            os.close(lock)

    def _record_turn_end(self, hook: HookInput) -> None:
        """Reads both from the transcript, as an async Stop may run after the next prompt."""
        sid = hook.session_id
        marker = self._paths.marker_dir / sid
        if hook.transcript_path is None or not hook.transcript_path.is_file():
            marker.unlink(missing_ok=True)
            return
        transcript = Transcript(hook.transcript_path)
        end = transcript.turn_end(hook.prompt_id)
        if end is None:
            log.debug("no turn has ended yet in the transcript of session %s", sid)
        else:
            _write_offset(self._paths.turn_end_dir / sid, end)
            log.info("turn end of session %s at offset %d", sid, end)
        marked = read_offset(marker)
        # No reply after the marker yet means the rewake turn has not run; it stays gated.
        if marked is not None and transcript.assistant_since(marked):
            marker.unlink(missing_ok=True)

    def _try_lock(self, session_id: str) -> int | None:
        self._paths.waiter_dir.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
        path = self._paths.waiter_dir / f"{session_id}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    def _serve(self, hook: HookInput, stderr: TextIO) -> int:
        try:
            config_dir = claude_config_dir(self._env)
        except PathError as exc:
            log.warning("waiter for session %s: %s", hook.session_id, exc.message)
            return EXIT_QUIET
        binding = {"config_dir": str(config_dir), "session_id": hook.session_id, "rewake": True}
        register = RegisterArgs(
            name=configured_name(self._env),
            harness=Harness.CLAUDE,
            cwd=hook.cwd,
            pid=os.getpid(),
            client="hook",
            config_dir=config_dir,
            delivery=binding,
        )
        run = _Run(hook, register, config_dir, ClaudeRegistry(config_dir), stderr)
        try:
            run.link = self._register(register)
        except RelayError as exc:
            level = logging.INFO if exc.code in REFUSED_CODES else logging.WARNING
            log.log(level, "waiter not registered: %s: %s", exc.code, exc.message)
            return EXIT_QUIET
        try:
            return self._loop(run)
        finally:
            if run.link is not None:
                run.link.client.close()

    def _register(self, args: RegisterArgs) -> _Link:
        client = self._launcher.connect(client="hook")
        try:
            registered = Registered.from_result(client.call("register", args.to_wire()))
        except BaseException:
            client.close()
            raise
        tools = registered.agent.get("tools")
        log.info(
            "waiter registered as %s (delivery %s, tools %s)",
            registered.agent.get("name"),
            registered.delivery_mode,
            tools,
        )
        return _Link(client, registered.token, read_only=tools == ToolMode.READ_ONLY.value)

    def _loop(self, run: _Run) -> int:
        sid = run.hook.session_id
        while True:
            entry = self._session_entry(run)
            if entry is None or not pid_alive(entry.pid):
                # No registry entry means no rewake delivery; its messages wait for inbox.
                run.misses += 1
                if run.misses >= MAX_SESSION_MISSES:
                    log.info("session %s is gone from the registry or dead; waiter exits", sid)
                    return EXIT_QUIET
                self._clock.sleep(WAIT_SLICE_S)
                continue
            run.misses = 0
            link = run.link
            if link is None:
                if not self._reconnect(run):
                    return EXIT_QUIET
                continue
            if link.read_only and entry.is_busy:
                self._clock.sleep(WAIT_SLICE_S)
                continue
            message = self._wait_once(run, link)
            if message is not None and self._hand_over(run, link, message):
                return EXIT_WAKE

    def _session_entry(self, run: _Run) -> RegistryEntry | None:
        """The session's registry entry; once its pid is known, a torn rewrite is read again."""
        sid = run.hook.session_id
        if run.pid is None:
            entry = run.registry.find_session(sid)
            if entry is not None:
                run.pid = entry.pid
            return entry
        entry = run.registry.read_settled(run.pid, clock=self._clock)
        # After /clear the same pid serves a new session, which gets its own waiter.
        return entry if entry is not None and entry.session_id == sid else None

    def _wait_once(self, run: _Run, link: _Link) -> Mapping[str, object] | None:
        args = {"token": link.token, "session_id": run.hook.session_id, "timeout_s": WAIT_SLICE_S}
        try:
            result = link.client.call("wait", args)
        except RelayError as exc:
            log.info("wait failed (%s: %s); registering again", exc.code, exc.message)
            self._drop(run)
            return None
        message = result.get("message")
        if message is None:
            return None
        if not isinstance(message, dict):
            raise ProtocolError("invalid_request", f"relayd's wait message is {type(message)}")
        return message

    def _hand_over(self, run: _Run, link: _Link, message: Mapping[str, object]) -> bool:
        """Prints a leased message and confirms it; False leaves it to the lease running out."""
        msg_id, text = _leased_parts(message)
        sid = run.hook.session_id
        if link.read_only:
            entry = self._session_entry(run)
            if entry is None or entry.is_busy:
                # A peer turn now would make the user's turn read-only; FALLBACK comes back later.
                log.info("session %s turned busy after %s was leased; not waking it", sid, msg_id)
                return False
        run.stderr.write(text if text.endswith("\n") else text + "\n")
        run.stderr.flush()
        if link.read_only:
            self._mark_peer_turn(run)
        try:
            link.client.call("confirm", {"token": link.token, "id": msg_id})
        except RelayError as exc:
            # The text is already out; relayd sees the lease lapse and offers it again.
            log.warning("confirm of %s failed: %s: %s", msg_id, exc.code, exc.message)
        log.info("waking session %s with %s", sid, msg_id)
        return True

    def _mark_peer_turn(self, run: _Run) -> None:
        sid = run.hook.session_id
        size = _file_size(run.hook.transcript_path)
        if size is None:
            size = TranscriptWatcher(run.config_dir, sid).size()
        _write_offset(self._paths.marker_dir / sid, size)

    def _drop(self, run: _Run) -> None:
        if run.link is not None:
            run.link.client.close()
        run.link = None
        run.failures = 0
        run.retry_at = self._clock.now() + DEFAULT_BACKOFF_S[0]

    def _reconnect(self, run: _Run) -> bool:
        """One step toward a new registration; False once relayd refuses this identity."""
        pending = run.retry_at - self._clock.now()
        if pending > 0:
            self._clock.sleep(min(pending, WAIT_SLICE_S))
            return True
        try:
            run.link = self._register(run.register)
        except RelayError as exc:
            if exc.code in REFUSED_CODES:
                log.info("relayd refused the waiter: %s: %s", exc.code, exc.message)
                return False
            run.failures += 1
            delay = DEFAULT_BACKOFF_S[min(run.failures, len(DEFAULT_BACKOFF_S) - 1)]
            run.retry_at = self._clock.now() + delay
            log.warning(
                "re-registration attempt %d failed: %s: %s", run.failures, exc.code, exc.message
            )
            return True
        log.info("waiter of session %s registered again", run.hook.session_id)
        return True


def _leased_parts(message: Mapping[str, object]) -> tuple[str, str]:
    envelope = message.get("envelope")
    msg_id = envelope.get("id") if isinstance(envelope, dict) else None
    text = message.get("text")
    if not isinstance(msg_id, str) or not msg_id or not isinstance(text, str):
        raise ProtocolError("invalid_request", "relayd's wait message lacks an envelope id or text")
    return msg_id, text


@dataclass(frozen=True)
class HookIO:
    """The streams and environment of one hook run."""

    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    env: Mapping[str, str]


@contextlib.contextmanager
def _hook_log(path: Path, level: int) -> Iterator[None]:
    """Sends mz_relay logs to hooks.log for one hook run; Claude may show a hook's stderr."""
    handler: logging.Handler
    try:
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        # A waiter's stderr becomes Claude's prompt on exit 2, so no log line may land there.
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger = logging.getLogger("mz_relay")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        handler.close()


def run_hook(
    argv: Sequence[str], stdin: TextIO, stdout: TextIO, stderr: TextIO, env: Mapping[str, str]
) -> int:
    """`mz-relay hook ...`; advisory, so every failure is logged to hooks.log and exits 0."""
    try:
        paths = Paths.from_env(env, uid=os.getuid(), pw_home=user_home())
        paths.ensure_dirs()
    except PathError as exc:
        stderr.write(f"mz-relay: {exc.code}: {exc.message}\n")
        return EXIT_QUIET
    args = list(argv)
    # The gate runs before every tool call and hooks.log is never rotated: denials and errors only.
    level = logging.WARNING if args[:1] == ["gate"] else logging.INFO
    with _hook_log(paths.hooks_log, level):
        try:
            return _dispatch(args, HookIO(stdin, stdout, stderr, env), paths)
        except EnvelopeError as exc:
            log.warning("bad hook input: %s", exc.message)
        except Exception:
            # A crashing hook must not wake Claude with a traceback on every Stop.
            log.exception("hook %s failed", " ".join(args))
    return EXIT_QUIET


def _dispatch(argv: list[str], streams: HookIO, paths: Paths) -> int:
    match argv:
        case ["wait", "SessionStart" | "Stop" as event]:
            hook = parse_hook_input(streams.stdin.read())
            clock = SystemClock()
            launcher = DaemonLauncher(paths, clock=clock, env=streams.env)
            waiter = Waiter(paths, launcher, env=streams.env, clock=clock)
            return waiter.run(event, hook, streams.stderr)
        case ["gate"]:
            return _gate(parse_hook_input(streams.stdin.read()), streams, paths)
        case _:
            log.warning("unknown hook command %r", argv)
            streams.stderr.write(f"mz-relay: {USAGE}\n")
            return EXIT_QUIET


def _gate(hook: HookInput, streams: HookIO, paths: Paths) -> int:
    """Prints the gate's deny object, or nothing to allow; it reads local files, never relayd."""
    # gate.py imports HookInput from this module, so it is imported here to avoid a cycle.
    from mz_relay.gate import Gate, always_allowed

    # The gate runs before every tool call; read tools skip parsing the roster.
    if hook.tool_name is not None and always_allowed(hook.tool_name):
        return EXIT_QUIET
    try:
        roster = RosterCache(paths.roster, paths.pw_home)
    except RosterError as exc:
        log.warning("gate allows %s: %s", hook.tool_name, exc.message)
        return EXIT_QUIET
    decision = Gate(paths, roster, env=streams.env).decide(hook)
    if decision is not None:
        streams.stdout.write(json.dumps(decision) + "\n")
        streams.stdout.flush()
    return EXIT_QUIET
