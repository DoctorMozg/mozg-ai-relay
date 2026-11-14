"""The event feed: triggers record message and presence changes, and the watch op reads them."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from mz_relay.envelope import Status
from mz_relay.roster import Harness
from mz_relay.store import EVENTS_KEPT, Registration, StatusChange, Store
from relay_rig import InThread, ServiceRig, envelope, mid

BODY = "secret body text that must not reach the event feed"
PROMPT_S = 2.0
# Lets a watching thread reach its wait before the write it waits for.
PARK_S = 0.2


def raw(db: Path) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(db, isolation_level=None))


def queued(store: Store, n: int, at: float = 10.0) -> None:
    store.insert_message(envelope(mid(n), body=BODY, at=at), Status.QUEUED, at, next_attempt_at=at)


def registration(token: str, name: str, online: bool, at: float = 20.0) -> Registration:
    return Registration(
        token=token,
        name=name,
        harness=Harness.OPENCODE,
        client="mcp",
        delivery_mode=None,
        binding=None,
        pid=1,
        created_at=at,
        last_seen=at,
        online=online,
    )


def kinds_and_statuses(store: Store) -> list[tuple[str, str | None]]:
    return [(e.kind, e.status) for e in store.events_after(0, 100).events]


class TestMessageEvents:
    def test_insert_and_each_status_change_append_one_event_in_order(self, store: Store) -> None:
        queued(store, 1)
        store.update("alice", mid(1), StatusChange(Status.DELIVERING), 11.0, expected=Status.QUEUED)
        store.update(
            "alice", mid(1), StatusChange(Status.DELIVERED), 12.0, expected=Status.DELIVERING
        )

        events = store.events_after(0, 100).events

        assert [(e.status, e.at) for e in events] == [
            ("queued", 10.0),
            ("delivering", 11.0),
            ("delivered", 12.0),
        ]
        assert {(e.source, e.id, e.recipient) for e in events} == {("alice", mid(1), "bob")}

    def test_a_write_that_keeps_the_status_appends_nothing(self, store: Store) -> None:
        queued(store, 1)
        store.update(
            "alice",
            mid(1),
            StatusChange(Status.QUEUED, reason="unreachable: socket gone"),
            11.0,
            expected=Status.QUEUED,
        )

        assert kinds_and_statuses(store) == [("message", "queued")]

    def test_no_event_row_holds_the_body(self, store: Store, db: Path) -> None:
        queued(store, 1)
        store.update("alice", mid(1), StatusChange(Status.DELIVERED), 11.0, expected=Status.QUEUED)

        with raw(db) as conn:
            rows = conn.execute("SELECT * FROM events").fetchall()

        assert rows
        assert all(BODY not in str(value) for row in rows for value in row)


class TestPresenceEvents:
    def test_registering_online_then_going_offline_records_both(self, store: Store) -> None:
        store.upsert_registration(registration("t1", "bob", online=True))
        store.set_online("t1", False, 30.0)

        events = store.events_after(0, 100).events

        assert [(e.kind, e.agent, e.online) for e in events] == [
            ("presence", "bob", True),
            ("presence", "bob", False),
        ]

    def test_an_agent_with_another_live_registration_stays_online(self, store: Store) -> None:
        store.upsert_registration(registration("t1", "bob", online=True))
        store.upsert_registration(registration("t2", "bob", online=True))
        store.set_online("t1", False, 30.0)

        assert store.events_after(0, 100).events[-1].online is True

    def test_marking_all_offline_records_each_agent(self, store: Store) -> None:
        store.upsert_registration(registration("t1", "alice", online=True))
        store.upsert_registration(registration("t2", "bob", online=True))
        store.mark_all_offline()

        offline = [str(e.agent) for e in store.events_after(2, 100).events if e.online is False]

        assert sorted(offline) == ["alice", "bob"]


class TestEventPages:
    def test_no_since_answers_the_head_and_no_events(self, store: Store) -> None:
        queued(store, 1)
        queued(store, 2)

        page = store.events_after(None, 10)

        assert (page.events, page.last_seq, page.gap) == ([], 2, False)

    def test_since_and_limit_page_forward(self, store: Store) -> None:
        for n in range(1, 6):
            queued(store, n)

        page = store.events_after(1, 2)

        assert [e.seq for e in page.events] == [2, 3]
        assert page.last_seq == 3

    def test_only_the_newest_events_are_kept_and_an_old_reader_sees_a_gap(
        self, store: Store, db: Path
    ) -> None:
        with raw(db) as conn:
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO events(at, kind) VALUES(?, 'message')",
                [(float(n),) for n in range(EVENTS_KEPT + 5)],
            )
            conn.execute("COMMIT")
            count, oldest = conn.execute("SELECT count(*), min(seq) FROM events").fetchone()

        page = store.events_after(1, 10)

        assert (count, oldest) == (EVENTS_KEPT, 6)
        assert page.gap is True
        assert page.events[0].seq == 6

    def test_a_since_past_the_newest_event_resets_to_the_head(self, store: Store) -> None:
        queued(store, 1)

        page = store.events_after(50, 10)

        assert (page.events, page.last_seq, page.gap) == ([], 1, True)

    def test_a_negative_since_is_refused(self, store: Store) -> None:
        with pytest.raises(ValueError, match="since"):
            store.events_after(-1, 10)


class TestCommitWait:
    def test_a_commit_from_another_thread_wakes_the_waiter(self, store: Store) -> None:
        generation = store.generation()
        waiting = InThread(lambda: store.wait_commit(generation, PROMPT_S))
        time.sleep(PARK_S)
        started = time.monotonic()

        queued(store, 1)

        assert waiting.result(PROMPT_S) > generation
        assert time.monotonic() - started < PROMPT_S / 2

    def test_without_a_commit_the_wait_ends_at_its_timeout(self, store: Store) -> None:
        generation = store.generation()

        assert store.wait_commit(generation, 0.05) == generation


class TestMigration:
    def test_a_version_2_database_gains_the_feed_and_keeps_its_rows(self, db: Path) -> None:
        first = Store(db)
        queued(first, 1)
        first.close()
        with raw(db) as conn:
            for name in (
                "events_message_insert",
                "events_message_status",
                "events_presence_insert",
                "events_presence_update",
                "events_prune",
            ):
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute("DROP TABLE events")
            conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")

        second = Store(db)
        try:
            second.update(
                "alice", mid(1), StatusChange(Status.DELIVERED), 11.0, expected=Status.QUEUED
            )
            statuses = kinds_and_statuses(second)
            kept = second.get("alice", mid(1))
        finally:
            second.close()

        with raw(db) as conn:
            version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        assert version == ("3",)
        assert statuses == [("message", "delivered")]
        assert kept is not None


class TestWatchOp:
    def test_no_since_answers_the_head_at_once(self, rig: ServiceRig) -> None:
        conn, token = rig.connect("alice")
        rig.send(conn, token, "bob")

        result = rig.call(rig.hello(client="cli"), "watch")

        assert result["events"] == []
        assert result["last_seq"] >= 2
        assert result["gap"] is False

    def test_a_send_wakes_a_parked_watch_with_its_event(self, rig: ServiceRig) -> None:
        conn, token = rig.connect("alice")
        head = rig.call(rig.hello(client="cli"), "watch")["last_seq"]
        watcher = rig.hello(client="cli")
        waiting = InThread(lambda: rig.call(watcher, "watch", since=head, timeout_s=30))
        time.sleep(PARK_S)

        sent = rig.send(conn, token, "bob", BODY)
        result = waiting.result(PROMPT_S)

        [event] = result["events"]
        assert (event["kind"], event["source"], event["id"], event["to"]) == (
            "message",
            "alice",
            sent["id"],
            "bob",
        )
        assert result["last_seq"] == event["seq"]
        assert BODY not in str(result)

    def test_presence_events_name_the_agent(self, rig: ServiceRig) -> None:
        head = rig.call(rig.hello(client="cli"), "watch")["last_seq"]

        rig.connect("bob")
        result = rig.call(rig.hello(client="cli"), "watch", since=head)

        assert {"kind": "presence", "agent": "bob", "online": True}.items() <= result["events"][
            0
        ].items()

    def test_a_zero_timeout_with_nothing_new_returns_at_once(self, rig: ServiceRig) -> None:
        head = rig.call(rig.hello(client="cli"), "watch")["last_seq"]
        started = time.monotonic()

        result = rig.call(rig.hello(client="cli"), "watch", since=head, timeout_s=0)

        assert (result["events"], result["last_seq"]) == ([], head)
        assert time.monotonic() - started < 0.5

    def test_shutdown_ends_a_parked_watch(self, rig: ServiceRig) -> None:
        head = rig.call(rig.hello(client="cli"), "watch")["last_seq"]
        watcher = rig.hello(client="cli")
        waiting = InThread(lambda: rig.call(watcher, "watch", since=head, timeout_s=30))
        time.sleep(PARK_S)

        rig.call(rig.hello(client="cli"), "shutdown")

        assert waiting.result(PROMPT_S)["events"] == []

    def test_an_out_of_range_timeout_is_invalid(self, rig: ServiceRig) -> None:
        from mz_relay.errors import RelayError

        with pytest.raises(RelayError, match="timeout_s"):
            rig.call(rig.hello(client="cli"), "watch", since=0, timeout_s=61)
