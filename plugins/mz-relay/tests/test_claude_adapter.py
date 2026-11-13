"""Claude adapter: binding checks, native posts confirmed in the transcript, busy and fallback."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Literal

import pytest
from fakes_claude import PINNED_VERSION, FakeClaudeSession, claude_agent, claude_roster, mz_text
from mz_relay.adapters.claude import ClaudeAdapter, ClaudeTiming
from mz_relay.clock import Clock, SystemClock
from mz_relay.delivery import (
    DeliveryBinding,
    DeliveryJob,
    DeliveryResult,
    HarnessAdapter,
    Outcome,
)
from mz_relay.envelope import Status
from mz_relay.errors import BindingError
from mz_relay.roster import AgentSpec, Harness, parse_roster
from mz_relay.store import StoredMessage
from relay_rig import (
    WAIT_S,
    EngineRig,
    FakeClock,
    InThread,
    SleepHookClock,
    envelope,
    mid,
    wait_until,
)

TIMING = ClaudeTiming(confirm_s=0.5, poll_s=0.02, busy_grace_s=1.0)
MSG = mid(7001)
OTHER = mid(7002)
# Far past busy_grace_s plus any sane margin for keeping an unasked mark.
LONG_AFTER_S = 3600.0
NO_LINE = f"no transcript line within {TIMING.confirm_s} s"
BUSY_DETAIL = "recipient busy; read_only deliveries wait for idle"

Mode = Literal["accept", "drop", "busy"]


def frame_of(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


class AdapterRig:
    """A real ClaudeAdapter on wall time, aimed at one FakeClaudeSession."""

    def __init__(
        self, home: Path, fake: FakeClaudeSession, tools: str, clock: Clock | None = None
    ) -> None:
        self.fake = fake
        roster = parse_roster(claude_roster(fake.config_dir, tools=tools), pw_home=home)
        self.spec: AgentSpec = roster.get("cc")
        self.clock: Clock = clock or SystemClock()
        self.claude = ClaudeAdapter(clock=self.clock, timing=TIMING)
        self.adapter: HarnessAdapter = self.claude

    def validate(self, raw: Mapping[str, object] | None) -> DeliveryBinding | None:
        return self.adapter.validate_binding(self.spec, raw)

    def bind(self, raw: Mapping[str, object] | None = None) -> DeliveryBinding:
        binding = self.validate(self.fake.native_binding() if raw is None else raw)
        assert binding is not None
        return binding

    def job(
        self,
        binding: DeliveryBinding,
        *,
        status: Status = Status.DELIVERING,
        handed_off_s: float = 0,
        msg_id: str = MSG,
    ) -> DeliveryJob:
        """A job for `msg_id`; `handed_off_s` ago it was handed off (HANDED_OFF jobs only)."""
        now = self.clock.now()
        handed_off_at = now - handed_off_s if status is Status.HANDED_OFF else None
        row = StoredMessage(
            envelope=envelope(msg_id, source="sender", to="cc", at=now),
            status=status,
            reason=None,
            via=None,
            attempts=0,
            next_attempt_at=None,
            handed_off_at=handed_off_at,
            lease_until=None,
            created_at=now,
            updated_at=now,
        )
        text = mz_text(msg_id)
        return DeliveryJob(message=row, text=text, spec=self.spec, binding=binding, now=now)

    def deliver(self, binding: DeliveryBinding | None = None) -> DeliveryResult:
        return self.adapter.deliver(self.job(binding or self.bind()))

    def confirm(
        self, binding: DeliveryBinding, *, handed_off_s: float, msg_id: str = MSG
    ) -> DeliveryResult:
        job = self.job(binding, status=Status.HANDED_OFF, handed_off_s=handed_off_s, msg_id=msg_id)
        return self.adapter.confirm(job)

    def registry_rewrite_in_flight(self) -> None:
        """The registry entry reads as missing until the adapter's next sleep ends the rewrite."""
        assert isinstance(self.clock, SleepHookClock)
        self.fake.registry_path.unlink()
        self.clock.on_next_sleep = lambda: self.fake.set_status(self.fake.status)


