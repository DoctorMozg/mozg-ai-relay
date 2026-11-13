"""Tool-result texts: the exact strings agents read after send, inbox, ack and peers."""

from __future__ import annotations

import pytest
from mz_relay.texts import ack_text, inbox_text, peers_text, send_text, with_notice_suffix

QUEUED_ONLINE = "queued m1 for bob"
QUEUED_OFFLINE = "queued m1 for bob (offline; waits until it connects, ttl 3600 s)"
HELD = "held m1 for user approval — ask your user to run: mz-relay approve m1"
AWAITING = {"id": "m2", "source": "alice", "kind": "request", "time": "2026-11-13T04:05:00.123Z"}
DENIED = {"id": "m3", "to": "bob", "status": "denied", "reason": "policy"}
EXPIRED = {"id": "m4", "to": "bob", "status": "expired", "reason": "ttl"}


def send(status: str, *, online: bool = True, dedupe: bool = False) -> str:
    return send_text(
        msg_id="m1", to="bob", status=status, recipient_online=online, ttl_s=3600, dedupe=dedupe
    )


def agent(name: str, *, online: bool = True, allowed: bool = True) -> dict[str, object]:
    return {
        "name": name,
        "role": f"{name}-role",
        "harness": "opencode",
        "label": "personal",
        "description": f"Does {name} things.",
        "tools": "full",
        "online": online,
        "may_receive_from_me": allowed,
    }


class TestSend:
    def test_queued_for_an_online_recipient(self) -> None:
        assert send("queued") == QUEUED_ONLINE

    def test_queued_for_an_offline_recipient_names_the_ttl(self) -> None:
        assert send("queued", online=False) == QUEUED_OFFLINE

    @pytest.mark.parametrize("online", [True, False])
    def test_held_gives_the_approve_command(self, online: bool) -> None:
        assert send("held", online=online) == HELD

    def test_dedupe_of_a_delivered_row_reports_its_status(self) -> None:
        assert send("delivered", dedupe=True) == "already sent m1 to bob: delivered"

    @pytest.mark.parametrize(
        ("status", "online", "expected"),
        [("queued", True, QUEUED_ONLINE), ("queued", False, QUEUED_OFFLINE), ("held", True, HELD)],
    )
    def test_dedupe_of_a_queued_or_held_row_repeats_the_original_text(
        self, status: str, online: bool, expected: str
    ) -> None:
        assert send(status, online=online, dedupe=True) == expected


class TestInbox:
    def test_nothing_is_exactly_no_new_messages(self) -> None:
        assert inbox_text([], [], []) == "no new messages"

    def test_messages_are_joined_by_one_blank_line(self) -> None:
        assert inbox_text(["A", "B"], [], []) == "A\n\nB"

    def test_awaiting_ack_block_follows_no_new_messages(self) -> None:
        assert inbox_text([], [AWAITING], []) == (
            "no new messages\n\nawaiting ack:\n- m2 from alice (request, 2026-11-13T04:05:00.123Z)"
        )

    def test_sent_line_appends_the_reason_when_set(self) -> None:
        assert inbox_text(["A"], [], [DENIED]) == "A\n\nsent:\n- m3 → bob: denied (policy)"

    def test_sent_line_without_reason_has_no_parentheses(self) -> None:
        sent = [{"id": "m3", "to": "bob", "status": "queued", "reason": None}]
        assert inbox_text(["A"], [], sent) == "A\n\nsent:\n- m3 → bob: queued"

    def test_notices_block_lists_status_and_reason(self) -> None:
        text = inbox_text([], [], [], [EXPIRED])
        assert text == "no new messages\n\nnotices:\n- m4 → bob: expired (ttl)"

    def test_all_sections_in_order_with_several_lines(self) -> None:
        second = {**AWAITING, "id": "m5", "kind": "inform"}
        text = inbox_text(["A", "B"], [AWAITING, second], [DENIED], [EXPIRED])
        assert text == (
            "A\n\nB\n\n"
            "awaiting ack:\n"
            "- m2 from alice (request, 2026-11-13T04:05:00.123Z)\n"
            "- m5 from alice (inform, 2026-11-13T04:05:00.123Z)\n\n"
            "sent:\n- m3 → bob: denied (policy)\n\n"
            "notices:\n- m4 → bob: expired (ttl)"
        )


class TestPeersAck:
    def test_peers_are_sorted_and_exclude_me(self) -> None:
        agents = [agent("carol"), agent("alice"), agent("bob", online=False)]
        assert peers_text("alice", agents) == (
            "bob : bob-role — Does bob things. [offline]\n"
            "carol : carol-role — Does carol things. [online]"
        )

    def test_peer_i_may_not_message_is_marked(self) -> None:
        text = peers_text("carol", [agent("carol"), agent("bob", allowed=False)])
        assert text == "bob : bob-role — Does bob things. [online] (not allowed to message)"

    @pytest.mark.parametrize("agents", [[], [agent("alice")]], ids=["empty", "only-me"])
    def test_no_other_agents_is_no_peers(self, agents: list[dict[str, object]]) -> None:
        assert peers_text("alice", agents) == "no peers"

    def test_ack_text(self) -> None:
        assert ack_text("m1") == "acked m1"

    def test_notice_suffix_absent_without_pending_notices(self) -> None:
        assert with_notice_suffix("acked m1", 0) == "acked m1"

    @pytest.mark.parametrize("pending", [1, 2])
    def test_notice_suffix_counts_pending_notices(self, pending: int) -> None:
        assert with_notice_suffix("acked m1", pending) == (
            f"acked m1\n({pending} undelivered notice(s) — call inbox)"
        )
