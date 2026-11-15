"""Live: `mz-relay ui` opens its launch page, headless Chrome signs in, and the stream survives
a relayd restart. Run by hand: uv run make live-ui"""

from __future__ import annotations

import contextlib
import os
import pty
import secrets
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from cdp_pipe import HeadlessChrome
from mz_relay.client import RelayClient
from mz_relay.ids import new_id
from relay_rig import BIN, daemon_pid, paths_in, pull_roster, stop_daemon, wait_until

START_S = 30.0
POLL_S = 0.1
STREAM = "document.querySelector('span.stream')?.textContent ?? ''"


def shows(chrome: HeadlessChrome, text: str) -> bool:
    return chrome.evaluate(f"document.body.innerText.includes({text!r})") is True


def send_as_user(socket: Path, body: str) -> None:
    relay = RelayClient(socket)
    relay.connect(client="cli")
    try:
        relay.call("send_as_user", {"id": new_id(), "to": "alice", "kind": "inform", "body": body})
    finally:
        relay.close()


@contextlib.contextmanager
def ui_under_a_tty(env: dict[str, str], err: Path) -> Iterator[subprocess.Popen[bytes]]:
    """`mz-relay ui` with a pty as stdin, which its user-command check requires."""
    leader, follower = pty.openpty()
    with err.open("wb") as sink:
        proc = subprocess.Popen(
            [str(BIN), "ui"], stdin=follower, stdout=subprocess.DEVNULL, stderr=sink, env=env
        )
    os.close(follower)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=START_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        os.close(leader)


@pytest.mark.live_ui
def test_the_ui_opens_signs_in_and_reconnects_after_a_relayd_restart(
    chrome_bin: str, relay_home: Path, relay_env: dict[str, str]
) -> None:
    nonce = secrets.token_hex(4)
    paths = paths_in(relay_home)
    paths.roster.write_text(pull_roster(relay_home), encoding="utf-8")
    opened = relay_home / "opened.txt"
    # webbrowser runs $BROWSER with the address as its one argument.
    recorder = relay_home / "browser.sh"
    recorder.write_text(f'#!/bin/sh\nprintf %s "$1" > {opened}\n', encoding="utf-8")
    recorder.chmod(0o700)
    env = {**relay_env, "BROWSER": str(recorder)}

    with contextlib.ExitStack() as stack:
        stack.callback(stop_daemon, paths.socket)
        err = relay_home / "ui.err"
        ui = stack.enter_context(ui_under_a_tty(env, err))
        assert wait_until(lambda: opened.is_file() and opened.stat().st_size > 0, START_S), (
            f"the browser was never opened; ui exit {ui.poll()}: {err.read_text()}"
        )
        launch = urlparse(opened.read_text(encoding="utf-8"))
        assert launch.scheme == "file"
        assert "token" not in launch.geturl(), "the token must stay out of the browser's argv"
        assert Path(launch.path).stat().st_mode & 0o777 == 0o600

        chrome = HeadlessChrome(chrome_bin, relay_home / "profile", relay_home / "chrome.log")
        stack.callback(chrome.close)
        chrome.navigate(launch.geturl())
        assert wait_until(lambda: chrome.evaluate(STREAM) == "live", START_S, POLL_S), (
            chrome.evaluate("document.body.innerText")
        )
        visited = [entry["url"] for entry in chrome.history()]
        assert not [url for url in visited if "token=" in url], visited

        send_as_user(paths.socket, f"FIRST-{nonce}")
        assert wait_until(lambda: shows(chrome, f"FIRST-{nonce}"), START_S, POLL_S)

        before = daemon_pid(paths.socket)
        stop_daemon(paths.socket)
        assert wait_until(lambda: chrome.evaluate(STREAM) == "reconnecting", START_S, POLL_S)
        # The server starts a new relayd when the browser's EventSource comes back.
        assert wait_until(lambda: daemon_pid(paths.socket) not in (None, before), START_S, POLL_S)
        assert wait_until(lambda: chrome.evaluate(STREAM) == "live", START_S, POLL_S)

        send_as_user(paths.socket, f"SECOND-{nonce}")
        assert wait_until(lambda: shows(chrome, f"SECOND-{nonce}"), START_S, POLL_S)

        ui.send_signal(signal.SIGINT)
        assert ui.wait(timeout=START_S) == 0
        assert not Path(launch.path).exists(), "the launch page is deleted on exit"
