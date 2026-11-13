"""Store: atomic inbox pulls and waiter leases, plus crash durability."""

from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from conftest import SRC, TESTS
from mz_relay.envelope import Kind, Status
from mz_relay.store import Store, StoredMessage
from relay_rig import PROC_WAIT_S, envelope, mid

NOW = 200.0


def put(
    store: Store, n: int, status: Status, *, to: str = "bob", kind: Kind = Kind.REQUEST
) -> None:
    # created_at follows n, so a lower n is always the older row.
    next_at = float(n) if status is Status.QUEUED else None
    env = envelope(mid(n), to=to, kind=kind, at=float(n))
    store.insert_message(env, status, float(n), next_attempt_at=next_at)


def row(store: Store, n: int) -> StoredMessage:
    stored = store.get("alice", mid(n))
    assert stored is not None
    return stored


def ids(messages: list[StoredMessage]) -> list[str]:
    return [m.envelope.id for m in messages]


def race_lease_and_pull(store: Store) -> tuple[bool, bool]:
    barrier = threading.Barrier(2, timeout=PROC_WAIT_S)

    def lease() -> bool:
        barrier.wait()
        return store.lease_fallback("bob", NOW, 30.0) is not None

    def pull() -> bool:
        barrier.wait()
        return store.pull_for("bob", 10, NOW) != []

    with ThreadPoolExecutor(max_workers=2) as pool:
        leased, pulled = pool.submit(lease), pool.submit(pull)
        return leased.result(), pulled.result()


class TestPullLease:
    def test_pull_delivers_the_oldest_queued_and_fallback_rows_via_inbox(
        self, store: Store
    ) -> None:
        put(store, 1, Status.FALLBACK)
        put(store, 2, Status.QUEUED)

        pulled = store.pull_for("bob", 10, NOW)

        assert ids(pulled) == [mid(1), mid(2)]
        for n in (1, 2):
            stored = row(store, n)
            assert (stored.status, stored.via, stored.updated_at) == (
                Status.DELIVERED,
                "inbox",
                NOW,
            )

    def test_pull_skips_rows_in_flight_held_or_finished(self, store: Store) -> None:
        skipped = [
            Status.DELIVERING,
            Status.HANDED_OFF,
            Status.LEASED,
            Status.HELD,
            Status.DELIVERED,
            Status.ACKED,
            Status.DENIED,
            Status.UNDELIVERABLE,
            Status.EXPIRED,
        ]
        for n, status in enumerate(skipped, start=1):
            put(store, n, status)

        assert store.pull_for("bob", 50, NOW) == []
        assert [row(store, n).status for n in range(1, len(skipped) + 1)] == skipped

    def test_pull_takes_only_the_recipients_rows(self, store: Store) -> None:
        put(store, 1, Status.QUEUED, to="carol")
        put(store, 2, Status.QUEUED)

        assert ids(store.pull_for("bob", 10, NOW)) == [mid(2)]
        assert row(store, 1).status is Status.QUEUED

    def test_pull_honors_the_limit_and_leaves_the_rest_for_the_next_call(
        self, store: Store
    ) -> None:
        for n in (1, 2, 3):
            put(store, n, Status.QUEUED)

        first = store.pull_for("bob", 2, NOW)
        second = store.pull_for("bob", 2, NOW)

        assert (ids(first), ids(second)) == ([mid(1), mid(2)], [mid(3)])
        assert row(store, 3).status is Status.DELIVERED

    def test_second_pull_returns_nothing(self, store: Store) -> None:
        put(store, 1, Status.QUEUED)
        store.pull_for("bob", 10, NOW)

        assert store.pull_for("bob", 10, NOW) == []

    @pytest.mark.parametrize("connections", ["shared", "separate"])
    def test_two_threads_pulling_ten_rows_get_every_row_exactly_once(
        self, store: Store, db: Path, connections: str
    ) -> None:
        for n in range(1, 11):
            put(store, n, Status.QUEUED)
        other = store if connections == "shared" else Store(db)
        barrier = threading.Barrier(2, timeout=PROC_WAIT_S)

        def drain(puller: Store) -> list[str]:
            barrier.wait()
            got: list[str] = []
            while batch := puller.pull_for("bob", 2, NOW):
                got.extend(ids(batch))
            return got

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(drain, [store, other]))
        finally:
            if other is not store:
                other.close()

        assert sorted(results[0] + results[1]) == [mid(n) for n in range(1, 11)]

    def test_lease_takes_the_oldest_fallback_row(self, store: Store) -> None:
        put(store, 1, Status.QUEUED)
        put(store, 2, Status.FALLBACK)
        put(store, 3, Status.FALLBACK)

        leased = store.lease_fallback("bob", NOW, 30.0)

        assert leased is not None

        assert leased.envelope.id == mid(2)
        stored = row(store, 2)
        assert (stored.status, stored.lease_until) == (Status.LEASED, NOW + 30.0)
        assert row(store, 3).status is Status.FALLBACK

    def test_leased_row_is_not_pulled_by_inbox(self, store: Store) -> None:
        put(store, 1, Status.FALLBACK)
        store.lease_fallback("bob", NOW, 30.0)

        assert store.pull_for("bob", 10, NOW) == []
        assert row(store, 1).status is Status.LEASED

    def test_lease_without_a_fallback_row_is_none(self, store: Store) -> None:
        put(store, 1, Status.QUEUED)
        put(store, 2, Status.FALLBACK, to="carol")

        assert store.lease_fallback("bob", NOW, 30.0) is None

    def test_waiter_and_inbox_racing_for_one_fallback_row_never_both_get_it(
        self, store: Store
    ) -> None:
        for n in range(1, 21):
            put(store, n, Status.FALLBACK)

            leased, pulled = race_lease_and_pull(store)

            assert (leased, pulled) in ((True, False), (False, True))
            assert row(store, n).status is (Status.LEASED if leased else Status.DELIVERED)

    def test_requeue_inflight_makes_delivering_rows_due_now(self, store: Store) -> None:
        put(store, 1, Status.DELIVERING)
        put(store, 2, Status.DELIVERING)
        put(store, 3, Status.HANDED_OFF)

        requeued = store.requeue_inflight(NOW)

        assert requeued == 2
        for n in (1, 2):
            stored = row(store, n)
            assert (stored.status, stored.next_attempt_at) == (Status.QUEUED, NOW)
        assert row(store, 3).status is Status.HANDED_OFF
        assert ids(store.due(NOW, {"bob"})) == [mid(1), mid(2)]

    def test_awaiting_ack_lists_delivered_requests_not_yet_acked(self, store: Store) -> None:
        put(store, 1, Status.DELIVERED)
        put(store, 2, Status.HANDED_OFF)
        put(store, 3, Status.LEASED)
        put(store, 4, Status.DELIVERED, kind=Kind.INFORM)
        put(store, 5, Status.ACKED)
        put(store, 6, Status.QUEUED)
        put(store, 7, Status.DELIVERING)
        put(store, 8, Status.DELIVERED, to="carol")

        assert set(ids(store.awaiting_ack("bob"))) == {mid(1), mid(2), mid(3)}

    def test_sent_by_lists_the_senders_rows_up_to_the_limit(self, store: Store) -> None:
        for n in range(1, 6):
            put(store, n, Status.QUEUED)
        store.insert_message(
            envelope(mid(9), source="carol", to="alice"), Status.HELD, 9.0, next_attempt_at=None
        )

        everything = ids(store.sent_by("alice"))
        limited = ids(store.sent_by("alice", limit=3))

        assert sorted(everything) == [mid(n) for n in range(1, 6)]
        assert len(limited) == 3
        assert set(limited) <= set(everything)


