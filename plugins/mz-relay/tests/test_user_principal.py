"""The user as a sender and a recipient: send_as_user, user_inbox, user_ack and its frame."""

from __future__ import annotations

from pathlib import Path

import pytest
from mz_relay.adapters.claude_transcript import LineKind, classify
from mz_relay.envelope import (
    HEADER_PREFIX,
    USER_HEADER_PREFIX,
    Kind,
    Status,
    render_from_user,
    render_text,
)
from mz_relay.errors import EnvelopeError, PolicyViolation, RelayError, RosterError
from mz_relay.ids import new_id
from mz_relay.policy import (
    USER_INBOX_TTL_S,
    ParentFacts,
    Policy,
    PolicyFacts,
    SendRequest,
    UserSender,
)
from mz_relay.roster import USER_NAME, Roster, parse_roster
from mz_relay.service import ConnState
from relay_rig import ServiceRig, envelope, mid, pull_roster

BODY = "please look at src/app.py before the release"
PW_HOME = Path("/tmp/mzr-home")


def roster_for_tests() -> Roster:
    return parse_roster(pull_roster(PW_HOME), pw_home=PW_HOME)


def send_as_user(rig: ServiceRig, to: str, body: str = BODY, **extra: object) -> dict[str, object]:
    args: dict[str, object] = {"id": new_id(), "to": to, "kind": "request", "body": body}
    args.update(extra)
    return rig.call(rig.hello(client="cli"), "send_as_user", **args)


def row(rig: ServiceRig, source: str, msg_id: object) -> tuple[Status, int, int]:
    stored = rig.store.get(source, str(msg_id))
    assert stored is not None
    return stored.status, stored.envelope.hop, stored.envelope.ttl_s


def pulled_texts(rig: ServiceRig, conn: ConnState, token: str) -> list[str]:
    return [str(m["text"]) for m in rig.call(conn, "inbox", token=token)["messages"]]


class TestSendAsUser:
    def test_only_a_cli_connection_may_send_as_the_user(self, rig: ServiceRig) -> None:
        args = {"id": new_id(), "to": "bob", "kind": "request", "body": BODY}

        with pytest.raises(RelayError) as caught:
            rig.call(rig.hello(client="mcp"), "send_as_user", **args)

        assert caught.value.code == "not_allowed"

    def test_the_user_may_message_a_corporate_agent_unheld_at_hop_0(self, rig: ServiceRig) -> None:
        sent = send_as_user(rig, "carol")

        assert row(rig, USER_NAME, sent["id"])[:2] == (Status.QUEUED, 0)

    def test_the_agent_reads_the_user_frame_not_the_peer_frame(self, rig: ServiceRig) -> None:
        conn, bob = rig.connect("bob")
        send_as_user(rig, "bob")

        [text] = pulled_texts(rig, conn, bob)

        assert text.startswith(f"{USER_HEADER_PREFIX}to bob")
        assert HEADER_PREFIX not in text
        assert "untrusted message from another agent" not in text
        assert f"  {BODY}" in text

    def test_an_unknown_recipient_is_refused(self, rig: ServiceRig) -> None:
        with pytest.raises(RelayError) as caught:
            send_as_user(rig, "nobody")

        assert caught.value.code == "unknown_recipient"


class TestMessagesToTheUser:
    def test_an_agent_outside_may_send_to_still_reaches_the_user(self, rig: ServiceRig) -> None:
        # carol's may_send_to names only alice, and carol is corporate.
        conn, carol = rig.connect("carol")

        sent = rig.send(conn, carol, USER_NAME)

        assert row(rig, "carol", sent["id"]) == (Status.QUEUED, 0, USER_INBOX_TTL_S)

    def test_user_inbox_hands_each_message_over_once_and_lists_it_until_acked(
        self, rig: ServiceRig
    ) -> None:
        conn, bob = rig.connect("bob")
        sent = rig.send(conn, bob, USER_NAME, BODY)
        cli = rig.hello(client="cli")

        first = rig.call(cli, "user_inbox")
        second = rig.call(cli, "user_inbox")
        rig.call(cli, "user_ack", id=sent["id"])
        third = rig.call(cli, "user_inbox")

        [message] = first["messages"]
        assert (message["id"], message["source"], message["body"]) == (sent["id"], "bob", BODY)
        assert second["messages"] == []
        assert [m["id"] for m in second["awaiting_ack"]] == [sent["id"]]
        assert third["awaiting_ack"] == []
        assert row(rig, "bob", sent["id"])[0] is Status.ACKED

    def test_the_user_ops_refuse_a_non_cli_connection(self, rig: ServiceRig) -> None:
        mcp = rig.hello(client="mcp")

        for op, args in (("user_inbox", {}), ("user_ack", {"id": mid(1)})):
            with pytest.raises(RelayError) as caught:
                rig.call(mcp, op, **args)
            assert caught.value.code == "not_allowed"

    def test_the_brief_tells_agents_they_may_message_the_user(self, rig: ServiceRig) -> None:
        brief = rig.register(rig.hello(), "bob")["brief"]

        assert f"- {USER_NAME} : your user" in brief


