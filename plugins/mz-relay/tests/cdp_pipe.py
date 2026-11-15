"""Headless Chrome driven over --remote-debugging-pipe, stdlib only, for the live UI test."""

from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import time
from pathlib import Path

from relay_rig import Message

CALL_S = 15.0
# Chrome reads DevTools commands on fd 3 and writes replies and events on fd 4.
CMD_FD = 3
REPLY_FD = 4


class CdpError(Exception):
    pass


class HeadlessChrome:
    """One headless Chrome with one page; `evaluate` runs JavaScript in it."""

    def __init__(self, chrome: str, profile: Path, log: Path) -> None:
        cmd_read, self._cmd = os.pipe()
        self._reply, reply_write = os.pipe()

        def wire_fds() -> None:
            # Lifted above 10 first, so placing one pipe end never overwrites the other.
            high_cmd = fcntl.fcntl(cmd_read, fcntl.F_DUPFD, 10)
            high_reply = fcntl.fcntl(reply_write, fcntl.F_DUPFD, 10)
            os.dup2(high_cmd, CMD_FD)
            os.dup2(high_reply, REPLY_FD)

        argv = [
            chrome,
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "about:blank",
        ]
        with log.open("wb") as sink:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
                # close_fds runs after preexec_fn and would close fds 3 and 4 again.
                close_fds=False,
                preexec_fn=wire_fds,
            )
        os.close(cmd_read)
        os.close(reply_write)
        self._buffer = b""
        self._next_id = 0
        self._session = self._attach()

    def _attach(self) -> str:
        targets = self.call("Target.getTargets")["targetInfos"]
        pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
        if not pages:
            raise CdpError(f"no page target among {targets}")
        target = {"targetId": pages[0]["targetId"], "flatten": True}
        attached = self.call("Target.attachToTarget", target)
        return str(attached["sessionId"])

    def call(
        self, method: str, params: Message | None = None, session: str | None = None
    ) -> Message:
        self._next_id += 1
        msg: Message = {"id": self._next_id, "method": method, "params": params or {}}
        if session is not None:
            msg["sessionId"] = session
        os.write(self._cmd, json.dumps(msg).encode() + b"\0")
        deadline = time.monotonic() + CALL_S
        while True:
            reply = self._read_one(deadline)
            if reply.get("id") != self._next_id:
                continue  # an event, or a reply to a call that already timed out
            if "error" in reply:
                raise CdpError(f"{method}: {reply['error']}")
            result = reply.get("result", {})
            assert isinstance(result, dict)
            return result

    def _read_one(self, deadline: float) -> Message:
        while b"\0" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self._reply], [], [], left)[0]:
                raise CdpError("Chrome did not answer in time")
            chunk = os.read(self._reply, 65536)
            if not chunk:
                raise CdpError(f"Chrome closed its pipe (exit {self.proc.poll()})")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\0", 1)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        return parsed

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url}, self._session)

    def history(self) -> list[Message]:
        """The page's session history entries, each with its `url`."""
        entries = self.call("Page.getNavigationHistory", session=self._session)["entries"]
        assert isinstance(entries, list)
        return entries

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}, self._session
        )
        if "exceptionDetails" in result:
            raise CdpError(f"{expression}: {result['exceptionDetails']}")
        value = result.get("result", {})
        return value.get("value") if isinstance(value, dict) else None

    def close(self) -> None:
        try:
            self.call("Browser.close")
        except (CdpError, OSError):
            self.proc.kill()
        try:
            self.proc.wait(timeout=CALL_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        os.close(self._cmd)
        os.close(self._reply)
