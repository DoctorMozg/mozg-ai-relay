"""Runs a harness binary in its own process group, so a timeout kills everything it started."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence

log = logging.getLogger(__name__)

Runner = Callable[[Sequence[str], Mapping[str, str], float], subprocess.CompletedProcess[str]]


def run_in_group(
    argv: Sequence[str], env: Mapping[str, str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    """Runs argv to completion; on a timeout or any interruption its whole group is killed."""
    if not argv:
        raise ValueError("argv must name a program")
    # Its own session lets a timeout kill what the binary started too, e.g. a node wrapper's.
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
            # Any interruption, not only the timeout, must not leave the group running.
            _kill_group(proc.pid)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        log.debug("process group %d was already gone", pid)
