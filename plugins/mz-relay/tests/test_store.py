"""Store: schema and pragmas, message rows, and compare-and-set status writes."""

from __future__ import annotations

import functools
import sqlite3
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from mz_relay.envelope import Kind, Status
from mz_relay.errors import StoreError
from mz_relay.store import DuplicateMessage, StatusChange, Store
from relay_rig import PROC_WAIT_S, envelope, mid

# The schema has no time column, so a read rebuilds Envelope.time from created_at.
CREATED = 1_763_006_700.125


def raw(db: Path) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(db, isolation_level=None))


class RollbackFails(sqlite3.Connection):
    """A connection whose ROLLBACK fails, as on an I/O error; every other statement is real."""

    def execute(self, sql: str, *parameters: object) -> sqlite3.Cursor:
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("disk I/O error")
        # Store passes at most one parameter sequence; the cast only restates that for mypy.
        return super().execute(sql, *cast("tuple[Sequence[object], ...]", parameters))


class TestOpen:
    def test_database_is_in_wal_mode(self, store: Store, db: Path) -> None:
        with raw(db) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_schema_version_is_recorded_as_2(self, store: Store, db: Path) -> None:
        with raw(db) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()

        assert row == ("2",)

    def test_a_context_log_reads_through_the_context_index_without_sorting(
        self, store: Store, db: Path
    ) -> None:
        with raw(db) as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE context_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 5",
                ("ctx",),
            ).fetchall()

        details = " | ".join(str(row[-1]) for row in plan)
        assert "USING INDEX messages_context" in details
        assert "TEMP B-TREE" not in details

    def test_a_version_1_database_gains_the_context_index_and_keeps_its_rows(
        self, db: Path
    ) -> None:
        first = Store(db)
        env = envelope(mid(1), context_id=mid(1), at=CREATED)
        first.insert_message(env, Status.QUEUED, CREATED, next_attempt_at=CREATED)
        first.close()
        with raw(db) as conn:
            conn.execute("DROP INDEX messages_context")
            conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")

        second = Store(db)
        try:
            logged = second.log(mid(1), 5)
        finally:
            second.close()

        with raw(db) as conn:
            version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            index = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'messages_context'"
            ).fetchone()
        assert version == ("2",)
        assert index == ("messages_context",)
        assert [stored.envelope for stored in logged] == [env]

    def test_write_waits_for_a_lock_held_by_another_connection(
        self, store: Store, db: Path
    ) -> None:
        # busy_timeout lives on the Store's own connection, so it is checked by its effect.
        blocker = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        release = threading.Timer(0.5, blocker.rollback)
        release.start()
        started = time.monotonic()
        try:
            store.insert_message(envelope(mid(1)), Status.QUEUED, 10.0, next_attempt_at=10.0)
        finally:
            release.join(PROC_WAIT_S)
            blocker.close()

        assert time.monotonic() - started >= 0.4
        assert store.get("alice", mid(1)) is not None

    def test_reopening_keeps_rows(self, db: Path) -> None:
        first = Store(db)
        env = envelope(mid(1), at=CREATED)
        first.insert_message(env, Status.QUEUED, CREATED, next_attempt_at=CREATED)
        first.close()

        second = Store(db)
        try:
            stored = second.get("alice", mid(1))
        finally:
            second.close()

        assert stored is not None
        assert stored.envelope == env

    def test_newer_schema_is_refused(self, db: Path) -> None:
        Store(db).close()
        with raw(db) as conn:
            conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")

        with pytest.raises(StoreError) as caught:
            Store(db)

        assert caught.value.code == "store_invalid"
        assert caught.value.message == "database schema 99 is newer than supported 2"

    def test_a_failing_rollback_does_not_hide_why_the_transaction_failed(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Store(db).close()
        with raw(db) as conn:
            conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        connect = functools.partial(sqlite3.connect, factory=RollbackFails)
        monkeypatch.setattr(sqlite3, "connect", connect)

        with pytest.raises(StoreError) as caught:
            Store(db)

        assert caught.value.message == "database schema 99 is newer than supported 2"


class TestMessages:
    def test_inserted_message_reads_back_with_its_status_and_times(self, store: Store) -> None:
        env = envelope(mid(1), at=CREATED)
        store.insert_message(env, Status.QUEUED, CREATED, next_attempt_at=CREATED + 2)

        stored = store.get("alice", mid(1))

        assert stored is not None
        assert stored.envelope == env
        assert (stored.status, stored.next_attempt_at) == (Status.QUEUED, CREATED + 2)
        assert (stored.created_at, stored.updated_at) == (CREATED, CREATED)
        assert (stored.reason, stored.via, stored.attempts) == (None, None, 0)
        assert (stored.handed_off_at, stored.lease_until) == (None, None)

    def test_missing_message_reads_as_none(self, store: Store) -> None:
        assert store.get("alice", mid(1)) is None

    def test_same_source_and_id_twice_is_a_duplicate(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 10.0, next_attempt_at=10.0)

        with pytest.raises(DuplicateMessage):
            store.insert_message(envelope(mid(1)), Status.QUEUED, 11.0, next_attempt_at=11.0)

    def test_same_id_from_another_source_is_a_separate_row(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 10.0, next_attempt_at=10.0)
        store.insert_message(
            envelope(mid(1), source="carol"), Status.HELD, 11.0, next_attempt_at=None
        )

        alice_row, carol_row = store.get("alice", mid(1)), store.get("carol", mid(1))

        assert alice_row is not None

        assert alice_row.status is Status.QUEUED
        assert carol_row is not None
        assert carol_row.status is Status.HELD

    def test_due_returns_queued_rows_of_the_recipients_up_to_now_oldest_first(
        self, store: Store
    ) -> None:
        # Inserted newest first so an insertion-order result would be caught.
        store.insert_message(envelope(mid(2)), Status.QUEUED, 2.0, next_attempt_at=100.0)
        store.insert_message(envelope(mid(1)), Status.QUEUED, 1.0, next_attempt_at=90.0)
        store.insert_message(envelope(mid(3)), Status.HELD, 3.0, next_attempt_at=None)
        store.insert_message(envelope(mid(4)), Status.QUEUED, 4.0, next_attempt_at=None)
        store.insert_message(envelope(mid(5)), Status.QUEUED, 5.0, next_attempt_at=101.0)
        store.insert_message(envelope(mid(6), to="carol"), Status.QUEUED, 0.5, next_attempt_at=50.0)

        due = store.due(100.0, {"bob"})

        assert [m.envelope.id for m in due] == [mid(1), mid(2)]

    def test_due_honors_the_limit(self, store: Store) -> None:
        for n in range(1, 4):
            store.insert_message(envelope(mid(n)), Status.QUEUED, float(n), next_attempt_at=5.0)

        assert [m.envelope.id for m in store.due(10.0, {"bob"}, limit=2)] == [mid(1), mid(2)]

    def test_confirm_due_returns_handed_off_rows_up_to_now(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.HANDED_OFF, 1.0, next_attempt_at=100.0)
        store.insert_message(envelope(mid(2)), Status.HANDED_OFF, 2.0, next_attempt_at=101.0)
        store.insert_message(envelope(mid(3)), Status.QUEUED, 3.0, next_attempt_at=50.0)
        store.insert_message(
            envelope(mid(4), to="carol"), Status.HANDED_OFF, 4.0, next_attempt_at=50.0
        )

        assert [m.envelope.id for m in store.confirm_due(100.0, {"bob"})] == [mid(1)]

    def test_open_count_counts_undelivered_and_unacked_requests(self, store: Store) -> None:
        open_statuses = [
            Status.QUEUED,
            Status.DELIVERING,
            Status.HANDED_OFF,
            Status.FALLBACK,
            Status.LEASED,
            Status.DELIVERED,
        ]
        closed_statuses = [
            Status.HELD,
            Status.ACKED,
            Status.DENIED,
            Status.UNDELIVERABLE,
            Status.EXPIRED,
        ]
        for n, status in enumerate(open_statuses + closed_statuses, start=1):
            store.insert_message(envelope(mid(n)), status, float(n), next_attempt_at=None)
        # A delivered inform needs no ack, so it is no longer open.
        store.insert_message(
            envelope(mid(50), kind=Kind.INFORM), Status.DELIVERED, 50.0, next_attempt_at=None
        )
        store.insert_message(envelope(mid(51), to="carol"), Status.QUEUED, 51.0, next_attempt_at=1)

        assert store.open_count("bob") == len(open_statuses)

    def test_insert_counts_a_turn_on_the_message_context(self, store: Store, db: Path) -> None:
        store.insert_message(
            envelope(mid(1), context_id="ctx-a"), Status.QUEUED, 1.0, next_attempt_at=1.0
        )
        store.insert_message(
            envelope(mid(2), context_id="ctx-a"), Status.HELD, 2.0, next_attempt_at=None
        )
        store.insert_message(
            envelope(mid(3), context_id="ctx-b"), Status.QUEUED, 3.0, next_attempt_at=3.0
        )

        with raw(db) as conn:
            turns = dict(conn.execute("SELECT context_id, turns FROM contexts").fetchall())

        assert turns == {"ctx-a": 2, "ctx-b": 1}

    def test_wake_recipient_makes_only_parked_queued_rows_due(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 1.0, next_attempt_at=None)
        store.insert_message(envelope(mid(2)), Status.QUEUED, 2.0, next_attempt_at=None)
        store.insert_message(envelope(mid(3)), Status.QUEUED, 3.0, next_attempt_at=500.0)
        store.insert_message(envelope(mid(4)), Status.HELD, 4.0, next_attempt_at=None)
        store.insert_message(envelope(mid(5), to="carol"), Status.QUEUED, 5.0, next_attempt_at=None)

        woken = store.wake_recipient("bob", 200.0)

        def next_at(n: int, source: str = "alice") -> float | None:
            stored = store.get(source, mid(n))
            assert stored is not None
            return stored.next_attempt_at

        assert woken == 2
        assert [next_at(n) for n in range(1, 6)] == [200.0, 200.0, 500.0, None, None]

    def test_find_by_id_with_recipient_skips_rows_addressed_to_others(self, store: Store) -> None:
        store.insert_message(
            envelope(mid(1), to="carol"), Status.DELIVERED, 1.0, next_attempt_at=None
        )

        assert store.find_by_id(mid(1), recipient="bob") is None
        found = store.find_by_id(mid(1), recipient="carol")
        assert found is not None
        assert found.envelope.to == "carol"

    def test_find_by_id_returns_the_newest_row_sharing_the_id(self, store: Store) -> None:
        store.insert_message(
            envelope(mid(1), source="carol"), Status.QUEUED, 20.0, next_attempt_at=20.0
        )
        store.insert_message(
            envelope(mid(1), source="alice"), Status.QUEUED, 10.0, next_attempt_at=10.0
        )

        found = store.find_by_id(mid(1))

        assert found is not None

        assert found.envelope.source == "carol"

    def test_find_by_id_of_an_unknown_id_is_none(self, store: Store) -> None:
        assert store.find_by_id(mid(9)) is None

    def test_with_status_filters_by_status_set_and_recipient(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.HELD, 1.0, next_attempt_at=None)
        store.insert_message(envelope(mid(2)), Status.QUEUED, 2.0, next_attempt_at=2.0)
        store.insert_message(envelope(mid(3), to="carol"), Status.HELD, 3.0, next_attempt_at=None)

        held_any = {m.envelope.id for m in store.with_status(frozenset({Status.HELD}))}
        held_bob = store.with_status(frozenset({Status.HELD, Status.DENIED}), recipient="bob")

        assert held_any == {mid(1), mid(3)}
        assert [m.envelope.id for m in held_bob] == [mid(1)]

    def test_counts_tally_rows_per_status(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 1.0, next_attempt_at=1.0)
        store.insert_message(envelope(mid(2)), Status.QUEUED, 2.0, next_attempt_at=2.0)
        store.insert_message(envelope(mid(3)), Status.HELD, 3.0, next_attempt_at=None)

        counts = store.counts()

        assert (counts["queued"], counts["held"], counts.get("acked", 0)) == (2, 1, 0)


class TestCas:
    @pytest.fixture
    def delivering(self, store: Store) -> Store:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 10.0, next_attempt_at=10.0)
        assert store.update(
            "alice",
            mid(1),
            StatusChange(Status.DELIVERING, lease_until=140.0),
            20.0,
            expected=Status.QUEUED,
        )
        return store

    def test_write_against_a_stale_expected_status_changes_nothing(self, delivering: Store) -> None:
        written = delivering.update(
            "alice",
            mid(1),
            StatusChange(Status.DELIVERED, via="inbox"),
            30.0,
            expected=Status.QUEUED,
        )

        stored = delivering.get("alice", mid(1))
        assert written is False
        assert stored is not None
        assert (stored.status, stored.updated_at, stored.via) == (Status.DELIVERING, 20.0, None)

    def test_successful_write_sets_status_and_updated_at(self, delivering: Store) -> None:
        stored = delivering.get("alice", mid(1))

        assert stored is not None
        assert (stored.status, stored.updated_at, stored.lease_until) == (
            Status.DELIVERING,
            20.0,
            140.0,
        )

    @pytest.mark.parametrize("current", [Status.DELIVERING, Status.HANDED_OFF])
    def test_expected_set_matches_any_of_its_statuses(self, store: Store, current: Status) -> None:
        store.insert_message(envelope(mid(1)), current, 10.0, next_attempt_at=None)

        written = store.update(
            "alice",
            mid(1),
            StatusChange(Status.ACKED),
            30.0,
            expected=frozenset({Status.DELIVERING, Status.HANDED_OFF}),
        )

        stored = store.get("alice", mid(1))
        assert written is True
        assert stored is not None
        assert stored.status is Status.ACKED

    def test_missing_row_is_not_written(self, store: Store) -> None:
        written = store.update(
            "alice", mid(1), StatusChange(Status.DELIVERING), 30.0, expected=Status.QUEUED
        )

        assert written is False
        assert store.get("alice", mid(1)) is None

    def test_none_keeps_via_attempts_and_handed_off_at_but_clears_the_rest(
        self, store: Store
    ) -> None:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 10.0, next_attempt_at=10.0)
        assert store.update(
            "alice",
            mid(1),
            StatusChange(
                Status.HANDED_OFF,
                reason="attempt 1 failed: busy",
                via="fake",
                attempts=2,
                next_attempt_at=21.0,
                handed_off_at=20.0,
                lease_until=140.0,
            ),
            20.0,
            expected=Status.QUEUED,
        )

        assert store.update(
            "alice", mid(1), StatusChange(Status.QUEUED), 30.0, expected=Status.HANDED_OFF
        )

        stored = store.get("alice", mid(1))
        assert stored is not None
        assert (stored.via, stored.attempts, stored.handed_off_at) == ("fake", 2, 20.0)
        assert (stored.reason, stored.next_attempt_at, stored.lease_until) == (None, None, None)

    def test_given_attempts_overwrite_the_stored_count(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 10.0, next_attempt_at=10.0)

        assert store.update(
            "alice", mid(1), StatusChange(Status.QUEUED, attempts=3), 20.0, expected=Status.QUEUED
        )

        stored = store.get("alice", mid(1))
        assert stored is not None
        assert stored.attempts == 3

    def test_concurrent_claims_of_one_row_let_exactly_one_win(self, store: Store) -> None:
        store.insert_message(envelope(mid(1)), Status.QUEUED, 10.0, next_attempt_at=10.0)
        barrier = threading.Barrier(8, timeout=PROC_WAIT_S)

        def claim(_: int) -> bool:
            barrier.wait()
            return store.update(
                "alice", mid(1), StatusChange(Status.DELIVERING), 20.0, expected=Status.QUEUED
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))

        assert results.count(True) == 1
