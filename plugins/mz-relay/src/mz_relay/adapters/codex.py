"""Codex delivery: `codex queue` by thread id, confirmed by the id line in its rollout."""

from __future__ import annotations

import logging
import math
import os
import re
import signal
import subprocess
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mz_relay.adapters.codex_threads import (
    CodexThreadMissing,
    find_rollout,
    resolve_thread,
    scan_rollout,
)
from mz_relay.delivery import DeliveryBinding, DeliveryJob, DeliveryResult, Outcome
from mz_relay.envelope import Status, header_nonce
from mz_relay.errors import BindingError
from mz_relay.roster import AgentSpec, Harness

log = logging.getLogger(__name__)

QUEUED_RE = re.compile(r"^Queued message (\S+) for thread (\S+)", re.MULTILINE)
CODEX = "codex"
# Stderr is shown in reasons; a runaway one is cut to its tail.
_DETAIL_MAX = 200
# Rows that end by TTL never reach the adapter, so the oldest watches are dropped past this.
_WATCH_MAX = 1024

Runner = Callable[[Sequence[str], Mapping[str, str], float], subprocess.CompletedProcess[str]]


def _run(
    argv: Sequence[str], env: Mapping[str, str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    # Its own session lets a timeout kill what codex started too, e.g. a node wrapper's binary.
    with subprocess.Popen(
        list(argv),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except BaseException:
            # Any interruption, not only the timeout, must not leave codex's group running.
            _kill_group(proc.pid)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        log.debug("codex process group %d was already gone", pid)


@dataclass(frozen=True)
class _Watch:
    """Where confirm resumes reading a handed-off row's rollout; None when it had none yet."""

    rollout: Path | None
    offset: int


@dataclass(frozen=True)
class _Target:
    """The roster's Codex binding: which account, which thread name, which codex."""

    codex_home: Path
    codex_thread: str
    codex_bin: str


class CodexAdapter:
    """Queues messages into a running Codex TUI thread; the rollout shows when the turn took it."""

    exhausted_status: Status = Status.UNDELIVERABLE
    via: str = CODEX

    def __init__(
        self,
        *,
        pw_home: Path,
        base_env: Mapping[str, str],
        run: Runner = _run,
        timeout_s: float = 30.0,
    ) -> None:
        if not isinstance(pw_home, Path) or not pw_home.is_absolute():
            raise ValueError(f"pw_home must be an absolute Path, got {pw_home!r}")
        if not isinstance(base_env, Mapping):
            raise ValueError(f"base_env must be a mapping, got {type(base_env).__name__}")
        if not callable(run):
            raise ValueError(f"run must be callable, got {type(run).__name__}")
        if not (math.isfinite(timeout_s) and timeout_s > 0):
            raise ValueError(f"timeout_s must be a positive number, got {timeout_s!r}")
        # base_env is the launcher's allowlisted daemon env; only Claude leftovers are dropped.
        self._env = {k: v for k, v in base_env.items() if not k.startswith("CLAUDE")}
        self._pw_home = pw_home
        self._run = run
        self._timeout_s = timeout_s
        self._watched: OrderedDict[tuple[str, str], _Watch] = OrderedDict()

    @property
    def watched_rows(self) -> int:
        return len(self._watched)

    def validate_binding(
        self, spec: AgentSpec, binding: Mapping[str, object] | None
    ) -> DeliveryBinding:
        """The roster's binding; a client never chooses where Codex messages go."""
        if spec.harness is not Harness.CODEX:
            raise BindingError(f"{spec.name} is a {spec.harness} agent, not a codex one")
        if spec.codex_home is None or not spec.codex_thread:
            raise BindingError(f"{spec.name} needs codex_home and codex_thread in the roster")
        data = {
            "codex_home": str(spec.codex_home),
            "codex_thread": spec.codex_thread,
            "codex_bin": spec.codex_bin,
        }
        return DeliveryBinding(CODEX, data)

    def deliver(self, job: DeliveryJob) -> DeliveryResult:
        env, target = job.message.envelope, _target(job.binding)
        key = (env.source, env.id)
        self._watched.pop(key, None)
        if "\0" in job.text:
            # An argv entry cannot hold NUL, and the OS error would say nothing useful.
            log.info("%s to %s not confirmed: the text holds a NUL byte", env.id, env.to)
            return DeliveryResult(Outcome.NOT_CONFIRMED, "message text holds a NUL byte")
        try:
            thread = resolve_thread(target.codex_home, target.codex_thread)
        except CodexThreadMissing as exc:
            log.info("%s to %s not confirmed: %s", env.id, env.to, exc)
            return DeliveryResult(Outcome.NOT_CONFIRMED, str(exc))
        # Taken before queuing, so confirm never rereads the history before this message.
        watch = _rollout_end(target.codex_home, thread.id)
        result = self._queue(target, thread.id, job.text)
        if result.outcome is Outcome.NOT_CONFIRMED:
            log.info("%s to %s not confirmed: %s", env.id, env.to, result.detail)
        else:
            log.info("%s to %s queued on Codex thread %s", env.id, env.to, thread.id)
            self._watch(key, watch)
        return result

    def confirm(self, job: DeliveryJob) -> DeliveryResult:
        env, target = job.message.envelope, _target(job.binding)
        try:
            thread = resolve_thread(target.codex_home, target.codex_thread)
        except CodexThreadMissing as exc:
            # The thread may be renamed back; the TTL ends the row otherwise.
            log.debug("%s to %s still handed off: %s", env.id, env.to, exc)
            return DeliveryResult(Outcome.HANDED_OFF)
        rollout = find_rollout(target.codex_home, thread.id)
        if rollout is None:
            return DeliveryResult(Outcome.HANDED_OFF)
        key = (env.source, env.id)
        watch = self._watched.get(key)
        # No watch (a restart) or a new rollout (rotation) is read from its start.
        offset = watch.offset if watch is not None and watch.rollout == rollout else 0
        scan = scan_rollout(rollout, header_nonce(env), offset)
        if scan.found:
            self._watched.pop(key, None)
            log.info("%s to %s seen in Codex rollout %s", env.id, env.to, rollout)
            return DeliveryResult(Outcome.CONFIRMED)
        self._watch(key, _Watch(rollout, scan.offset))
        return DeliveryResult(Outcome.HANDED_OFF)

    def _watch(self, key: tuple[str, str], watch: _Watch) -> None:
        # Updating a key keeps its place, so eviction drops the longest-watched rows first.
        self._watched[key] = watch
        while len(self._watched) > _WATCH_MAX:
            (source, msg_id), _ = self._watched.popitem(last=False)
            log.debug("dropped the rollout offset of %s from %s", msg_id, source)

    def _queue(self, target: _Target, thread_id: str, text: str) -> DeliveryResult:
        # argv carries the message, so neither it nor an exception holding it is logged.
        argv = [target.codex_bin, "queue", "--thread", thread_id, "--message", text]
        env = {**self._env, "HOME": str(self._pw_home), "CODEX_HOME": str(target.codex_home)}
        try:
            done = self._run(argv, env, self._timeout_s)
        except FileNotFoundError:
            return DeliveryResult(Outcome.NOT_CONFIRMED, f"codex not found: {target.codex_bin}")
        except subprocess.TimeoutExpired:
            detail = f"codex queue timed out after {self._timeout_s:g} s"
            return DeliveryResult(Outcome.NOT_CONFIRMED, detail)
        except OSError as exc:
            detail = f"codex could not run: {target.codex_bin}: {exc.strerror}"
            return DeliveryResult(Outcome.NOT_CONFIRMED, detail)
        if done.returncode == 0:
            if QUEUED_RE.search(done.stdout or "") is None:
                return DeliveryResult(Outcome.NOT_CONFIRMED, "unexpected codex output")
            return DeliveryResult(Outcome.HANDED_OFF)
        prefix = f"codex exited with {done.returncode}"
        tail = (done.stderr or "").strip()[-(_DETAIL_MAX - len(prefix) - 2) :]
        return DeliveryResult(Outcome.NOT_CONFIRMED, f"{prefix}: {tail}" if tail else prefix)


def _rollout_end(codex_home: Path, thread_id: str) -> _Watch:
    rollout = find_rollout(codex_home, thread_id)
    if rollout is None:
        return _Watch(None, 0)
    try:
        return _Watch(rollout, rollout.stat().st_size)
    except FileNotFoundError:
        return _Watch(None, 0)


def _target(binding: DeliveryBinding) -> _Target:
    home, thread = binding.data.get("codex_home"), binding.data.get("codex_thread")
    codex_bin = binding.data.get("codex_bin")
    if not (isinstance(home, str) and isinstance(thread, str) and isinstance(codex_bin, str)):
        raise BindingError("codex delivery needs codex_home, codex_thread and codex_bin strings")
    return _Target(codex_home=Path(home), codex_thread=thread, codex_bin=codex_bin)
