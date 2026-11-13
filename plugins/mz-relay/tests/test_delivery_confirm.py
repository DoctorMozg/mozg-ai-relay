"""Delivery engine: binding errors, confirm, sweep, TTL expiry, selection and next_wakeup."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from typing import cast

import pytest
from mz_relay.delivery import DeliveryJob, DeliveryResult, Outcome
from mz_relay.envelope import Status
from mz_relay.errors import BindingError
from mz_relay.roster import Harness
from mz_relay.store import StatusChange
from relay_rig import (
    BOB_SOCKET,
    BOB_STALE,
    CAROL_SOCKET,
    DAVE_PI,
    NATIVE,
    REWAKE,
    EngineRig,
    FakeAdapter,
    MakeRig,
    envelope,
    mid,
    result,
)

OPENCODE = Harness.OPENCODE
PULL_ONLY_REASON = "pull-only: bob has no wake-up channel; waits for its inbox call"


def recipients(jobs: list[DeliveryJob]) -> list[str]:
    return [job.message.envelope.to for job in jobs]


def warnings_naming(caplog: pytest.LogCaptureFixture, msg_id: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and msg_id in r.getMessage()
    ]


class TestBindingErrors:
    def test_stale_binding_parks_that_recipient_while_others_deliver_in_same_pass(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter(validate_error=BindingError("socket gone"))
        rig = make_rig(adapter)
        bob_token = rig.register("bob", BOB_STALE)
        rig.register("carol", CAROL_SOCKET)
        to_bob = rig.queue("bob")
        to_carol = rig.queue("carol")

        rig.engine.run_once(OPENCODE)

        bob_row = rig.row(to_bob)
        assert (bob_row.status, bob_row.next_attempt_at) == (Status.QUEUED, None)
        assert bob_row.reason == "unreachable: socket gone; waits for bob to reconnect"
        assert rig.online(bob_token) is False
        carol_row = rig.row(to_carol)
        assert (carol_row.status, carol_row.via) == (Status.DELIVERED, "fake")
        assert recipients(adapter.delivered) == ["carol"]

    def test_newer_stale_registration_rearms_row_through_older_good_one(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter(validate_error=BindingError("socket gone"))
        rig = make_rig(adapter)
        good = rig.register("bob", BOB_SOCKET, age_s=10.0)
        stale = rig.register("bob", BOB_STALE)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at, row.reason) == (Status.QUEUED, rig.t0, None)
        assert (rig.online(stale), rig.online(good)) == (False, True)
        assert adapter.delivered == []

        rig.engine.run_once(OPENCODE)

        assert rig.row(msg_id).status == Status.DELIVERED
        assert [job.binding.data for job in adapter.delivered] == [BOB_SOCKET]

    def test_unexpected_validate_error_defers_row_and_warns_once(
        self, make_rig: MakeRig, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="mz_relay")
        adapter = FakeAdapter(validate_error=RuntimeError("boom"))
        rig = make_rig(adapter)
        rig.register("bob", BOB_STALE)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at) == (Status.QUEUED, rig.t0 + 2.0)

        rig.at(2)
        rig.engine.run_once(OPENCODE)

        assert rig.row(msg_id).next_attempt_at == rig.t0 + 4.0
        assert adapter.delivered == []
        assert len(warnings_naming(caplog, msg_id)) == 1

    def test_stale_binding_on_confirm_path_parks_handed_off_row(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter(
            outcomes=[result(Outcome.HANDED_OFF)], validate_error=BindingError("socket gone")
        )
        rig = make_rig(adapter)
        token = rig.register("bob", BOB_SOCKET)
        msg_id = rig.handed_off()
        rig.register("bob", BOB_STALE, token=token)

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at) == (Status.QUEUED, None)
        assert row.reason == "unreachable: socket gone; waits for bob to reconnect"
        assert rig.online(token) is False
        assert adapter.confirmed == []

    def test_unexpected_error_on_confirm_path_defers_handed_off_row(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter(
            outcomes=[result(Outcome.HANDED_OFF)], validate_error=RuntimeError("boom")
        )
        rig = make_rig(adapter)
        token = rig.register("bob", BOB_SOCKET)
        msg_id = rig.handed_off()
        rig.register("bob", BOB_STALE, token=token)

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at) == (Status.HANDED_OFF, rig.t0 + 1.0 + 2.0)
        assert adapter.confirmed == []


class TestConfirm:
    def test_confirmed_marks_handed_off_row_delivered(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter(
            outcomes=[result(Outcome.HANDED_OFF)], confirms=[result(Outcome.CONFIRMED)]
        )
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.handed_off()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "fake")
        assert [job.message.envelope.id for job in adapter.confirmed] == [msg_id]
        assert len(adapter.delivered) == 1

    def test_still_handed_off_schedules_next_confirm_and_keeps_handed_off_at(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter(
            outcomes=[result(Outcome.HANDED_OFF)], confirms=[result(Outcome.HANDED_OFF)]
        )
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.handed_off()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert row.status == Status.HANDED_OFF
        assert row.handed_off_at == rig.t0
        assert row.next_attempt_at == rig.t0 + 1.0 + 1.0

    def test_not_confirmed_on_confirm_takes_the_retry_path(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter(
            outcomes=[result(Outcome.HANDED_OFF)],
            confirms=[result(Outcome.NOT_CONFIRMED, "not seen within 600 s")],
        )
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.handed_off()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.attempts) == (Status.QUEUED, 1)
        assert row.next_attempt_at == rig.t0 + 1.0 + 2.0
        assert row.reason == "attempt 1 failed: not seen within 600 s"

    def test_unreachable_on_confirm_parks_row_and_takes_registration_offline(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter(
            outcomes=[result(Outcome.HANDED_OFF)],
            confirms=[result(Outcome.UNREACHABLE, "socket gone")],
        )
        rig = make_rig(adapter)
        token = rig.register("bob", BOB_SOCKET)
        msg_id = rig.handed_off()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at) == (Status.QUEUED, None)
        assert row.reason == "unreachable: socket gone; waits for bob to reconnect"
        assert rig.online(token) is False

    def test_ack_during_confirm_wins_over_late_confirmed(self, make_rig: MakeRig) -> None:
        rig: EngineRig
        acked: list[bool] = []

        class AckingConfirmAdapter(FakeAdapter):
            # The recipient acks while relayd is still polling the rollout (the Codex race).
            def confirm(self, job: DeliveryJob) -> DeliveryResult:
                acked.append(rig.ack(job.message.envelope.id))
                return super().confirm(job)

        adapter = AckingConfirmAdapter(
            outcomes=[result(Outcome.HANDED_OFF)], confirms=[result(Outcome.CONFIRMED)]
        )
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.handed_off()

        rig.engine.run_once(OPENCODE)

        assert acked == [True]
        assert rig.row(msg_id).status == Status.ACKED


class TestSweep:
    def leased(self, rig: EngineRig) -> str:
        msg_id = rig.queue()
        rig.move(msg_id, StatusChange(status=Status.FALLBACK), Status.QUEUED)
        leased = rig.store.lease_fallback("bob", rig.clock.now(), 30.0)
        assert leased is not None
        assert leased.envelope.id == msg_id
        return msg_id

    def delivering(self, rig: EngineRig) -> str:
        msg_id = rig.queue()
        claim = StatusChange(status=Status.DELIVERING, lease_until=rig.clock.now() + 120.0)
        rig.move(msg_id, claim, Status.QUEUED)
        return msg_id

    def test_expired_lease_returns_row_to_fallback(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter())
        msg_id = self.leased(rig)

        rig.at(29)
        assert rig.engine.sweep() == 0
        assert rig.row(msg_id).status == Status.LEASED
        rig.at(31)

        assert rig.engine.sweep() == 1
        assert rig.row(msg_id).status == Status.FALLBACK

    def test_stuck_delivering_claim_is_requeued_due_now_with_warning(
        self, make_rig: MakeRig, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="mz_relay")
        rig = make_rig(FakeAdapter())
        msg_id = self.delivering(rig)

        rig.at(119)
        assert rig.engine.sweep() == 0
        assert rig.row(msg_id).status == Status.DELIVERING
        rig.at(121)

        assert rig.engine.sweep() == 1
        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at) == (Status.QUEUED, rig.t0 + 121)
        assert warnings_naming(caplog, msg_id), "no WARNING names the requeued row"

    def test_recover_requeues_delivering_rows_due_now(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter())
        msg_id = self.delivering(rig)
        rig.at(5)

        assert rig.engine.recover() == 1

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at) == (Status.QUEUED, rig.t0 + 5)

    def test_leased_row_acked_before_sweep_stays_acked(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter())
        msg_id = self.leased(rig)
        assert rig.ack(msg_id)

        rig.at(31)
        rig.engine.sweep()

        assert rig.row(msg_id).status == Status.ACKED


# Far beyond any ttl, so lease and claim expiry stay out of the TTL tests.
FAR = 10.0**7
EXPIRING = [Status.QUEUED, Status.HANDED_OFF, Status.FALLBACK, Status.LEASED]


def row_in(rig: EngineRig, status: Status) -> str:
    """A request from alice to bob created at t0 with ttl 3600, moved to `status`."""
    if status is Status.HELD:
        return rig.queue(status=Status.HELD, due_in=None)
    msg_id = rig.queue(due_in=None)
    if status is not Status.QUEUED:
        change = StatusChange(status=status, lease_until=rig.t0 + FAR)
        rig.move(msg_id, change, Status.QUEUED)
    return msg_id


class TestTTL:
    @pytest.mark.parametrize("status", EXPIRING)
    def test_a_waiting_row_expires_one_second_after_its_ttl(
        self, make_rig: MakeRig, status: Status
    ) -> None:
        rig = make_rig(FakeAdapter())
        msg_id = row_in(rig, status)

        rig.at(3600)
        assert rig.engine.sweep() == 0
        assert rig.row(msg_id).status is status
        rig.at(3601)

        assert rig.engine.sweep() == 1
        row = rig.row(msg_id)
        assert (row.status, row.reason) == (Status.EXPIRED, "ttl")
        assert rig.engine.sweep() == 0

    @pytest.mark.parametrize(
        "status", [Status.HELD, Status.DELIVERING, Status.DELIVERED, Status.ACKED]
    )
    def test_held_in_flight_and_delivered_rows_never_expire(
        self, make_rig: MakeRig, status: Status
    ) -> None:
        rig = make_rig(FakeAdapter())
        msg_id = row_in(rig, status)
        rig.at(10.0**6)

        assert rig.engine.sweep() == 0

        assert rig.row(msg_id).status is status

    def test_each_row_expires_by_its_own_ttl(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter())
        # rig.queue mints ids from a small counter, so this one never collides with them.
        short = dataclasses.replace(envelope(mid(990_001), to="bob", at=rig.t0), ttl_s=60)
        rig.store.insert_message(short, Status.QUEUED, rig.t0, next_attempt_at=None)
        long = row_in(rig, Status.QUEUED)
        rig.at(61)

        rig.engine.sweep()

        assert rig.row(short.id).status is Status.EXPIRED
        assert rig.row(long).status is Status.QUEUED

    def test_a_row_acked_between_the_read_and_the_expiry_stays_acked(
        self, make_rig: MakeRig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = make_rig(FakeAdapter())
        msg_id = row_in(rig, Status.HANDED_OFF)
        # The wrapper forwards whatever Store.update gets, however the sweep calls it.
        update = cast("Callable[..., bool]", rig.store.update)
        raced: list[bool] = []

        def ack_first(*args: object, **kwargs: object) -> bool:
            changes = [a for a in (*args, *kwargs.values()) if isinstance(a, StatusChange)]
            if any(c.status is Status.EXPIRED for c in changes) and not raced:
                raced.append(rig.ack(msg_id))
            return update(*args, **kwargs)

        monkeypatch.setattr(rig.store, "update", ack_first)
        rig.at(3601)

        assert rig.engine.sweep() == 0

        assert raced == [True], "the sweep never wrote EXPIRED through Store.update"
        assert rig.row(msg_id).status is Status.ACKED


class TestSelection:
    def test_recipient_without_binding_goes_pull_only_and_leaves_wakeup(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.next_attempt_at) == (Status.QUEUED, None)
        assert row.reason == PULL_ONLY_REASON
        assert adapter.delivered == []
        assert rig.engine.next_wakeup(OPENCODE) is None

    def test_pull_only_row_is_not_revalidated_on_later_passes(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)
        rig.queue()
        rig.engine.run_once(OPENCODE)
        calls = adapter.validate_calls

        rig.at(5)
        rig.engine.run_once(OPENCODE)

        assert adapter.validate_calls == calls

    def test_wake_recipient_rearms_pull_only_row_once_a_binding_exists(
        self, make_rig: MakeRig
    ) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)
        msg_id = rig.queue()
        rig.engine.run_once(OPENCODE)
        rig.at(5)
        rig.register("bob", BOB_SOCKET)

        rig.store.wake_recipient("bob", rig.clock.now())
        rig.engine.run_once(OPENCODE)

        row = rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "fake")
        assert recipients(adapter.delivered) == ["bob"]

    def test_roster_binding_delivers_without_any_registration(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter(roster_binding=True)
        rig = make_rig(adapter)
        msg_id = rig.queue()

        rig.engine.run_once(OPENCODE)

        assert rig.row(msg_id).status == Status.DELIVERED
        assert adapter.delivered[0].binding.mode == "codex"

    def test_held_row_is_never_delivered(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)
        rig.register("bob", BOB_SOCKET)
        msg_id = rig.queue(status=Status.HELD, due_in=None)

        rig.engine.run_once(OPENCODE)
        rig.at(60)
        rig.engine.run_once(OPENCODE)

        assert adapter.delivered == []
        assert rig.row(msg_id).status == Status.HELD

    def test_native_registration_is_preferred_over_newer_fallback(self, make_rig: MakeRig) -> None:
        adapter = FakeAdapter()
        rig = make_rig(adapter)
        rig.register("bob", NATIVE, mode="native", age_s=100.0)
        rig.register("bob", REWAKE, mode="fallback")
        rig.queue()

        rig.engine.run_once(OPENCODE)

        assert [job.binding.data for job in adapter.delivered] == [NATIVE]

    def test_next_wakeup_is_earliest_due_time_of_that_harness(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter(), pi=FakeAdapter(), extra_agents=DAVE_PI)
        rig.queue("bob", due_in=5.0)
        to_carol = rig.queue("carol", due_in=50.0)
        claim = StatusChange(status=Status.DELIVERING, lease_until=rig.t0 + 120.0)
        rig.move(to_carol, claim, Status.QUEUED)
        handed = StatusChange(
            status=Status.HANDED_OFF, handed_off_at=rig.t0, next_attempt_at=rig.t0 + 3.0
        )
        rig.move(to_carol, handed, Status.DELIVERING)
        rig.queue("dave", due_in=1.0)
        rig.queue("bob", status=Status.HELD, due_in=None)

        assert rig.engine.next_wakeup(OPENCODE) == rig.t0 + 3.0
        assert rig.engine.next_wakeup(Harness.PI) == rig.t0 + 1.0

    def test_next_wakeup_is_none_when_idle(self, make_rig: MakeRig) -> None:
        rig = make_rig(FakeAdapter())
        rig.queue(status=Status.HELD, due_in=None)

        assert rig.engine.next_wakeup(OPENCODE) is None
