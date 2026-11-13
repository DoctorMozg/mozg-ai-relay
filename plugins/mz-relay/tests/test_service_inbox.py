"""RelayService ops inbox, ack, held, decide, wait, confirm, status and log; sender notices."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from mz_relay import __version__
from mz_relay.delivery import DeliveryEngine, EngineDeps
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
    envelope,
    mid,
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

    @pytest.mark.parametrize("client", ["mcp", "hook", "pi", "opencode"])
    def test_only_the_cli_may_decide_or_extend_a_context(
        self, rig: ServiceRig, client: str
    ) -> None:
        # An adapter or MCP client that sends a user op by mistake must not release a held row.
        conn_c, carol = rig.connect("carol")
        msg_id = sent_id(rig, conn_c, carol, "alice")
        other = rig.hello(client=client)

        for op, args in (
            ("decide", {"id": msg_id, "source": "carol", "decision": "approve"}),
            ("context_extend", {"context_id": msg_id, "by": 5}),
        ):
            with pytest.raises(RelayError) as err:
                rig.call(other, op, **args)
            assert err.value.code == "not_allowed"
            assert op in err.value.message

        assert status_of(rig, "carol", msg_id) is Status.HELD

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
        conn_b, bob = rig.connect("bob")
        shared = sent_id(rig, conn_c, carol, "alice")
        question = sent_id(rig, conn_b, bob, "alice")
        rig.clock.advance(1.0)
        rig.send(conn_a, alice, "bob", id=shared, kind="reply", in_reply_to=question)

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
        conn_a, alice = rig.connect("alice")
        question = sent_id(rig, conn_a, alice, "carol")
        shared = new_id()
        # carol's row is the newer one, so a lookup by id alone would pick it. Only a reply
        # may reuse an id that already names a context.
        rig.send(conn_e, erin, "alice", id=shared)
        rig.clock.advance(1.0)
        rig.send(conn_c, carol, "alice", id=shared, kind="reply", in_reply_to=question)

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


SUFFIX = "\n({n} undelivered notice(s) — call inbox)"
UNREACHABLE = "unreachable: socket gone; waits for bob to reconnect"
PULL_ONLY = "pull-only: bob has no wake-up channel; waits for its inbox call"


def expire(rig: ServiceRig, source: str, msg_id: str) -> None:
    """The write the engine's sweep makes when the row's ttl has passed."""
    force_status(rig, source, msg_id, StatusChange(Status.EXPIRED, reason="ttl"))


def sweep_past_ttl(rig: ServiceRig) -> None:
    """Moves the clock past the default ttl and runs the engine's sweep, as relayd does."""
    rig.clock.advance(3601)
    deps = EngineDeps(store=rig.store, roster=rig.roster.current, adapters={}, clock=rig.clock)
    DeliveryEngine(deps).sweep()


def notices_block(text: str) -> str | None:
    blocks = [block for block in text.split("\n\n") if block.startswith("notices:")]
    assert len(blocks) <= 1, f"more than one notices block in {text!r}"
    return blocks[0] if blocks else None


