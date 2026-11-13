"""PresenceSession: register over its own connection, re-register after a drop."""

from __future__ import annotations

import itertools
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from mz_relay.clock import SystemClock
from mz_relay.errors import DaemonUnavailable
from mz_relay.launcher import DaemonLauncher
from mz_relay.paths import Paths
from mz_relay.presence import PresenceSession, RegisterArgs, configured_name
from mz_relay.roster import Harness
from relay_rig import (
    WAIT_S,
    FakeRelayd,
    Message,
    NoSpawn,
    Reply,
    hello_then,
    launcher_for,
    ok,
    token,
    wait_until,
)

AGENT_CARD: dict[str, object] = {
    "name": "alice",
    "role": "lead",
    "harness": "opencode",
    "label": "personal",
    "description": "Leads the work.",
    "tools": "full",
}
BRIEF = 'You are "alice" on mz-relay, role lead: Leads the work.'
ARGS = RegisterArgs(
    name="alice",
    harness=Harness.OPENCODE,
    cwd=Path("/tmp/mzr-alice"),
    pid=os.getpid(),
    client="mcp",
)


def registrar() -> Reply:
    """Answers hello like relayd, and each register with the next token."""
    tokens = itertools.count(1)

    def register(req: Message) -> Message | None:
        if req["op"] != "register":
            return ok(req, {})
        issued = token(next(tokens))
        return ok(
            req, {"token": issued, "agent": AGENT_CARD, "delivery_mode": None, "brief": BRIEF}
        )

    return hello_then(register)


@pytest.fixture
def relayd(fake_relayd: Callable[[Reply], FakeRelayd]) -> FakeRelayd:
    return fake_relayd(registrar())


@pytest.fixture
def spawn() -> NoSpawn:
    return NoSpawn()


@pytest.fixture
def launcher(relay_paths: Paths, spawn: NoSpawn) -> DaemonLauncher:
    return launcher_for(relay_paths, spawn)


def new_session(launcher: DaemonLauncher) -> PresenceSession:
    return PresenceSession(launcher, ARGS, clock=SystemClock(), backoff_s=(0.05,))


class TestPresence:
    def test_start_registers_over_hello_and_returns_registered(
        self, relayd: FakeRelayd, launcher: DaemonLauncher, spawn: NoSpawn
    ) -> None:
        session = new_session(launcher)
        try:
            registered = session.start(wait_s=1.0)

            assert registered.token == token(1)
            assert registered.agent == AGENT_CARD
            assert registered.delivery_mode is None
            assert registered.brief == BRIEF
            assert session.current().token == token(1)
            assert relayd.args_of("hello")[0]["client"] == "mcp"
            sent = relayd.args_of("register")[0]
            assert (sent["name"], sent["harness"]) == ("alice", "opencode")
            assert (sent["cwd"], sent["pid"]) == ("/tmp/mzr-alice", os.getpid())
            assert spawn.calls == []
        finally:
            session.close()

    def test_dropped_connection_reregisters_with_a_new_token(
        self,
        relayd: FakeRelayd,
        launcher: DaemonLauncher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="mz_relay.presence")
        session = new_session(launcher)
        try:
            session.start(wait_s=1.0)

            relayd.drop_connection_of("register")

            assert wait_until(lambda: session.current().token == token(2), WAIT_S)
            assert relayd.args_of("register")[1]["name"] == "alice"
            assert any(
                r.levelno == logging.INFO and r.name == "mz_relay.presence" for r in caplog.records
            )
        finally:
            session.close()

    def test_close_stops_any_further_reconnect(
        self, relayd: FakeRelayd, launcher: DaemonLauncher
    ) -> None:
        session = new_session(launcher)
        session.start(wait_s=1.0)

        session.close()
        # Ten backoff periods: a reconnect loop that survived close would register again.
        time.sleep(0.5)

        assert len(relayd.args_of("register")) == 1

    def test_an_os_error_in_the_launcher_does_not_end_re_registration(
        self,
        fake_relayd: Callable[[Reply], FakeRelayd],
        relay_paths: Paths,
        launcher: DaemonLauncher,
    ) -> None:
        # The first reconnect's hello is hung up, so the launcher goes on to take spawn.lock,
        # which is a directory here and cannot be opened.
        relay_paths.spawn_lock.mkdir()
        hellos = itertools.count(1)
        answer = registrar()

        def refuse_second_hello(req: Message) -> Message | None:
            if req["op"] == "hello" and next(hellos) == 2:
                return None
            return answer(req)

        relayd = fake_relayd(refuse_second_hello)
        session = new_session(launcher)
        try:
            session.start(wait_s=1.0)

            relayd.drop_connection_of("register")

            assert wait_until(lambda: session.current().token == token(2), WAIT_S)
            assert len(relayd.args_of("hello")) >= 3
        finally:
            session.close()

    def test_start_raises_when_the_daemon_cannot_be_reached(self, launcher: DaemonLauncher) -> None:
        session = new_session(launcher)

        with pytest.raises(DaemonUnavailable):
            session.start(wait_s=0.3)


class TestConfiguredName:
    """MCP servers and hooks both see the plugin option; it wins over a shell MZ_RELAY_NAME."""

    OPTION = "CLAUDE_PLUGIN_OPTION_RELAY_NAME"

    def test_the_plugin_option_wins_over_the_shell_name(self) -> None:
        assert configured_name({self.OPTION: "cc", "MZ_RELAY_NAME": "cf"}) == "cc"

    def test_the_shell_name_is_used_without_the_plugin_option(self) -> None:
        assert configured_name({"MZ_RELAY_NAME": "cf"}) == "cf"

    @pytest.mark.parametrize("raw", ["", "  ", "${user_config.relay_name}"])
    def test_an_unset_looking_plugin_option_falls_back_to_the_shell_name(self, raw: str) -> None:
        assert configured_name({self.OPTION: raw, "MZ_RELAY_NAME": "cf"}) == "cf"

    def test_neither_set_is_none(self) -> None:
        assert configured_name({self.OPTION: "", "MZ_RELAY_NAME": ""}) is None