# Writes 200 rows slowly, so the test can kill it mid-stream.
CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path[:0] = sys.argv[1:3]
    from pathlib import Path
    from mz_relay.envelope import Status
    from mz_relay.store import Store
    from relay_rig import envelope, mid

    store = Store(Path(sys.argv[3]))
    for n in range(200):
        env = envelope(mid(n), at=float(n))
        store.insert_message(env, Status.QUEUED, float(n), next_attempt_at=float(n))
        time.sleep(0.01)
    """
)


def committed_rows(db: Path) -> int:
    if not db.exists():
        return 0
    try:
        # Read-only, so the poller never creates the file or takes a write lock.
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)) as conn:
            return int(conn.execute("SELECT count(*) FROM messages").fetchone()[0])
    except sqlite3.OperationalError:
        # The child has not created the schema yet.
        return 0


class TestDurability:
    def test_rows_committed_before_a_sigkill_survive_intact(self, db: Path) -> None:
        child = subprocess.Popen([sys.executable, "-c", CHILD, str(SRC), str(TESTS), str(db)])
        try:
            deadline = time.monotonic() + 15.0
            while committed_rows(db) < 50:
                assert child.poll() is None, "writer exited before it was killed"
                assert time.monotonic() < deadline, "writer never committed 50 rows"
                time.sleep(0.005)
        finally:
            child.kill()
            child.wait(timeout=PROC_WAIT_S)
        assert child.returncode == -signal.SIGKILL

        reopened = Store(db)
        try:
            first = reopened.get("alice", mid(0))
        finally:
            reopened.close()
        with closing(sqlite3.connect(db)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            broken = conn.execute(
                "SELECT count(*) FROM messages WHERE body IS NULL OR status IS NULL"
            ).fetchone()[0]

        assert integrity == "ok"
        assert count >= 50
        assert broken == 0
        assert first is not None
        assert (first.status, first.envelope.body) == (Status.QUEUED, f"body of {mid(0)}")
