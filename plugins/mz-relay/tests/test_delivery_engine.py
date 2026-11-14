"""Delivery engine: claim, deliver, outcome table and races against ack/inbox."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

import pytest
from mz_relay.delivery import DeliveryBinding, DeliveryJob, Outcome
from mz_relay.envelope import Status
from mz_relay.roster import AgentSpec, Harness
from mz_relay.store import StoredMessage
from relay_rig import BOB_SOCKET, DAVE_PI, NATIVE, REWAKE, EngineRig, FakeAdapter, MakeRig, result

OPENCODE = Harness.OPENCODE


def delivered_ids(adapter: FakeAdapter) -> list[str]:
    return [job.message.envelope.id for job in adapter.delivered]


class RegisteringAdapter(FakeAdapter):
    """Runs `on_unbound` once, the first time the engine asks it about a recipient without one."""

    def __init__(self) -> None:
        super().__init__()
        self.on_unbound: Callable[[], None] | None = None

    def validate_binding(
        self, spec: AgentSpec, binding: Mapping[str, object] | None
    ) -> DeliveryBinding | None:
        if binding is None and self.on_unbound is not None:
            hook, self.on_unbound = self.on_unbound, None
            hook()
        return super().validate_binding(spec, binding)


class HeartbeatAdapter(FakeAdapter):
    """Runs `on_validate` once, during the first binding validation."""

    def __init__(self) -> None:
        super().__init__()
        self.on_validate: Callable[[], None] | None = None

    def validate_binding(
        self, spec: AgentSpec, binding: Mapping[str, object] | None
    ) -> DeliveryBinding | None:
        if self.on_validate is not None:
            hook, self.on_validate = self.on_validate, None
            hook()
        return super().validate_binding(spec, binding)


class TestOutcomes:
    def test_confirmed_marks_row_delivered_via_adapter_with_rendered_text(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        handled = rig.engine.run_once(OPENCODE)

        assert handled == 1
        row = rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "fake")
        job = adapter.delivered[0]
        assert job.message.envelope.id == msg_id
        assert job.spec.name == "bob"
        assert job.binding.data == BOB_SOCKET
        assert job.text.startswith("[mz-relay] Peer message from alice")

    def test_run_once_with_nothing_due_returns_zero(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)

        assert rig.engine.run_once(OPENCODE) == 0
        assert adapter.delivered == []

    @pytest.mark.parametrize("exhausted", [Status.UNDELIVERABLE, Status.FALLBACK])
    def test_not_confirmed_retries_at_0_2_10_then_takes_exhausted_status(
        self, make_rig: MakeRig, exhausted: Status
    ) -> None:
        adapter = FakeAdapter(
            exhausted_status=exhausted,
            outcomes=[result(Outcome.NOT_CONFIRMED, "thread busy") for _ in range(3)],
        )
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()
        called_at: list[int] = []
        rows: dict[int, StoredMessage] = {}

        for offset in (0, 1, 2, 9, 10):
            rig.at(offset)
            before = len(adapter.delivered)
            rig.engine.run_once(OPENCODE)
            if len(adapter.delivered) > before:
                called_at.append(offset)
            rows[offset] = rig.row(msg_id)

        assert called_at == [0, 2, 10]
        first, second, last = rows[0], rows[2], rows[10]
        assert (first.status, first.attempts) == (Status.QUEUED, 1)
        assert first.next_attempt_at == rig.t0 + 2.0
        assert first.reason == "attempt 1 failed: thread busy"
        assert (second.attempts, second.next_attempt_at) == (2, rig.t0 + 10.0)
        assert second.reason == "attempt 2 failed: thread busy"
        assert (last.status, last.attempts, last.reason) == (exhausted, 3, "thread busy")

    def test_handed_off_schedules_confirm_one_second_later(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter(outcomes=[result(Outcome.HANDED_OFF)]))
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert row.status == Status.HANDED_OFF
        assert row.handed_off_at == rig.t0
        assert row.next_attempt_at == rig.t0 + 1.0

    def test_unreachable_takes_registration_offline_and_parks_row(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter(outcomes=[result(Outcome.UNREACHABLE, "connection refused")])
        rig = make_rig(adapter)
        token = rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)
        rig.at(60)
        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert row.status == Status.QUEUED
        assert row.next_attempt_at is None
        assert row.reason == "unreachable: connection refused; waits for bob to reconnect"
        reg = rig.store.registration(token)
        assert reg is not None
        assert reg.online is False
        assert len(adapter.delivered) == 1

    def test_unreachable_native_with_live_fallback_rearms_now_through_fallback(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter(outcomes=[result(Outcome.UNREACHABLE, "socket gone")])
        rig = make_rig(adapter)
        native = rig.register("bob", NATIVE, mode="native", age_s=10.0)
        fallback = rig.register("bob", REWAKE, mode="fallback")
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at, row.reason) == (Status.QUEUED, rig.t0, None)
        native_reg, fallback_reg = rig.store.registration(native), rig.store.registration(fallback)
        assert native_reg is not None
        assert fallback_reg is not None
        assert (native_reg.online, fallback_reg.online) == (False, True)

        rig.engine.run_once(OPENCODE)

        assert rig.row(msg_id).status == Status.DELIVERED
        assert [job.binding.data for job in adapter.delivered] == [NATIVE, REWAKE]

    def test_fallback_outcome_moves_row_to_fallback(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter(outcomes=[result(Outcome.FALLBACK)]))
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        assert rig.row(msg_id).status == Status.FALLBACK

    def test_deferred_requeues_two_seconds_later_without_counting_an_attempt(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter(outcomes=[result(Outcome.DEFERRED, "busy")])
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.attempts) == (Status.QUEUED, 0)
        assert row.next_attempt_at == rig.t0 + 2.0
        rig.at(1)
        rig.engine.run_once(OPENCODE)
        assert len(adapter.delivered) == 1
        rig.at(2)
        rig.engine.run_once(OPENCODE)
        assert len(adapter.delivered) == 2
        assert rig.row(msg_id).status == Status.DELIVERED

    def test_adapter_exception_is_a_failed_attempt_with_error_log(
        self, make_rig: MakeRig, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="mz_relay")
        rig = make_rig(FakeAdapter(outcomes=[RuntimeError("boom")]))
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.attempts) == (Status.QUEUED, 1)
        assert row.reason == "attempt 1 failed: internal: RuntimeError"
        assert row.next_attempt_at == rig.t0 + 2.0
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "adapter exception was not logged at ERROR"
        assert any(r.exc_info for r in errors), "ERROR log carries no traceback"


class TestClaim:
    def test_row_pulled_by_inbox_first_is_never_handed_to_adapter(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()
        pulled = rig.store.pull_for("bob", 10, rig.clock.now())
        assert [m.envelope.id for m in pulled] == [msg_id]

        rig.engine.run_once(OPENCODE)

        assert adapter.delivered == []
        row = rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "inbox")

    def test_row_is_claimed_delivering_while_adapter_runs(self, make_rig: MakeRig) -> None:
        seen: list[StoredMessage] = []
        rig: EngineRig

        def on_deliver(job: DeliveryJob) -> None:
            seen.append(rig.row(job.message.envelope.id))

        rig = make_rig(FakeAdapter(on_deliver=on_deliver))
        rig.register("bob", BOB_SOCKET)
        rig.queue()

        rig.engine.run_once(OPENCODE)

        assert [m.status for m in seen] == [Status.DELIVERING]
        assert seen[0].lease_until == rig.t0 + 120.0

    def test_only_recipients_of_the_given_harness_are_processed(self, make_rig: MakeRig) -> None:
        oc, pi = FakeAdapter(), FakeAdapter()
        rig = make_rig(oc, pi=pi, extra_agents=DAVE_PI)
        rig.register("bob", BOB_SOCKET)
        rig.register("dave", {"socket": "/tmp/mzr-dave.sock", "pid": 4243})
        to_bob = rig.queue("bob")
        to_dave = rig.queue("dave")

        rig.engine.run_once(Harness.PI)

        assert delivered_ids(pi) == [to_dave]
        assert oc.delivered == []
        assert rig.row(to_bob).status == Status.QUEUED


class TestRaces:
    def test_ack_during_delivery_wins_over_late_confirmed(
        self, make_rig: MakeRig, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="mz_relay")
        rig: EngineRig
        rig = make_rig(FakeAdapter(on_deliver=lambda job: rig.ack_job(job)))
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        assert rig.acked_during_delivery == [True]
        assert rig.row(msg_id).status == Status.ACKED
        stale = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "stale outcome confirmed" in r.getMessage()
        ]
        assert stale, "no INFO 'stale outcome confirmed' log"
        assert msg_id in stale[0]

    def test_inbox_during_delivery_gets_nothing_and_row_is_delivered_once(
        self, make_rig: MakeRig
    ) -> None:
        pulls: list[list[StoredMessage]] = []
        rig: EngineRig

        def on_deliver(job: DeliveryJob) -> None:
            pulls.append(rig.store.pull_for("bob", 10, rig.clock.now()))

        adapter = FakeAdapter(on_deliver=on_deliver)
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)
        rig.at(30)
        rig.engine.run_once(OPENCODE)

        assert pulls == [[]]
        row = rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "fake")
        assert len(adapter.delivered) == 1

    def test_ack_during_delivery_then_not_confirmed_schedules_no_retry(
        self, make_rig: MakeRig
    ) -> None:
        rig: EngineRig
        adapter = FakeAdapter(
            outcomes=[result(Outcome.NOT_CONFIRMED, "busy")],
            on_deliver=lambda job: rig.ack_job(job),
        )
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)
        rig.at(20)
        rig.engine.run_once(OPENCODE)

        assert rig.acked_during_delivery == [True]
        assert rig.row(msg_id).status == Status.ACKED
        assert len(adapter.delivered) == 1

    def test_ack_during_delivery_then_adapter_raises_keeps_acked(self, make_rig: MakeRig) -> None:
        rig: EngineRig
        adapter = FakeAdapter(
            outcomes=[RuntimeError("boom")],
            on_deliver=lambda job: rig.ack_job(job),
        )
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        assert rig.acked_during_delivery == [True]
        assert rig.row(msg_id).status == Status.ACKED


class TestParkRace:
    def test_a_registration_landing_while_the_row_is_parked_still_gets_it_delivered(
        self, make_rig: MakeRig
    ) -> None:
        adapter = RegisteringAdapter()
        rig = make_rig(adapter)
        msg_id = rig.queue()

        def bob_registers() -> None:
            # What the register op does: commit the registration, then wake parked rows.
            rig.register("bob", BOB_SOCKET)
            rig.store.wake_recipient("bob", rig.clock.now())

        adapter.on_unbound = bob_registers

        rig.engine.run_once(OPENCODE)

        parked = rig.row(msg_id)
        assert parked.status == Status.QUEUED
        assert parked.next_attempt_at is not None, "row parked although bob has a binding now"

        rig.at(0.1)
        rig.engine.run_once(OPENCODE)

        assert rig.row(msg_id).status == Status.DELIVERED
        assert [job.binding.data for job in adapter.delivered] == [BOB_SOCKET]

    def test_a_heartbeat_on_the_same_registration_does_not_rearm_a_pull_only_row(
        self, make_rig: MakeRig
    ) -> None:
        adapter = HeartbeatAdapter()
        rig = make_rig(adapter)
        token = rig.register("bob", {})
        msg_id = rig.queue()
        # last_seen moves between bind and park; token, mode and binding stay the same.
        adapter.on_validate = lambda: rig.store.set_online(token, True, rig.clock.now() + 1)

        rig.engine.run_once(OPENCODE)

        parked = rig.row(msg_id)
        assert parked.status == Status.QUEUED
        assert parked.next_attempt_at is None, "a last_seen bump re-armed the pull-only row"
