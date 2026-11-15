"""`claude -p` in stream-json mode with this plugin loaded, for the live tests."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from relay_rig import PLUGIN, Message, reap


class StreamJsonClaude:
    """`claude -p` in stream-json mode with this plugin loaded; prompts go in on stdin."""

    def __init__(
        self, claude: str, env: Mapping[str, str], *, cwd: Path, log: Path, extra: list[str]
    ) -> None:
        self.events: list[Message] = []
        # results() re-enters the lock from inside wait_results(), so it must be reentrant.
        self._changed = threading.Condition(threading.RLock())
        self._log = log.open("wb")
        # Claude's own debug log: the only record of where a session stalls before its init event.
        self.debug_log = log.with_suffix(".debug")
        config_dir = env.get("CLAUDE_CONFIG_DIR")
        self._projects = Path(config_dir) / "projects" if config_dir else None
        argv = [
            claude,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--plugin-dir",
            str(PLUGIN),
            "--debug-file",
            str(self.debug_log),
            *extra,
        ]
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                env=dict(env),
                cwd=cwd,
            )
        except BaseException:
            self._log.close()
            raise
        threading.Thread(target=self._read, args=(self.proc.stdout,), daemon=True).start()

    def _read(self, stream: IO[bytes] | None) -> None:
        assert stream is not None
        for raw in iter(stream.readline, b""):
            with contextlib.suppress(ValueError):
                event = json.loads(raw)
                if isinstance(event, dict):
                    with self._changed:
                        self.events.append(event)
                        self._changed.notify_all()

    def prompt(self, text: str) -> None:
        assert self.proc.stdin is not None
        line = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(line).encode() + b"\n")
        self.proc.stdin.flush()

    def results(self) -> int:
        with self._changed:
            return sum(1 for event in self.events if event.get("type") == "result")

    def wait_results(self, count: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._changed:
            while self.results() < count:
                left = deadline - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return False
                self._changed.wait(min(left, 1.0))
            return True

    def session_id(self) -> str | None:
        with self._changed:
            for event in self.events:
                if event.get("type") == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    return sid if isinstance(sid, str) else None
        return None

    def saw(self, text: str) -> bool:
        """True when any stream-json event so far contains `text`."""
        with self._changed:
            return any(text in json.dumps(event) for event in self.events)

    def diagnose(self, events: int = 8, debug_lines: int = 15) -> str:
        """Exit code, the last stream events, the stderr tail and the debug log tail."""
        with self._changed:
            tail = [f"{e.get('type')}/{e.get('subtype', '')}" for e in self.events[-events:]]
            count = len(self.events)
        self._log.flush()
        stderr = Path(self._log.name).read_text(encoding="utf-8", errors="replace")[-2000:]
        try:
            debug = self.debug_log.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            debug = ""
        debug_tail = "\n".join(debug.splitlines()[-debug_lines:])
        return (
            f"exit={self.proc.poll()} events={count} last={tail}\n"
            f"stderr tail:\n{stderr or '(empty)'}\n"
            f"debug log {self.debug_log} tail:\n{debug_tail or '(empty)'}"
        )

    def close(self) -> None:
        try:
            reap(self.proc)
            self._forget_project()
        finally:
            self._log.close()

    def _forget_project(self) -> None:
        """Deletes this session's project folder, so live runs leave no transcripts behind."""
        sid = self.session_id()
        if sid is None or self._projects is None:
            return
        for transcript in self._projects.glob(f"*/{sid}.jsonl"):
            # Only the folder Claude names after a /tmp/mzr-* working directory, never a real one.
            if transcript.parent.name.startswith("-tmp-mzr-"):
                shutil.rmtree(transcript.parent)