class TestNotices:
    def test_an_expiry_is_a_notice_shown_once(self, rig: ServiceRig) -> None:
        conn, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn, alice, "bob")
        sweep_past_ttl(rig)

        first = rig.call(conn, "inbox", token=alice)
        second = rig.call(conn, "inbox", token=alice)

        assert first["notices"] == [
            {"id": msg_id, "to": "bob", "status": "expired", "reason": "ttl"}
        ]
        assert notices_block(first["text"]) == f"notices:\n- {msg_id} → bob: expired (ttl)"
        assert first["notices_pending"] == 0
        assert second["notices"] == []
        assert notices_block(second["text"]) is None

    def test_an_undeliverable_row_is_a_notice_with_its_reason(self, rig: ServiceRig) -> None:
        conn, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn, alice, "bob")
        exhausted = StatusChange(Status.UNDELIVERABLE, reason="codex not found: /nope")
        force_status(rig, "alice", msg_id, exhausted)

        text = rig.call(conn, "inbox", token=alice)["text"]

        assert notices_block(text) == (
            f"notices:\n- {msg_id} → bob: undeliverable (codex not found: /nope)"
        )

    def test_a_denied_row_is_a_notice_for_its_sender(self, rig: ServiceRig) -> None:
        conn, carol = rig.connect("carol")
        msg_id = sent_id(rig, conn, carol, "alice")
        decide = {"id": msg_id, "source": "carol", "decision": "deny", "reason": "not now"}
        rig.call(rig.hello(client="cli"), "decide", **decide)

        text = rig.call(conn, "inbox", token=carol)["text"]

        assert notices_block(text) == f"notices:\n- {msg_id} → alice: denied (not now)"

    def test_an_unreachable_row_is_a_notice_and_its_later_expiry_is_another(
        self, rig: ServiceRig
    ) -> None:
        conn, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn, alice, "bob")
        force_status(rig, "alice", msg_id, StatusChange(Status.QUEUED, reason=UNREACHABLE))

        shown = [notices_block(rig.call(conn, "inbox", token=alice)["text"]) for _ in range(2)]
        expire(rig, "alice", msg_id)
        shown += [notices_block(rig.call(conn, "inbox", token=alice)["text"]) for _ in range(2)]

        assert shown == [
            f"notices:\n- {msg_id} → bob: queued ({UNREACHABLE})",
            None,
            f"notices:\n- {msg_id} → bob: expired (ttl)",
            None,
        ]

    def test_delivered_acked_retried_and_pull_only_rows_are_not_notices(
        self, rig: ServiceRig
    ) -> None:
        conn, alice = rig.connect("alice")
        ids = [sent_id(rig, conn, alice, "bob", body=f"task {n}") for n in range(4)]
        force_status(rig, "alice", ids[0], StatusChange(Status.DELIVERED, via="inbox"))
        force_status(rig, "alice", ids[1], StatusChange(Status.ACKED))
        retry = StatusChange(Status.QUEUED, reason="attempt 1 failed: socket busy")
        force_status(rig, "alice", ids[2], retry)
        force_status(rig, "alice", ids[3], StatusChange(Status.QUEUED, reason=PULL_ONLY))

        result = rig.call(conn, "inbox", token=alice)

        assert result["notices"] == []
        assert notices_block(result["text"]) is None

    def test_notices_go_to_the_sender_only(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        expire(rig, "alice", sent_id(rig, conn_a, alice, "bob"))

        result = rig.call(conn_b, "inbox", token=bob)

        assert result["notices"] == []
        assert rig.send(conn_b, bob, "alice")["notices_pending"] == 0


class TestNoticeSuffix:
    def test_send_counts_pending_notices_until_the_inbox_shows_them(self, rig: ServiceRig) -> None:
        conn, alice = rig.connect("alice")
        rig.connect("bob")
        for body in ("first", "second"):
            expire(rig, "alice", sent_id(rig, conn, alice, "bob", body=body))

        before = rig.send(conn, alice, "bob")
        rig.call(conn, "inbox", token=alice)
        after = rig.send(conn, alice, "bob")

        assert before["notices_pending"] == 2
        assert before["text"] == f"queued {before['id']} for bob" + SUFFIX.format(n=2)
        assert after["notices_pending"] == 0
        assert after["text"] == f"queued {after['id']} for bob"

    def test_ack_carries_the_pending_count_and_the_suffix(self, rig: ServiceRig) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        expire(rig, "alice", sent_id(rig, conn_a, alice, "bob"))
        incoming = sent_id(rig, conn_b, bob, "alice")
        force_status(rig, "bob", incoming, StatusChange(Status.DELIVERED, via="inbox"))

        result = rig.call(conn_a, "ack", token=alice, id=incoming)

        assert result["notices_pending"] == 1
        assert result["text"] == f"acked {incoming}" + SUFFIX.format(n=1)

    def test_peers_text_ends_with_the_suffix(self, rig: ServiceRig) -> None:
        conn, alice = rig.connect("alice")
        expire(rig, "alice", sent_id(rig, conn, alice, "bob"))

        text = rig.call(conn, "peers", token=alice)["text"]

        assert text.endswith(SUFFIX.format(n=1))


class TestLog:
    """The user's message log: any sender, no token, newest rows returned oldest first."""

    @pytest.fixture
    def user(self, rig: ServiceRig) -> ConnState:
        return rig.hello(client="cli")

    @staticmethod
    def insert(rig: ServiceRig, msg_id: str, at: float) -> None:
        """An alice → bob row stored at `at`; its context id is its own id."""
        rig.store.insert_message(envelope(msg_id, at=at), Status.QUEUED, at, next_attempt_at=None)

    @classmethod
    def stored(cls, rig: ServiceRig, count: int) -> list[str]:
        """`count` rows one second apart, each opening a separate context."""
        ids = [mid(n) for n in range(1, count + 1)]
        for n, msg_id in enumerate(ids, start=1):
            cls.insert(rig, msg_id, 1000.0 + n)
        return ids

    @staticmethod
    def ids(result: dict[str, object]) -> list[str]:
        messages = result["messages"]
        assert isinstance(messages, list)
        return [str(m["id"]) for m in messages]

    def test_a_row_carries_route_kind_status_reason_time_and_preview(
        self, rig: ServiceRig, user: ConnState
    ) -> None:
        conn, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn, alice, "bob", body="see src/app.py")
        row = rig.store.get("alice", msg_id)
        assert row is not None

        result = rig.call(user, "log")

        assert result == {
            "messages": [
                {
                    "id": msg_id,
                    "source": "alice",
                    "to": "bob",
                    "kind": "request",
                    "status": "queued",
                    "reason": None,
                    "time": row.envelope.time,
                    "preview": "see src/app.py",
                }
            ]
        }

    def test_a_row_shows_its_reason_when_one_is_set(self, rig: ServiceRig, user: ConnState) -> None:
        conn, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn, alice, "bob")
        retry = StatusChange(Status.QUEUED, reason="attempt 1 failed: socket busy")
        force_status(rig, "alice", msg_id, retry)

        [row] = rig.call(user, "log")["messages"]

        assert (row["status"], row["reason"]) == ("queued", "attempt 1 failed: socket busy")

    def test_rows_of_every_sender_come_oldest_first(self, rig: ServiceRig, user: ConnState) -> None:
        conn_a, alice = rig.connect("alice")
        conn_c, carol = rig.connect("carol")
        first = sent_id(rig, conn_a, alice, "bob")
        rig.clock.advance(1.0)
        held = sent_id(rig, conn_c, carol, "alice")
        rig.clock.advance(1.0)
        last = sent_id(rig, conn_a, alice, "bob")

        result = rig.call(user, "log")

        assert self.ids(result) == [first, held, last]
        assert [m["status"] for m in result["messages"]] == ["queued", "held", "queued"]

    def test_context_id_keeps_only_that_contexts_rows(
        self, rig: ServiceRig, user: ConnState
    ) -> None:
        conn_a, alice = rig.connect("alice")
        conn_b, bob = rig.connect("bob")
        question = sent_id(rig, conn_a, alice, "bob")
        rig.clock.advance(1.0)
        sent_id(rig, conn_a, alice, "bob", body="an unrelated task")
        rig.clock.advance(1.0)
        answer = rig.send(conn_b, bob, "alice", body="done", kind="reply", in_reply_to=question)

        result = rig.call(user, "log", context_id=question)

        assert self.ids(result) == [question, answer["id"]]

    def test_an_unknown_context_has_no_rows(self, rig: ServiceRig, user: ConnState) -> None:
        conn, alice = rig.connect("alice")
        sent_id(rig, conn, alice, "bob")

        assert rig.call(user, "log", context_id=new_id()) == {"messages": []}

    def test_a_limit_keeps_the_newest_rows_still_oldest_first(
        self, rig: ServiceRig, user: ConnState
    ) -> None:
        ids = self.stored(rig, 4)

        assert self.ids(rig.call(user, "log", limit=2)) == ids[2:]

    def test_the_limit_defaults_to_50(self, rig: ServiceRig, user: ConnState) -> None:
        ids = self.stored(rig, 51)

        assert self.ids(rig.call(user, "log")) == ids[1:]

    @pytest.mark.parametrize(("limit", "kept"), [(1, 1), (200, 51)], ids=["min-1", "max-200"])
    def test_the_limit_bounds_are_accepted(
        self, rig: ServiceRig, user: ConnState, limit: int, kept: int
    ) -> None:
        ids = self.stored(rig, 51)

        assert self.ids(rig.call(user, "log", limit=limit)) == ids[-kept:]

    def test_rows_stamped_alike_keep_insert_order(self, rig: ServiceRig, user: ConnState) -> None:
        # Ids sort differently from insert order, so only the insert order can explain the result.
        tied = [mid(3), mid(1), mid(2)]
        for msg_id in tied:
            self.insert(rig, msg_id, 1000.0)

        assert self.ids(rig.call(user, "log", limit=2)) == tied[1:]

    def test_log_needs_hello_first(self, rig: ServiceRig) -> None:
        with pytest.raises(RelayError) as err:
            rig.call(rig.new_conn(), "log")

        assert err.value.code == "invalid_request"

    @pytest.mark.parametrize("limit", [0, -1, 201, True, "10", 2.5])
    def test_a_limit_outside_1_to_200_is_invalid_args(
        self, rig: ServiceRig, user: ConnState, limit: object
    ) -> None:
        with pytest.raises(RelayError) as err:
            rig.call(user, "log", limit=limit)

        assert err.value.code == "invalid_args"
        assert err.value.message.startswith("log: limit ")

    @pytest.mark.parametrize("context_id", ["", 7])
    def test_an_empty_or_non_string_context_id_is_invalid_args(
        self, rig: ServiceRig, user: ConnState, context_id: object
    ) -> None:
        with pytest.raises(RelayError) as err:
            rig.call(user, "log", context_id=context_id)

        assert err.value.code == "invalid_args"
        assert err.value.message.startswith("log: context_id ")

    @pytest.mark.parametrize(
        ("body", "preview"),
        [
            ("y" * 200, "y" * 200),
            ("y" * 201, "y" * 200 + "…"),
            ("line one\nline two", "line one\nline two"),
        ],
        ids=["200", "201", "newline"],
    )
    def test_preview_is_the_first_200_chars_of_the_body_with_an_ellipsis_when_cut(
        self, rig: ServiceRig, user: ConnState, body: str, preview: str
    ) -> None:
        conn, alice = rig.connect("alice")
        sent_id(rig, conn, alice, "bob", body=body)

        [row] = rig.call(user, "log")["messages"]

        assert row["preview"] == preview

    def test_reading_the_log_leaves_rows_and_notices_alone(
        self, rig: ServiceRig, user: ConnState
    ) -> None:
        conn, alice = rig.connect("alice")
        msg_id = sent_id(rig, conn, alice, "bob")
        expire(rig, "alice", msg_id)
        before = rig.store.get("alice", msg_id)
        pending = rig.store.pending_notices("alice")

        [row] = rig.call(user, "log")["messages"]

        assert (row["status"], row["reason"]) == ("expired", "ttl")
        assert rig.store.get("alice", msg_id) == before
        assert rig.store.pending_notices("alice") == pending == 1

    def test_store_log_returns_the_newest_rows_oldest_first(self, rig: ServiceRig) -> None:
        ids = self.stored(rig, 3)

        newest = rig.store.log(None, 2)
        only_newest = rig.store.log(None, 1)
        one_context = rig.store.log(ids[0], 50)

        assert [row.envelope.id for row in newest] == ids[1:]
        assert [row.envelope.id for row in only_newest] == ids[2:]
        assert [row.envelope.id for row in one_context] == ids[:1]

    def test_store_log_breaks_time_ties_by_insert_order(self, rig: ServiceRig) -> None:
        tied = [mid(3), mid(1), mid(2)]
        for msg_id in tied:
            self.insert(rig, msg_id, 1000.0)

        assert [row.envelope.id for row in rig.store.log(None, 2)] == tied[1:]

    @pytest.mark.parametrize("limit", [0, -5, True])
    def test_store_log_rejects_a_limit_below_one(self, rig: ServiceRig, limit: int) -> None:
        with pytest.raises(ValueError, match="limit"):
            rig.store.log(None, limit)
