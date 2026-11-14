"""The delivery engine: claims due rows, hands them to harness adapters and applies outcomes."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, assert_never

from mz_relay.clock import Clock
from mz_relay.envelope import Status, render_for_delivery
from mz_relay.errors import BindingError
from mz_relay.roster import AgentSpec, Harness, Roster
from mz_relay.store import (
    UNREACHABLE_PREFIX,
    Registration,
    StatusChange,
    Store,
    StoredMessage,
)

log = logging.getLogger(__name__)

BACKOFF_S = (0.0, 2.0, 8.0)
MAX_ATTEMPTS = 3
# HANDED_OFF rows reuse next_attempt_at as their next confirm time.
CONFIRM_EVERY_S = 1.0
LEASE_S = 30.0
# Longer than any adapter timeout, so a live claim is never swept from under the adapter.
CLAIM_S = 120.0
DEFER_S = 2.0
WAKEUP_STATUSES = frozenset({Status.QUEUED, Status.HANDED_OFF})
NO_BINDING_DETAIL = "no live binding"
# HELD waits for the user and DELIVERING for an adapter call in flight, so neither expires.
EXPIRING = frozenset({Status.QUEUED, Status.HANDED_OFF, Status.FALLBACK, Status.LEASED})
TTL_REASON = "ttl"


class Outcome(StrEnum):
    CONFIRMED = "confirmed"
    HANDED_OFF = "handed_off"
    NOT_CONFIRMED = "not_confirmed"
    UNREACHABLE = "unreachable"
    FALLBACK = "fallback"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class DeliveryResult:
    outcome: Outcome
    detail: str = ""


@dataclass(frozen=True)
class DeliveryBinding:
    mode: str
    data: Mapping[str, object]


@dataclass(frozen=True)
class DeliveryJob:
    message: StoredMessage
    text: str
    spec: AgentSpec
    binding: DeliveryBinding
    now: float


class HarnessAdapter(Protocol):
    exhausted_status: Status
    via: str

    # Raises BindingError for a broken binding. An adapter whose validate_binding(spec, None)
    # returns a roster binding never returns UNREACHABLE.
    def validate_binding(
        self, spec: AgentSpec, binding: Mapping[str, object] | None
    ) -> DeliveryBinding | None: ...

    def deliver(self, job: DeliveryJob) -> DeliveryResult: ...

    def confirm(self, job: DeliveryJob) -> DeliveryResult: ...


@dataclass(frozen=True)
class EngineDeps:
    store: Store
    roster: Callable[[], Roster]
    adapters: Mapping[Harness, HarnessAdapter]
    clock: Clock


def pull_only_reason(to: str) -> str:
    return f"pull-only: {to} has no wake-up channel; waits for its inbox call"


def unreachable_reason(to: str, detail: str) -> str:
    return f"{UNREACHABLE_PREFIX} {detail}; waits for {to} to reconnect"


def _same_route(a: Registration, b: Registration) -> bool:
    # last_seen and online change on every heartbeat; only these say where delivery goes.
    return (a.token, a.delivery_mode, a.binding) == (b.token, b.delivery_mode, b.binding)


def retry_reason(attempt: int, detail: str) -> str:
    return f"attempt {attempt} failed: {detail}"


@dataclass(frozen=True)
class _Attempt:
    """A row being handled, its recipient, and the registration and binding it goes through."""

    row: StoredMessage
    spec: AgentSpec
    adapter: HarnessAdapter
    registration: Registration | None
    binding: DeliveryBinding | None


class DeliveryEngine:
    """Moves QUEUED and HANDED_OFF rows forward; every status write is a compare-and-set."""

    def __init__(self, deps: EngineDeps) -> None:
        if not isinstance(deps, EngineDeps):
            raise ValueError(f"deps must be EngineDeps, got {type(deps).__name__}")
        self._deps = deps
        # Rows already warned about, so a persistent fault logs one WARNING, not one per pass.
        self._warned: set[tuple[str, str]] = set()
        # Rows deferred before, so a long busy turn logs one INFO line per row, not one per pass.
        self._deferred: set[tuple[str, str]] = set()

    def run_once(self, harness: Harness) -> int:
        """Delivers due QUEUED rows and confirms due HANDED_OFF rows of the harness's recipients."""
        adapter = self._adapter(harness)
        roster = self._deps.roster()
        names = [spec.name for spec in roster.agents.values() if spec.harness == harness]
        store, now = self._deps.store, self._deps.clock.now()
        handled = 0
        for row in store.due(now, names):
            handled += self._guarded(roster, adapter, row)
        for row in store.confirm_due(now, names):
            handled += self._guarded(roster, adapter, row)
        return handled

    def sweep(self) -> int:
        """Expires rows past their ttl, returns lapsed leases to FALLBACK, requeues stale claims."""
        store, now = self._deps.store, self._deps.clock.now()
        swept = 0
        for row in store.past_ttl(EXPIRING, now):
            change = StatusChange(Status.EXPIRED, reason=TTL_REASON)
            if self._write(row, change, row.status, "ttl expiry"):
                log.info(
                    "%s to %s expired after its ttl of %d s",
                    row.envelope.id,
                    row.envelope.to,
                    row.envelope.ttl_s,
                )
                swept += 1
        for row in store.with_status(frozenset({Status.LEASED})):
            if row.lease_until is not None and row.lease_until <= now:
                change = StatusChange(Status.FALLBACK)
                if self._write(row, change, Status.LEASED, "lease expiry"):
                    log.info(
                        "lease of %s to %s expired; back to fallback",
                        row.envelope.id,
                        row.envelope.to,
                    )
                    swept += 1
        for row in store.with_status(frozenset({Status.DELIVERING})):
            if row.lease_until is not None and row.lease_until <= now:
                change = StatusChange(Status.QUEUED, next_attempt_at=now)
                if self._write(row, change, Status.DELIVERING, "claim expiry"):
                    log.warning(
                        "claim on %s to %s expired after %.0f s; requeued",
                        row.envelope.id,
                        row.envelope.to,
                        CLAIM_S,
                    )
                    swept += 1
        return swept

    def next_wakeup(self, harness: Harness) -> float | None:
        """The earliest absolute time a QUEUED or HANDED_OFF row of the harness is due."""
        if not isinstance(harness, Harness):
            raise ValueError(f"harness must be a Harness, got {harness!r}")
        names = [
            spec.name for spec in self._deps.roster().agents.values() if spec.harness == harness
        ]
        return self._deps.store.earliest_due(WAKEUP_STATUSES, names)

    def recover(self) -> int:
        """Requeues rows a previous daemon left DELIVERING; run once at daemon start."""
        return self._deps.store.requeue_inflight(self._deps.clock.now())

    def _adapter(self, harness: Harness) -> HarnessAdapter:
        if not isinstance(harness, Harness):
            raise ValueError(f"harness must be a Harness, got {harness!r}")
        adapter = self._deps.adapters.get(harness)
        if adapter is None:
            raise ValueError(f"no delivery adapter for {harness}")
        return adapter

    def _guarded(self, roster: Roster, adapter: HarnessAdapter, row: StoredMessage) -> int:
        # Worker boundary: one bad row must not skip the rest of the harness's rows.
        try:
            if row.status is Status.HANDED_OFF:
                return self._confirm(roster, adapter, row)
            return self._deliver(roster, adapter, row)
        except Exception as exc:
            self._defer_faulty(row, exc)
            return 0

    def _deliver(self, roster: Roster, adapter: HarnessAdapter, row: StoredMessage) -> int:
        attempt = self._bind(roster, adapter, row)
        if attempt is None:
            return 1
        env = row.envelope
        if attempt.binding is None:
            change = StatusChange(Status.QUEUED, reason=pull_only_reason(env.to))
            if self._write(row, change, Status.QUEUED, "pull-only parking"):
                log.info("%s to %s is pull-only: no wake-up channel", env.id, env.to)
                self._rearm_if_registered(row, attempt.registration)
            return 1
        text = render_for_delivery(env, roster.get(env.source))
        now = self._deps.clock.now()
        claim = StatusChange(Status.DELIVERING, lease_until=now + CLAIM_S)
        if not self._write(row, claim, Status.QUEUED, "claim"):
            return 0
        job = DeliveryJob(
            message=row, text=text, spec=attempt.spec, binding=attempt.binding, now=now
        )
        try:
            result = adapter.deliver(job)
        except Exception as exc:
            log.exception("adapter %s raised delivering %s to %s", adapter.via, env.id, env.to)
            result = DeliveryResult(Outcome.NOT_CONFIRMED, f"internal: {type(exc).__name__}")
        self._apply(attempt, result, Status.DELIVERING)
        return 1

    def _confirm(self, roster: Roster, adapter: HarnessAdapter, row: StoredMessage) -> int:
        attempt = self._bind(roster, adapter, row)
        if attempt is None:
            return 1
        env = row.envelope
        if attempt.binding is None:
            self._unreachable(attempt, NO_BINDING_DETAIL, Status.HANDED_OFF)
            return 1
        text = render_for_delivery(env, roster.get(env.source))
        job = DeliveryJob(
            message=row,
            text=text,
            spec=attempt.spec,
            binding=attempt.binding,
            now=self._deps.clock.now(),
        )
        try:
            result = adapter.confirm(job)
        except Exception as exc:
            log.exception("adapter %s raised confirming %s to %s", adapter.via, env.id, env.to)
            result = DeliveryResult(Outcome.NOT_CONFIRMED, f"internal: {type(exc).__name__}")
        self._apply(attempt, result, Status.HANDED_OFF)
        return 1

    def _bind(self, roster: Roster, adapter: HarnessAdapter, row: StoredMessage) -> _Attempt | None:
        """The attempt with its binding; None when a stale stored binding already parked the row."""
        spec = roster.get(row.envelope.to)
        reg = self._deps.store.latest_delivery_registration(spec.name)
        attempt = _Attempt(row=row, spec=spec, adapter=adapter, registration=reg, binding=None)
        if reg is None:
            return replace(attempt, binding=adapter.validate_binding(spec, None))
        try:
            return replace(attempt, binding=adapter.validate_binding(spec, reg.binding))
        except BindingError as exc:
            log.info(
                "stored %s binding of %s failed validation for %s: %s",
                reg.delivery_mode,
                spec.name,
                row.envelope.id,
                exc,
            )
            self._unreachable(attempt, str(exc), row.status)
            return None

    def _apply(self, attempt: _Attempt, result: DeliveryResult, expected: Status) -> None:
        row, now = attempt.row, self._deps.clock.now()
        what = f"outcome {result.outcome}"
        key = (row.envelope.source, row.envelope.id)
        deferred_before = key in self._deferred
        if result.outcome is Outcome.DEFERRED:
            self._deferred.add(key)
        else:
            self._deferred.discard(key)
        match result.outcome:
            case Outcome.CONFIRMED:
                change = StatusChange(Status.DELIVERED, via=attempt.adapter.via)
            case Outcome.HANDED_OFF:
                # A confirm that is still waiting keeps the original hand-off time.
                handed_off_at = now if expected is Status.DELIVERING else None
                change = StatusChange(
                    Status.HANDED_OFF,
                    next_attempt_at=now + CONFIRM_EVERY_S,
                    handed_off_at=handed_off_at,
                )
            case Outcome.NOT_CONFIRMED:
                change = self._retry(attempt, result.detail, now)
            case Outcome.UNREACHABLE:
                self._unreachable(attempt, result.detail, expected)
                return
            case Outcome.FALLBACK:
                change = StatusChange(Status.FALLBACK)
            case Outcome.DEFERRED:
                change = StatusChange(Status.QUEUED, next_attempt_at=now + DEFER_S)
            case _:
                assert_never(result.outcome)
        if not self._write(row, change, expected, what):
            return
        env = row.envelope
        if result.outcome is Outcome.DEFERRED and deferred_before:
            log.debug("%s for %s to %s again: now %s", what, env.id, env.to, change.status)
        elif (
            change.status is attempt.adapter.exhausted_status
            and result.outcome is Outcome.NOT_CONFIRMED
        ):
            log.warning(
                "%s to %s failed %d attempts; now %s", env.id, env.to, MAX_ATTEMPTS, change.status
            )
        else:
            log.info("%s for %s to %s: now %s", what, env.id, env.to, change.status)

    def _retry(self, attempt: _Attempt, detail: str, now: float) -> StatusChange:
        attempts = attempt.row.attempts + 1
        if attempts < MAX_ATTEMPTS:
            return StatusChange(
                Status.QUEUED,
                reason=retry_reason(attempts, detail),
                attempts=attempts,
                next_attempt_at=now + BACKOFF_S[attempts],
            )
        return StatusChange(
            attempt.adapter.exhausted_status, reason=detail or None, attempts=attempts
        )

    def _unreachable(self, attempt: _Attempt, detail: str, expected: Status) -> None:
        store, now = self._deps.store, self._deps.clock.now()
        env = attempt.row.envelope
        if attempt.registration is not None:
            store.set_online(attempt.registration.token, False, now)
            log.info(
                "%s registration of %s is unreachable; marked offline",
                attempt.registration.delivery_mode,
                env.to,
            )
        # Each UNREACHABLE takes one registration offline, so re-arming at once cannot loop.
        if store.latest_delivery_registration(env.to) is not None:
            change = StatusChange(Status.QUEUED, next_attempt_at=now)
        else:
            change = StatusChange(Status.QUEUED, reason=unreachable_reason(env.to, detail))
        if self._write(attempt.row, change, expected, f"outcome {Outcome.UNREACHABLE}"):
            parked = change.next_attempt_at is None
            log.info("%s to %s unreachable; %s", env.id, env.to, "parked" if parked else "re-armed")
            if parked:
                # The bound registration is offline now, so any registration found is a new one.
                self._rearm_if_registered(attempt.row, None)

    def _rearm_if_registered(self, row: StoredMessage, seen: Registration | None) -> None:
        # A register that committed before the park woke nothing, since the row was not parked yet.
        env = row.envelope
        latest = self._deps.store.latest_delivery_registration(env.to)
        if latest is None or (seen is not None and _same_route(latest, seen)):
            return
        change = StatusChange(Status.QUEUED, next_attempt_at=self._deps.clock.now())
        if self._write(row, change, Status.QUEUED, "re-arm after parking"):
            log.info("%s to %s registered while parking; re-armed", env.id, env.to)

    def _defer_faulty(self, row: StoredMessage, exc: Exception) -> None:
        env = row.envelope
        key = (env.source, env.id)
        if key in self._warned:
            log.debug("%s to %s failed again: %s", env.id, env.to, type(exc).__name__)
        else:
            self._warned.add(key)
            log.warning(
                "handling %s to %s raised %s; retrying every %.0f s",
                env.id,
                env.to,
                type(exc).__name__,
                DEFER_S,
                exc_info=exc,
            )
        now = self._deps.clock.now()
        change = StatusChange(row.status, reason=row.reason, next_attempt_at=now + DEFER_S)
        try:
            self._write(row, change, row.status, "deferral")
        except Exception:
            log.exception("cannot defer %s to %s", env.id, env.to)

    def _write(self, row: StoredMessage, change: StatusChange, expected: Status, what: str) -> bool:
        env = row.envelope
        now = self._deps.clock.now()
        if self._deps.store.update(env.source, env.id, change, now, expected=expected):
            return True
        current = self._deps.store.get(env.source, env.id)
        status = current.status if current is not None else "missing"
        log.info("stale %s for %s: row is %s", what, env.id, status)
        return False
