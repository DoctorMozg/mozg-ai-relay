"""relayd wire errors: unknown ops, unexpected exceptions and oversized lines."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from mz_relay.paths import Paths
from mz_relay.protocol import MAX_LINE_BYTES
from mz_relay.roster import Harness
from relay_rig import BOB_STALE, PROC_WAIT_S, FakeAdapter, InThread, RawConn, running_daemon


@pytest.fixture
def socket_path(relay_paths: Paths) -> Iterator[Path]:
    # A stale binding makes the adapter raise RuntimeError: an exception no op expects.
    adapter = FakeAdapter(validate_error=RuntimeError("boom"))
    with running_daemon(relay_paths, {Harness.OPENCODE: adapter}):
        yield relay_paths.socket


class TestWireErrors:
    def test_unknown_op_is_refused_and_the_connection_still_serves(self, socket_path: Path) -> None:
        with RawConn(socket_path) as conn:
            conn.hello()
            bad = conn.request("teleport", {}, rid=2)
            good = conn.request("status", {}, rid=3)

        assert (bad["ok"], bad["rid"], bad["error"]["code"]) == (False, 2, "unknown_op")
        assert (good["ok"], good["rid"], good["result"]["pid"]) == (True, 3, os.getpid())

    def test_an_op_raising_unexpectedly_is_internal_and_the_connection_still_serves(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="mz_relay")
        register = {
            "name": "bob",
            "harness": "opencode",
            "cwd": str(socket_path.parent),
            "pid": os.getpid(),
            "delivery": BOB_STALE,
        }
        with RawConn(socket_path) as conn:
            conn.hello()
            failed = conn.request("register", register, rid=2)
            good = conn.request("status", {}, rid=3)

        assert (failed["ok"], failed["rid"], failed["error"]["code"]) == (False, 2, "internal")
        assert (good["ok"], good["rid"]) == (True, 3)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(r.exc_info for r in errors), (
            "the unexpected exception was logged without traceback"
        )

    def test_a_line_over_one_mebibyte_gets_line_too_long_then_the_connection_closes(
        self, socket_path: Path
    ) -> None:
        with RawConn(socket_path) as conn:
            conn.hello()
            # relayd stops reading at the limit, so the writer blocks until relayd hangs up.
            writer = InThread(
                lambda: conn.send_ignoring_hangup(b"x" * (MAX_LINE_BYTES + 1) + b"\n")
            )
            reply = conn.reply()
            closed = conn.closed_by_peer()
            writer.result(PROC_WAIT_S)

        assert (reply["ok"], reply["rid"], reply["error"]["code"]) == (False, None, "line_too_long")
        assert closed, "relayd kept the connection open after line_too_long"
