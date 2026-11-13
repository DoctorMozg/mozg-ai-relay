"""RelayService ops inbox, ack, held, decide, wait, confirm and status."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from mz_relay import __version__
from mz_relay.envelope import Status
from mz_relay.errors import RelayError
from mz_relay.ids import new_id
from mz_relay.protocol import PROTOCOL_VERSION
from mz_relay.service import ConnState
from mz_relay.store import StatusChange
from relay_rig import (
    ERIN_CORPORATE,
    PROC_WAIT_S,
    InThread,
    ServiceRig,
    notified_within,
    run_in_thread,
)


def sent_id(
    rig: ServiceRig, conn: ConnState, token: str, to: str, body: str = "please review src/app.py"
) -> str:
    return str(rig.send(conn, token, to, body)["id"])


def force_status(rig: ServiceRig, source: str, msg_id: str, change: StatusChange) -> None:
    row = rig.store.get(source, msg_id)
    assert row is not None
    assert rig.store.update(source, msg_id, change, rig.clock.now(), expected=row.status)


def status_of(rig: ServiceRig, source: str, msg_id: str) -> Status:
    row = rig.store.get(source, msg_id)
    assert row is not None
    return row.status


@contextmanager
def clock_ticking(rig: ServiceRig) -> Iterator[None]:
    """Advances the fake clock alongside real time while a blocking op runs."""
    # `wait` may measure its deadline on the injected clock; a frozen one would never expire.
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(0.05):
            rig.clock.advance(0.05)

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(PROC_WAIT_S)


class TestInbox:
    def test_inbox_returns_the_rendered_message_and_marks_it_delivered(
        self, rig: ServiceRig
    ) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob", body="see src/app.py line 40")

        result = rig.call(conn_b, "inbox", token=bob)

        assert len(result["messages"]) == 1
        message = result["messages"][0]
        assert message["envelope"]["id"] == msg_id
        assert message["text"].startswith("[mz-relay] Peer message from alice")
        assert "  see src/app.py line 40" in message["text"]
        assert result["text"].startswith(message["text"])
        row = rig.store.get("alice", msg_id)
        assert row is not None
        assert row.status is Status.DELIVERED
        assert row.via == "inbox"

    def test_second_inbox_has_no_messages_and_lists_awaiting_ack(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        rig.call(conn_b, "inbox", token=bob)

        result = rig.call(conn_b, "inbox", token=bob)

        assert result["messages"] == []
        [awaiting] = result["awaiting_ack"]
        assert awaiting["id"] == msg_id
        assert awaiting["source"] == "alice"
        assert awaiting["kind"] == "request"
        assert result["text"] == (
            f"no new messages\n\nawaiting ack:\n- {msg_id} from alice (request, {awaiting['time']})"
        )

    def test_sender_inbox_lists_sent_messages_with_status(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        rig.call(conn_b, "inbox", token=bob)

        result = rig.call(conn_a, "inbox", token=alice)

        sent = result["sent"][0]
        assert (sent["id"], sent["to"], sent["status"]) == (msg_id, "bob", "delivered")
        assert f"sent:\n- {msg_id} → bob: delivered" in result["text"]

    def test_limit_caps_the_messages_returned(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        ids = [sent_id(rig, conn_a, alice, "bob", body=f"note {n}") for n in range(3)]

        first = rig.call(conn_b, "inbox", token=bob, limit=2)
        second = rig.call(conn_b, "inbox", token=bob, limit=2)

        assert [m["envelope"]["id"] for m in first["messages"]] == ids[:2]
        assert [m["envelope"]["id"] for m in second["messages"]] == ids[2:]

    @pytest.mark.parametrize(
        ("op", "args"),
        [
            ("inbox", {}),
            ("ack", {"id": "01JC0000000000000000000001"}),
            ("wait", {"session_id": "s-1", "timeout_s": 1}),
            ("confirm", {"id": "01JC0000000000000000000001"}),
        ],
    )
    def test_a_bad_token_is_refused(
        self, rig: ServiceRig, op: str, args: dict[str, object]
    ) -> None:
        conn = rig.hello(client="cli")

        with pytest.raises(RelayError) as err:
            rig.call(conn, op, token="0" * 32, **args)

        assert err.value.code == "bad_token"


class TestAck:
    @pytest.mark.parametrize(
        "status", [Status.DELIVERING, Status.HANDED_OFF, Status.DELIVERED, Status.LEASED]
    )
    def test_ack_from_an_in_flight_or_delivered_status(
        self, rig: ServiceRig, status: Status
    ) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        force_status(rig, "alice", msg_id, StatusChange(status=status))

        result = rig.call(conn_b, "ack", token=bob, id=msg_id)

        assert result["id"] == msg_id
        assert result["status"] == "acked"
        assert result["text"] == f"acked {msg_id}"
        assert status_of(rig, "alice", msg_id) is Status.ACKED

    def test_second_ack_returns_acked_again(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        rig.call(conn_b, "inbox", token=bob)
        rig.call(conn_b, "ack", token=bob, id=msg_id)

        result = rig.call(conn_b, "ack", token=bob, id=msg_id)

        assert result["status"] == "acked"
        assert result["text"] == f"acked {msg_id}"

    def test_ack_of_an_undelivered_row_names_its_status(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")

        with pytest.raises(RelayError) as err:
            rig.call(conn_b, "ack", token=bob, id=msg_id)

        assert err.value.code == "not_found"
        assert err.value.message == (
            f"message {msg_id} has not been delivered to you yet (status queued)"
        )
        assert status_of(rig, "alice", msg_id) is Status.QUEUED

    def test_sender_cannot_ack_its_own_outbound_message(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        rig.call(conn_b, "inbox", token=bob)

        with pytest.raises(RelayError) as err:
            rig.call(conn_a, "ack", token=alice, id=msg_id)

        assert err.value.code == "not_found"
        assert status_of(rig, "alice", msg_id) is Status.DELIVERED

    def test_ack_of_an_unknown_id_is_not_found(self, rig: ServiceRig) -> None:
        conn_b, bob = rig.connect("bob")

        with pytest.raises(RelayError) as err:
            rig.call(conn_b, "ack", token=bob, id=new_id())

        assert err.value.code == "not_found"


class TestDecide:
    def test_held_lists_the_message_with_both_labels(self, rig: ServiceRig) -> None:
        conn_c, carol = rig.connect("carol")
        msg_id = sent_id(rig, conn_c, carol, "alice", body="corporate diff summary")

        result = rig.call(rig.hello(client="cli"), "held")

        [held] = result["messages"]
        assert held["id"] == msg_id
        assert (held["source"], held["to"]) == ("carol", "alice")
        assert (held["source_label"], held["to_label"]) == ("corporate", "personal")
        assert held["preview"] == "corporate diff summary"

    @pytest.mark.parametrize(
        ("body", "preview"), [("y" * 200, "y" * 200), ("y" * 201, "y" * 200 + "…")]
    )
    def test_held_preview_is_the_first_200_chars_with_an_ellipsis_when_cut(
        self, rig: ServiceRig, body: str, preview: str
    ) -> None:
        conn_c, carol = rig.connect("carol")
        sent_id(rig, conn_c, carol, "alice", body=body)

        [held] = rig.call(rig.hello(client="cli"), "held")["messages"]

        assert held["preview"] == preview

    def test_approve_queues_the_message_due_now(self, rig: ServiceRig) -> None:
        conn_c, carol = rig.connect("carol")
        msg_id = sent_id(rig, conn_c, carol, "alice")
        rig.clock.advance(30.0)

        result = rig.call(
            rig.hello(client="cli"), "decide", id=msg_id, source="carol", decision="approve"
        )

        assert result == {"id": msg_id, "status": "queued"}
        row = rig.store.get("carol", msg_id)
        assert row is not None
        assert row.status is Status.QUEUED
        assert row.next_attempt_at == rig.clock.now()
        assert rig.call(rig.hello(client="cli"), "held")["messages"] == []

    def test_approve_notifies_changed(self, rig: ServiceRig) -> None:
        conn_c, carol = rig.connect("carol")
        msg_id = sent_id(rig, conn_c, carol, "alice")
        user = rig.hello(client="cli")

        with notified_within(rig.changed, 0.5) as woke:
            rig.call(user, "decide", id=msg_id, source="carol", decision="approve")

        assert woke.is_set()

    def test_approved_message_reaches_the_recipient_inbox(self, rig: ServiceRig) -> None:
        conn_c, carol = rig.connect("carol")
        conn_a, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn_c, carol, "alice")
        assert rig.call(conn_a, "inbox", token=alice)["messages"] == []

        rig.call(rig.hello(client="cli"), "decide", id=msg_id, source="carol", decision="approve")

        [message] = rig.call(conn_a, "inbox", token=alice)["messages"]
        assert message["envelope"]["id"] == msg_id

    def test_deny_with_reason_shows_in_the_sender_sent_list(self, rig: ServiceRig) -> None:
        conn_c, carol = rig.connect("carol")
        msg_id = sent_id(rig, conn_c, carol, "alice")

        result = rig.call(
            rig.hello(client="cli"),
            "decide",
            id=msg_id,
            source="carol",
            decision="deny",
            reason="policy",
        )

        assert result == {"id": msg_id, "status": "denied"}
        assert status_of(rig, "carol", msg_id) is Status.DENIED
        inbox_text = rig.call(conn_c, "inbox", token=carol)["text"]
        assert f"- {msg_id} → alice: denied (policy)" in inbox_text.splitlines()

    def test_decide_on_a_queued_message_is_not_held(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn_a, alice, "bob")

        with pytest.raises(RelayError) as err:
            rig.call(
                rig.hello(client="cli"), "decide", id=msg_id, source="alice", decision="approve"
            )

        assert err.value.code == "not_held"
        assert status_of(rig, "alice", msg_id) is Status.QUEUED

    def test_decide_on_an_unknown_id_is_not_found(self, rig: ServiceRig) -> None:
        with pytest.raises(RelayError) as err:
            rig.call(
                rig.hello(client="cli"), "decide", id=new_id(), source="carol", decision="deny"
            )

        assert err.value.code == "not_found"


@pytest.fixture
def two_corporate(relay_home: Path) -> Iterator[ServiceRig]:
    """The pull roster plus erin, a second corporate sender to alice."""
    with ServiceRig(relay_home, extra_agents=ERIN_CORPORATE) as built:
        yield built


class TestHeldLookup:
    def test_held_with_an_id_returns_only_that_row_and_leaves_it_held(
        self, rig: ServiceRig
    ) -> None:
        conn_c, carol = rig.connect("carol")
        wanted = sent_id(rig, conn_c, carol, "alice", body="corporate diff summary")
        sent_id(rig, conn_c, carol, "alice", body="another held message")
        before = rig.store.get("carol", wanted)

        result = rig.call(rig.hello(client="cli"), "held", id=wanted)

        [held] = result["messages"]
        assert (held["id"], held["source"], held["to"]) == (wanted, "carol", "alice")
        assert (held["source_label"], held["to_label"]) == ("corporate", "personal")
        assert held["preview"] == "corporate diff summary"
        assert rig.store.get("carol", wanted) == before

    def test_held_with_an_unknown_id_is_not_found_and_writes_nothing(self, rig: ServiceRig) -> None:
        conn_c, carol = rig.connect("carol")
        sent_id(rig, conn_c, carol, "alice")
        before = rig.store.counts()

        with pytest.raises(RelayError) as err:
            rig.call(rig.hello(client="cli"), "held", id=new_id())

        assert err.value.code == "not_found"
        assert rig.store.counts() == before

    def test_held_with_the_id_of_a_queued_row_is_not_held_and_writes_nothing(
        self, rig: ServiceRig
    ) -> None:
        conn_a, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        before = rig.store.get("alice", msg_id)

        with pytest.raises(RelayError) as err:
            rig.call(rig.hello(client="cli"), "held", id=msg_id)

        assert err.value.code == "not_held"
        assert rig.store.get("alice", msg_id) == before

    def test_held_with_an_id_finds_the_held_row_when_a_newer_sender_reused_the_id(
        self, rig: ServiceRig
    ) -> None:
        conn_c, carol = rig.connect("carol")
        conn_a, alice = rig.connect("alice")
        shared = sent_id(rig, conn_c, carol, "alice")
        rig.clock.advance(1.0)
        rig.send(conn_a, alice, "bob", id=shared)

        [held] = rig.call(rig.hello(client="cli"), "held", id=shared)["messages"]

        assert (held["id"], held["source"]) == (shared, "carol")


class TestDecideBySource:
    @pytest.mark.parametrize(
        ("decision", "status"), [("approve", Status.QUEUED), ("deny", Status.DENIED)]
    )
    def test_decide_changes_only_the_named_senders_row_when_two_share_an_id(
        self, two_corporate: ServiceRig, decision: str, status: Status
    ) -> None:
        rig = two_corporate
        conn_e, erin = rig.connect("erin")
        conn_c, carol = rig.connect("carol")
        shared = new_id()
        # carol's row is the newer one, so a lookup by id alone would pick it.
        rig.send(conn_e, erin, "alice", id=shared)
        rig.clock.advance(1.0)
        rig.send(conn_c, carol, "alice", id=shared)

        result = rig.call(
            rig.hello(client="cli"), "decide", id=shared, source="erin", decision=decision
        )

        assert result == {"id": shared, "status": status.value}
        assert status_of(rig, "erin", shared) is status
        assert status_of(rig, "carol", shared) is Status.HELD

    def test_decide_for_a_sender_that_never_used_the_id_is_not_found(
        self, two_corporate: ServiceRig
    ) -> None:
        rig = two_corporate
        conn_c, carol = rig.connect("carol")
        msg_id = sent_id(rig, conn_c, carol, "alice")

        with pytest.raises(RelayError) as err:
            rig.call(
                rig.hello(client="cli"), "decide", id=msg_id, source="erin", decision="approve"
            )

        assert err.value.code == "not_found"
        assert status_of(rig, "carol", msg_id) is Status.HELD


class TestWaitConfirm:
    def test_wait_without_fallback_rows_times_out(self, rig: ServiceRig) -> None:
        conn_b, bob = rig.connect("bob")

        with clock_ticking(rig):
            result = run_in_thread(
                lambda: rig.call(conn_b, "wait", token=bob, session_id="s-1", timeout_s=1),
                timeout_s=PROC_WAIT_S,
            )

        assert result == {"timeout": True}

    def test_wait_leases_a_fallback_row_set_from_another_thread(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        waiter = InThread(
            lambda: rig.call(conn_b, "wait", token=bob, session_id="s-1", timeout_s=10)
        )
        time.sleep(0.2)
        force_status(rig, "alice", msg_id, StatusChange(status=Status.FALLBACK))
        with rig.changed:
            rig.changed.notify_all()

        # Well under the 10 s wait: only the notify can have ended it this soon.
        message = waiter.result(timeout_s=3.0)["message"]
        assert message["envelope"]["id"] == msg_id
        assert message["text"].startswith("[mz-relay] Peer message from alice")
        assert status_of(rig, "alice", msg_id) is Status.LEASED
        assert rig.call(conn_b, "inbox", token=bob)["messages"] == []

    def test_wait_logs_the_client_session_id_quoted(
        self, rig: ServiceRig, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="mz_relay.service")
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        force_status(rig, "alice", msg_id, StatusChange(status=Status.FALLBACK))
        # The session id comes from the client, so a newline in it could forge a log line.
        session_id = "s-1\nforged: relayd stopped"

        rig.call(conn_b, "wait", token=bob, session_id=session_id, timeout_s=1)

        lines = [r.getMessage() for r in caplog.records if r.name == "mz_relay.service"]
        assert any(repr(session_id) in line for line in lines)
        assert not any("\nforged" in line for line in lines)

    def test_confirm_marks_a_leased_row_delivered_via_rewake(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        lease = StatusChange(status=Status.LEASED, lease_until=rig.clock.now() + 30.0)
        force_status(rig, "alice", msg_id, lease)

        result = rig.call(conn_b, "confirm", token=bob, id=msg_id)

        assert result == {"id": msg_id, "status": "delivered"}
        row = rig.store.get("alice", msg_id)
        assert row is not None
        assert row.status is Status.DELIVERED
        assert row.via == "rewake"

    def test_confirm_on_an_acked_row_reports_acked(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        msg_id = sent_id(rig, conn_a, alice, "bob")
        force_status(rig, "alice", msg_id, StatusChange(status=Status.ACKED))

        result = rig.call(conn_b, "confirm", token=bob, id=msg_id)

        assert result == {"id": msg_id, "status": "acked"}
        assert status_of(rig, "alice", msg_id) is Status.ACKED

    def test_confirm_of_an_unknown_id_is_not_found(self, rig: ServiceRig) -> None:
        conn_b, bob = rig.connect("bob")

        with pytest.raises(RelayError) as err:
            rig.call(conn_b, "confirm", token=bob, id=new_id())

        assert err.value.code == "not_found"

    def test_status_reports_agents_and_counts(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_c, carol = rig.connect("carol")
        sent_id(rig, conn_a, alice, "bob")
        sent_id(rig, conn_c, carol, "alice")

        result = rig.call(rig.hello(client="cli"), "status")

        assert result["protocol"] == PROTOCOL_VERSION
        assert result["version"] == __version__
        agents = {a["name"]: a for a in result["agents"]}
        assert set(agents) == {"alice", "bob", "carol"}
        assert agents["alice"]["online"] is True
        assert agents["bob"]["online"] is False
        assert agents["bob"]["harness"] == "opencode"
        nonzero = {k: v for k, v in result["counts"].items() if v}
        assert nonzero == {"queued": 1, "held": 1}