class TestReplyChains:
    def test_an_agent_reply_to_the_user_joins_the_users_context(self, rig: ServiceRig) -> None:
        conn, bob = rig.connect("bob")
        asked = send_as_user(rig, "bob")
        rig.call(conn, "inbox", token=bob)

        reply = rig.send(
            conn, bob, USER_NAME, "done, see abc1234", kind="reply", in_reply_to=asked["id"]
        )

        stored = rig.store.get("bob", str(reply["id"]))
        assert stored is not None
        assert (stored.envelope.context_id, stored.envelope.hop) == (asked["id"], 1)

    def test_a_user_reply_joins_the_agents_context_at_hop_0(self, rig: ServiceRig) -> None:
        conn, bob = rig.connect("bob")
        question = rig.send(conn, bob, USER_NAME, "which branch?")
        rig.call(rig.hello(client="cli"), "user_inbox")

        answer = send_as_user(rig, "bob", "main", kind="reply", in_reply_to=question["id"])

        stored = rig.store.get(USER_NAME, str(answer["id"]))
        assert stored is not None
        assert (stored.envelope.context_id, stored.envelope.hop) == (question["id"], 0)


class TestUserPolicy:
    @pytest.fixture
    def policy(self) -> Policy:
        return Policy(roster_for_tests())

    def request(self, to: str, *, in_reply_to: str | None = None) -> SendRequest:
        return SendRequest(
            msg_id=mid(1),
            source=UserSender(may_send_to=frozenset({"alice", "bob", "carol"})),
            to=to,
            kind=Kind.REQUEST if in_reply_to is None else Kind.REPLY,
            body=BODY,
            context_id=None,
            in_reply_to=in_reply_to,
        )

    def test_the_user_is_not_held_by_the_turn_cap(self, policy: Policy) -> None:
        decision = policy.check(self.request("bob"), PolicyFacts(parent=None, context_turns=999))

        assert decision.status is Status.QUEUED

    def test_a_user_reply_deep_in_a_chain_starts_at_hop_0(self, policy: Policy) -> None:
        parent = ParentFacts(context_id=mid(9), hop=4, source="bob", to=USER_NAME)

        decision = policy.check(self.request("bob", in_reply_to=mid(9)), PolicyFacts(parent))

        assert (decision.hop, decision.context_id) == (0, mid(9))

    def test_the_user_cannot_message_itself(self, policy: Policy) -> None:
        with pytest.raises(PolicyViolation) as caught:
            policy.check(self.request(USER_NAME), PolicyFacts(parent=None))

        assert caught.value.code == "echo_to_origin"


class TestFrames:
    def test_render_text_picks_the_user_frame_for_the_user(self) -> None:
        env = envelope(mid(1), source=USER_NAME, body=BODY)

        assert render_text(env, roster_for_tests()) == render_from_user(env)

    def test_render_from_user_refuses_an_agent_source(self) -> None:
        with pytest.raises(EnvelopeError):
            render_from_user(envelope(mid(1), source="alice", body=BODY))

    def test_claude_classifies_a_user_frame_as_another_peer_so_the_gate_stays_off(self) -> None:
        text = render_from_user(envelope(mid(1), source=USER_NAME, body=BODY))
        line = {
            "type": "user",
            "isMeta": True,
            "origin": {"kind": "peer"},
            "message": {"role": "user", "content": text},
        }

        assert classify(line).kind is LineKind.OTHER_PEER


class TestRoster:
    def test_an_agent_named_user_is_refused(self) -> None:
        raw = (
            'version = 1\n[agents.user]\nharness = "opencode"\nlabel = "personal"\n'
            'role = "x"\ndescription = "y"\n'
        )

        with pytest.raises(RosterError, match="reserved"):
            parse_roster(raw, pw_home=PW_HOME)