class MakeFake:
    """Starts FakeClaudeSessions under `<home>/c`; every one is closed after the test."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self._stack = contextlib.ExitStack()

    def __call__(
        self,
        *,
        mode: Mode = "accept",
        status: str = "idle",
        version: str = PINNED_VERSION,
        peer_protocol: int = 1,
        pid: int | None = None,
    ) -> FakeClaudeSession:
        fake = FakeClaudeSession(
            self.home / "c",
            mode=mode,
            status=status,
            version=version,
            peer_protocol=peer_protocol,
            pid=pid,
        )
        return self._stack.enter_context(fake)

    def close(self) -> None:
        self._stack.close()


@pytest.fixture
def make_fake(relay_home: Path) -> Iterator[MakeFake]:
    maker = MakeFake(relay_home)
    yield maker
    maker.close()


@pytest.fixture
def make(relay_home: Path, make_fake: MakeFake) -> Callable[..., AdapterRig]:
    def build(
        *,
        tools: str = "full",
        mode: Mode = "accept",
        status: str = "idle",
        clock: Clock | None = None,
    ) -> AdapterRig:
        return AdapterRig(relay_home, make_fake(mode=mode, status=status), tools, clock)

    return build


def nonce_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if MSG in line]


class TestBinding:
    def test_no_binding_gives_no_delivery_channel(self, make: Callable[..., AdapterRig]) -> None:
        rig = make()

        assert rig.validate(None) is None

    def test_native_binding_matching_the_registry_is_native(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()

        assert rig.bind().mode == "native"

    def test_rewake_only_binding_is_fallback_mode(self, make: Callable[..., AdapterRig]) -> None:
        rig = make()

        assert rig.bind(rig.fake.rewake_binding()).mode == "fallback"

    @pytest.mark.parametrize("kind", ["native", "rewake"])
    def test_config_dir_other_than_the_roster_is_refused(
        self, make: Callable[..., AdapterRig], relay_home: Path, kind: str
    ) -> None:
        rig = make()
        raw = rig.fake.native_binding() if kind == "native" else rig.fake.rewake_binding()
        other = relay_home / "other-cfg"
        other.mkdir()
        raw["config_dir"] = str(other)

        with pytest.raises(BindingError) as caught:
            rig.validate(raw)
        assert caught.value.code == "binding_invalid"

    def test_config_dir_reached_through_a_symlink_is_accepted(
        self, make: Callable[..., AdapterRig], relay_home: Path
    ) -> None:
        rig = make()
        link = relay_home / "cfg-link"
        link.symlink_to(rig.fake.config_dir)
        raw = rig.fake.native_binding()
        raw["config_dir"] = str(link)

        assert rig.bind(raw).mode == "native"

    def test_socket_other_than_the_registry_entry_is_refused(
        self, make: Callable[..., AdapterRig], relay_home: Path
    ) -> None:
        rig = make()
        raw = rig.fake.native_binding()
        raw["messaging_socket"] = str(relay_home / "c" / "s" / "elsewhere.sock")

        with pytest.raises(BindingError):
            rig.validate(raw)

    def test_pid_without_a_registry_entry_is_refused(self, make: Callable[..., AdapterRig]) -> None:
        rig = make()
        raw = rig.fake.native_binding()
        raw["pid"] = os.getppid()

        with pytest.raises(BindingError):
            rig.validate(raw)

    def test_binding_with_neither_socket_nor_rewake_is_refused(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()
        raw = {"config_dir": str(rig.fake.config_dir), "session_id": rig.fake.session_id}

        with pytest.raises(BindingError):
            rig.validate(raw)

    @pytest.mark.parametrize(("version", "peer_protocol"), [("2.1.300", 1), (PINNED_VERSION, 2)])
    def test_unsupported_claude_falls_back_with_a_warning(
        self,
        relay_home: Path,
        make_fake: MakeFake,
        caplog: pytest.LogCaptureFixture,
        version: str,
        peer_protocol: int,
    ) -> None:
        rig = AdapterRig(
            relay_home, make_fake(version=version, peer_protocol=peer_protocol), "full"
        )
        caplog.set_level(logging.WARNING, logger="mz_relay")

        binding = rig.bind()

        assert binding.mode == "fallback"
        assert any(record.levelno == logging.WARNING for record in caplog.records)


class TestDeliver:
    def test_accepted_post_is_confirmed_by_its_transcript_line(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()

        got = rig.deliver()

        assert got.outcome is Outcome.CONFIRMED
        assert rig.fake.frames == [frame_of(mz_text(MSG))]

    def test_post_is_one_frame_line_without_an_auth_line(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()

        rig.deliver()

        lines = rig.fake.raw_lines
        assert len(lines) == 1
        assert json.loads(lines[0]) == frame_of(mz_text(MSG))

    def test_nonce_in_a_human_line_does_not_confirm(self, make: Callable[..., AdapterRig]) -> None:
        rig = make(mode="drop")
        binding = rig.bind()
        posting = InThread(lambda: rig.adapter.deliver(rig.job(binding)))
        assert wait_until(lambda: len(rig.fake.raw_lines) == 1, WAIT_S)
        rig.fake.writer.human(f"what is {MSG}?")

        got = posting.result()

        assert (got.outcome, got.detail) == (Outcome.NOT_CONFIRMED, NO_LINE)

    def test_peer_line_written_before_the_post_does_not_confirm(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make(mode="drop")
        rig.fake.writer.peer(mz_text(MSG))

        got = rig.deliver()

        assert (got.outcome, got.detail) == (Outcome.NOT_CONFIRMED, NO_LINE)

    def test_dropped_post_to_an_idle_session_is_not_confirmed(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make(mode="drop")

        got = rig.deliver()

        assert (got.outcome, got.detail) == (Outcome.NOT_CONFIRMED, NO_LINE)
        assert len(rig.fake.frames) == 1

    def test_post_to_a_busy_session_is_handed_off_then_confirmed(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make(mode="busy", status="busy")
        binding = rig.bind()

        handed = rig.adapter.deliver(rig.job(binding))
        rig.fake.release_busy()
        confirmed = rig.confirm(binding, handed_off_s=0.1)

        assert handed.outcome is Outcome.HANDED_OFF
        assert confirmed.outcome is Outcome.CONFIRMED
        assert len(rig.fake.frames) == 1

    def test_read_only_recipient_busy_is_deferred_without_posting(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make(tools="read_only", status="busy")

        got = rig.deliver()

        assert (got.outcome, got.detail) == (Outcome.DEFERRED, BUSY_DETAIL)
        assert rig.fake.frames == []
        assert rig.fake.raw_lines == []

    def test_read_only_recipient_with_an_unknown_status_counts_as_busy(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make(tools="read_only", status="waiting")

        got = rig.deliver()

        assert (got.outcome, got.detail) == (Outcome.DEFERRED, BUSY_DETAIL)
        assert rig.fake.raw_lines == []

    def test_read_only_recipient_idle_is_posted(self, make: Callable[..., AdapterRig]) -> None:
        rig = make(tools="read_only")

        assert rig.deliver().outcome is Outcome.CONFIRMED
        assert len(rig.fake.frames) == 1

    def test_removed_socket_is_unreachable(self, make: Callable[..., AdapterRig]) -> None:
        rig = make()
        binding = rig.bind()
        rig.fake.socket.unlink()

        assert rig.deliver(binding).outcome is Outcome.UNREACHABLE

    def test_socket_refusing_connections_is_unreachable(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()
        binding = rig.bind()
        rig.fake.socket.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as refusing:
            refusing.bind(str(rig.fake.socket))

            got = rig.deliver(binding)

        assert got.outcome is Outcome.UNREACHABLE

    def test_session_gone_from_the_registry_is_unreachable(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()
        binding = rig.bind()
        rig.fake.registry_path.unlink()

        assert rig.deliver(binding).outcome is Outcome.UNREACHABLE
        assert rig.fake.frames == []

    def test_dead_session_pid_is_unreachable_without_posting(
        self, relay_home: Path, make_fake: MakeFake, dead_pid: int
    ) -> None:
        rig = AdapterRig(relay_home, make_fake(pid=dead_pid), "full")
        binding = rig.bind()

        assert rig.deliver(binding).outcome is Outcome.UNREACHABLE
        assert rig.fake.frames == []

    def test_rotated_session_is_confirmed_in_the_new_transcript(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()
        binding = rig.bind()
        first_transcript = rig.fake.transcript_path
        rig.fake.rotate_session("s-2")

        got = rig.deliver(binding)

        assert got.outcome is Outcome.CONFIRMED
        assert len(nonce_lines(rig.fake.transcript_path)) == 1
        assert nonce_lines(first_transcript) == []

    def test_rewake_only_binding_falls_back_without_posting(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig = make()

        got = rig.deliver(rig.bind(rig.fake.rewake_binding()))

        assert got.outcome is Outcome.FALLBACK
        assert rig.fake.raw_lines == []

    def test_version_pinned_native_binding_falls_back_without_posting(
        self, relay_home: Path, make_fake: MakeFake
    ) -> None:
        rig = AdapterRig(relay_home, make_fake(version="2.1.300"), "full")

        got = rig.deliver()

        assert got.outcome is Outcome.FALLBACK
        assert rig.fake.raw_lines == []


class TestConfirm:
    def handed_off(self, make: Callable[..., AdapterRig]) -> tuple[AdapterRig, DeliveryBinding]:
        rig = make(mode="busy", status="busy")
        binding = rig.bind()
        assert rig.adapter.deliver(rig.job(binding)).outcome is Outcome.HANDED_OFF
        return rig, binding

    def test_busy_session_stays_handed_off_past_the_grace(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig, binding = self.handed_off(make)

        assert rig.confirm(binding, handed_off_s=5.0).outcome is Outcome.HANDED_OFF

    def test_idle_session_within_the_grace_stays_handed_off(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig, binding = self.handed_off(make)
        rig.fake.set_status("idle")

        assert rig.confirm(binding, handed_off_s=0.1).outcome is Outcome.HANDED_OFF

    def test_idle_session_without_a_line_after_the_grace_is_not_confirmed(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig, binding = self.handed_off(make)
        rig.fake.set_status("idle")

        past_grace = 2 * TIMING.busy_grace_s
        assert rig.confirm(binding, handed_off_s=past_grace).outcome is Outcome.NOT_CONFIRMED

    @pytest.mark.parametrize(("status", "handed_off_s"), [("busy", 0.1), ("idle", 2.0)])
    def test_a_line_in_the_session_after_clear_confirms(
        self, make: Callable[..., AdapterRig], status: str, handed_off_s: float
    ) -> None:
        rig, binding = self.handed_off(make)
        # /clear: a new session id under the same pid; Claude writes the queued post there.
        rig.fake.rotate_session("s-2")
        rig.fake.release_busy()
        rig.fake.set_status(status)

        got = rig.confirm(binding, handed_off_s=handed_off_s)

        assert got.outcome is Outcome.CONFIRMED
        assert len(nonce_lines(rig.fake.transcript_path)) == 1


class TestRegistryRewrite:
    """One missing or torn registry read while the pid lives is a rewrite in flight, not gone."""

    @pytest.fixture
    def make_rewriting(self, make: Callable[..., AdapterRig]) -> Callable[..., AdapterRig]:
        def build(**kwargs: object) -> AdapterRig:
            return make(clock=SleepHookClock(SystemClock()), **kwargs)

        return build

    def test_deliver_reads_the_registry_again_and_posts(
        self, make_rewriting: Callable[..., AdapterRig]
    ) -> None:
        rig = make_rewriting()
        binding = rig.bind()
        rig.registry_rewrite_in_flight()

        got = rig.deliver(binding)

        assert got.outcome is Outcome.CONFIRMED
        assert rig.fake.frames == [frame_of(mz_text(MSG))]

    def test_confirm_reads_the_registry_again_and_stays_handed_off(
        self, make_rewriting: Callable[..., AdapterRig]
    ) -> None:
        rig = make_rewriting(mode="busy", status="busy")
        binding = rig.bind()
        assert rig.adapter.deliver(rig.job(binding)).outcome is Outcome.HANDED_OFF
        rig.registry_rewrite_in_flight()

        assert rig.confirm(binding, handed_off_s=0.1).outcome is Outcome.HANDED_OFF

    def test_validate_binding_reads_the_registry_again(
        self, make_rewriting: Callable[..., AdapterRig]
    ) -> None:
        rig = make_rewriting()
        rig.registry_rewrite_in_flight()

        assert rig.bind().mode == "native"


class TestMarks:
    """A mark lives while its row may still be confirmed; rows acked while HANDED_OFF never are."""

    def handed_off(self, make: Callable[..., AdapterRig]) -> tuple[AdapterRig, FakeClock]:
        clock = FakeClock(start=time.time())
        rig = make(mode="busy", status="busy", clock=clock)
        binding = rig.bind()
        assert rig.adapter.deliver(rig.job(binding)).outcome is Outcome.HANDED_OFF
        assert rig.claude.pending_marks == 1
        return rig, clock

    def test_a_mark_no_one_asks_about_is_dropped_long_after_the_grace(
        self, make: Callable[..., AdapterRig]
    ) -> None:
        rig, clock = self.handed_off(make)
        clock.advance(TIMING.busy_grace_s + LONG_AFTER_S)

        rig.confirm(rig.bind(), handed_off_s=0.1, msg_id=OTHER)

        assert rig.claude.pending_marks == 0

    def test_a_mark_within_the_grace_is_kept(self, make: Callable[..., AdapterRig]) -> None:
        rig, clock = self.handed_off(make)
        clock.advance(TIMING.busy_grace_s / 2)

        rig.confirm(rig.bind(), handed_off_s=0.1, msg_id=OTHER)

        assert rig.claude.pending_marks == 1


class EngineClaude:
    """The real engine and Store on a FakeClock, with a real ClaudeAdapter on wall time."""

    def __init__(self, home: Path, fake: FakeClaudeSession, tools: str) -> None:
        self.fake = fake
        self.adapter: HarnessAdapter = ClaudeAdapter(clock=SystemClock(), timing=TIMING)
        self.rig = EngineRig(
            home, {Harness.CLAUDE: self.adapter}, claude_agent(fake.config_dir, tools=tools)
        )

    def native(self) -> str:
        return self.rig.register("cc", self.fake.native_binding(), mode="native")

    def rewake(self) -> str:
        return self.rig.register("cc", self.fake.rewake_binding(), mode="fallback")

    def pass_at(self, offset: float) -> None:
        self.rig.at(offset)
        self.rig.engine.run_once(Harness.CLAUDE)


@pytest.fixture
def make_engine(relay_home: Path, make_fake: MakeFake) -> Iterator[Callable[..., EngineClaude]]:
    with contextlib.ExitStack() as stack:

        def build(
            *, tools: str = "full", mode: Mode = "accept", status: str = "idle"
        ) -> EngineClaude:
            engine = EngineClaude(relay_home, make_fake(mode=mode, status=status), tools)
            stack.callback(engine.rig.store.close)
            return engine

        yield build


class TestEngine:
    def test_native_post_is_delivered_via_claude(
        self, make_engine: Callable[..., EngineClaude]
    ) -> None:
        claude = make_engine()
        claude.native()
        msg_id = claude.rig.queue(to="cc")

        claude.pass_at(0)

        row = claude.rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "claude")

    def test_dropped_posts_retry_three_times_then_fall_back(
        self, make_engine: Callable[..., EngineClaude]
    ) -> None:
        claude = make_engine(mode="drop")
        claude.native()
        msg_id = claude.rig.queue(to="cc")

        for offset in (0, 2, 10):
            claude.pass_at(offset)

        row = claude.rig.row(msg_id)
        assert (row.status, row.attempts, row.reason) == (Status.FALLBACK, 3, NO_LINE)
        assert len(claude.fake.frames) == 3

    def test_read_only_busy_recipient_is_deferred_then_posted_when_idle(
        self, make_engine: Callable[..., EngineClaude]
    ) -> None:
        claude = make_engine(tools="read_only", status="busy")
        claude.native()
        msg_id = claude.rig.queue(to="cc")

        claude.pass_at(0)
        deferred = claude.rig.row(msg_id)
        claude.fake.set_status("idle")
        claude.pass_at(2)

        assert (deferred.status, deferred.attempts) == (Status.QUEUED, 0)
        assert deferred.next_attempt_at == claude.rig.t0 + 2
        row = claude.rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "claude")
        assert len(claude.fake.frames) == 1

    def test_a_read_only_row_deferred_again_and_again_logs_info_only_once(
        self, make_engine: Callable[..., EngineClaude], caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="mz_relay")
        claude = make_engine(tools="read_only", status="busy")
        claude.native()
        msg_id = claude.rig.queue(to="cc")

        claude.pass_at(0)
        first = [
            r for r in caplog.records if r.levelno == logging.INFO and msg_id in r.getMessage()
        ]
        caplog.clear()
        for offset in (2, 4, 6):
            claude.pass_at(offset)

        assert first, "the first deferral is logged at INFO"
        about_row = [r for r in caplog.records if msg_id in r.getMessage()]
        assert [r.getMessage() for r in about_row if r.levelno > logging.DEBUG] == []
        assert about_row, "later deferrals are still logged at DEBUG"
        assert claude.rig.row(msg_id).status is Status.QUEUED

    def test_handed_off_post_is_confirmed_on_a_later_pass(
        self, make_engine: Callable[..., EngineClaude]
    ) -> None:
        claude = make_engine(mode="busy", status="busy")
        claude.native()
        msg_id = claude.rig.queue(to="cc")

        claude.pass_at(0)
        handed = claude.rig.row(msg_id)
        claude.fake.release_busy()
        claude.pass_at(1)

        assert handed.status is Status.HANDED_OFF
        row = claude.rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "claude")

    def test_unreachable_native_session_hands_over_to_the_waiter_binding(
        self, make_engine: Callable[..., EngineClaude]
    ) -> None:
        claude = make_engine()
        native = claude.native()
        claude.rewake()
        msg_id = claude.rig.queue(to="cc")
        claude.fake.socket.unlink()

        claude.pass_at(0)
        rearmed = claude.rig.row(msg_id)
        claude.pass_at(0)

        assert not claude.rig.online(native)
        assert (rearmed.status, rearmed.reason) == (Status.QUEUED, None)
        assert claude.rig.row(msg_id).status is Status.FALLBACK
        assert claude.fake.frames == []
