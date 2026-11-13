"""Policy.check: routing, hold, reply chains, hop budget, body limits, caps and closed contexts."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import PLUGIN
from mz_relay.envelope import Kind, Status
from mz_relay.errors import EnvelopeError, PolicyViolation, RelayError
from mz_relay.policy import (
    MAX_BODY_CHARS,
    Decision,
    ParentFacts,
    Policy,
    PolicyFacts,
    SendRequest,
)
from mz_relay.roster import Label, Roster, parse_roster

EXAMPLE_ROSTER = PLUGIN / "data" / "roster.example.toml"
PW_HOME = Path("/home/u")
MSG_ID = "01JC0000000000000000000001"
PARENT_ID = "01JC0000000000000000000000"
NO_FACTS = PolicyFacts(parent=None)


def example_roster(old: str = "", new: str = "") -> Roster:
    text = EXAMPLE_ROSTER.read_text(encoding="utf-8")
    if old:
        assert old in text, f"example roster no longer contains {old!r}"
        text = text.replace(old, new)
    return parse_roster(text, pw_home=PW_HOME)


def request(roster: Roster, source: str, to: str, **changes: object) -> SendRequest:
    base = SendRequest(
        msg_id=MSG_ID,
        source=roster.get(source),
        to=to,
        kind=Kind.REQUEST,
        body="please review the diff in src/app.py",
        context_id=None,
        in_reply_to=None,
    )
    return dataclasses.replace(base, **cast("dict[str, Any]", changes))


def parent(hop: int, *, context_id: str = PARENT_ID, source: str = "builder") -> ParentFacts:
    return ParentFacts(context_id=context_id, hop=hop, source=source, to="architect")


def reply_facts(hop: int, **kwargs: str) -> PolicyFacts:
    return PolicyFacts(parent=parent(hop, **kwargs))


def context_facts(*, parent_hop: int = 0, **changes: object) -> PolicyFacts:
    """Facts for a reply into PARENT_ID's context, with the given context and inbox."""
    return dataclasses.replace(reply_facts(parent_hop), **cast("dict[str, Any]", changes))


@pytest.fixture
def roster() -> Roster:
    return example_roster()


@pytest.fixture
def policy(roster: Roster) -> Policy:
    return Policy(roster)


class TestRouting:
    def test_personal_to_corporate_request_is_queued_as_a_new_chain(
        self, roster: Roster, policy: Policy
    ) -> None:
        decision = policy.check(request(roster, "architect", "builder"), NO_FACTS)

        assert decision == Decision(
            status=Status.QUEUED,
            hop=0,
            context_id=MSG_ID,
            label=Label.PERSONAL,
            ack_required=True,
            ttl_s=3600,
        )

    def test_corporate_to_personal_is_held_for_the_user(
        self, roster: Roster, policy: Policy
    ) -> None:
        decision = policy.check(request(roster, "builder", "architect"), NO_FACTS)

        assert decision.status is Status.HELD
        assert decision.label is Label.CORPORATE

    def test_corporate_to_personal_on_another_harness_is_held_too(
        self, roster: Roster, policy: Policy
    ) -> None:
        decision = policy.check(request(roster, "builder", "reviewer"), NO_FACTS)

        assert decision.status is Status.HELD

    def test_hold_turned_off_in_the_roster_queues_corporate_to_personal(self) -> None:
        roster = example_roster(
            "hold_corporate_to_personal = true", "hold_corporate_to_personal = false"
        )

        decision = Policy(roster).check(request(roster, "builder", "architect"), NO_FACTS)

        assert decision.status is Status.QUEUED

    def test_local_sender_is_never_held(self, roster: Roster, policy: Policy) -> None:
        decision = policy.check(request(roster, "scout", "architect"), NO_FACTS)

        assert decision.status is Status.QUEUED
        assert decision.label is Label.LOCAL

    def test_recipient_outside_may_send_to_is_not_allowed(
        self, roster: Roster, policy: Policy
    ) -> None:
        with pytest.raises(PolicyViolation) as caught:
            policy.check(request(roster, "builder", "scout"), NO_FACTS)

        assert caught.value.code == "not_allowed"

    def test_message_to_self_is_an_echo_even_when_self_is_not_in_may_send_to(
        self, roster: Roster, policy: Policy
    ) -> None:
        with pytest.raises(PolicyViolation) as caught:
            policy.check(request(roster, "architect", "architect"), NO_FACTS)

        assert caught.value.code == "echo_to_origin"

    def test_name_missing_from_the_roster_is_an_unknown_recipient(
        self, roster: Roster, policy: Policy
    ) -> None:
        with pytest.raises(PolicyViolation) as caught:
            policy.check(request(roster, "architect", "ghost"), NO_FACTS)

        assert caught.value.code == "unknown_recipient"

    def test_default_may_send_to_reaches_every_other_agent(
        self, roster: Roster, policy: Policy
    ) -> None:
        # reviewer has no may_send_to, so it may message all four others.
        for to in ("architect", "builder", "helper", "scout"):
            assert policy.check(request(roster, "reviewer", to), NO_FACTS).status is Status.QUEUED


class TestChains:
    def test_reply_one_below_the_budget_is_accepted_at_the_budget(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "architect", "builder", kind=Kind.REPLY, in_reply_to=PARENT_ID)

        decision = policy.check(req, reply_facts(3))

        assert decision.hop == 4
        assert decision.context_id == PARENT_ID

    def test_reply_past_the_budget_is_refused_with_the_exact_message(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "architect", "builder", kind=Kind.REPLY, in_reply_to=PARENT_ID)

        with pytest.raises(PolicyViolation) as caught:
            policy.check(req, reply_facts(4))

        assert caught.value.code == "hop_exceeded"
        assert caught.value.message == (
            "hop 5 exceeds the hop budget 4; ask your user before continuing this chain"
        )

    def test_hop_budget_comes_from_the_roster_policy(self) -> None:
        roster = example_roster("hop_budget = 4", "hop_budget = 2")
        req = request(roster, "architect", "builder", kind=Kind.REPLY, in_reply_to=PARENT_ID)

        with pytest.raises(PolicyViolation) as caught:
            Policy(roster).check(req, reply_facts(2))

        assert caught.value.message == (
            "hop 3 exceeds the hop budget 2; ask your user before continuing this chain"
        )

    def test_reply_to_a_message_never_delivered_is_an_unknown_parent(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "architect", "builder", kind=Kind.REPLY, in_reply_to="m9")

        with pytest.raises(PolicyViolation) as caught:
            policy.check(req, NO_FACTS)

        assert caught.value.code == "unknown_parent"
        assert caught.value.message == "no message m9 was delivered to architect"

    def test_reply_inherits_the_parent_context(self, roster: Roster, policy: Policy) -> None:
        req = request(roster, "architect", "builder", kind=Kind.REPLY, in_reply_to=PARENT_ID)

        decision = policy.check(req, reply_facts(0, context_id="ctx-original-0001"))

        assert decision.context_id == "ctx-original-0001"
        assert decision.hop == 1

    def test_explicit_context_equal_to_the_parent_is_accepted(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(
            roster,
            "architect",
            "builder",
            kind=Kind.REPLY,
            in_reply_to=PARENT_ID,
            context_id="ctx-original-0001",
        )

        decision = policy.check(req, reply_facts(0, context_id="ctx-original-0001"))

        assert decision.context_id == "ctx-original-0001"

    def test_explicit_context_differing_from_the_parent_is_invalid(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(
            roster,
            "architect",
            "builder",
            kind=Kind.REPLY,
            in_reply_to=PARENT_ID,
            context_id="ctx-someone-else",
        )

        with pytest.raises(EnvelopeError) as caught:
            policy.check(req, reply_facts(0, context_id="ctx-original-0001"))

        assert caught.value.code == "invalid_args"
        assert caught.value.message == "context_id differs from the parent's"

    def test_explicit_context_without_in_reply_to_cannot_join_a_context(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "architect", "builder", context_id="ctx-original-0001")

        with pytest.raises(EnvelopeError) as caught:
            policy.check(req, NO_FACTS)

        assert caught.value.code == "invalid_args"
        assert caught.value.message == (
            "context_id needs in_reply_to; a new message starts its own context"
        )

    def test_forwarding_within_the_chain_to_another_agent_still_costs_a_hop(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "architect", "reviewer", kind=Kind.REQUEST, in_reply_to=PARENT_ID)

        decision = policy.check(req, reply_facts(1))

        assert (decision.hop, decision.context_id) == (2, PARENT_ID)

    def test_reply_from_corporate_to_personal_is_still_held(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "builder", "architect", kind=Kind.REPLY, in_reply_to=PARENT_ID)
        facts = PolicyFacts(
            parent=ParentFacts(context_id=PARENT_ID, hop=0, source="architect", to="builder")
        )

        assert policy.check(req, facts).status is Status.HELD

    @pytest.mark.parametrize("kind", [Kind.INFORM, Kind.REPLY, Kind.DONE])
    def test_only_requests_require_an_ack(self, roster: Roster, policy: Policy, kind: Kind) -> None:
        req = request(roster, "architect", "builder", kind=kind, in_reply_to=PARENT_ID)

        assert policy.check(req, reply_facts(0)).ack_required is False


class TestBody:
    def test_body_limit_is_32000_characters(self) -> None:
        assert MAX_BODY_CHARS == 32_000

    def test_body_of_exactly_32000_characters_is_accepted(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "architect", "builder", body="x" * 32_000)

        assert policy.check(req, NO_FACTS).status is Status.QUEUED

    def test_body_one_over_the_limit_is_too_large(self, roster: Roster, policy: Policy) -> None:
        req = request(roster, "architect", "builder", body="x" * 32_001)

        with pytest.raises(RelayError) as caught:
            policy.check(req, NO_FACTS)

        assert caught.value.code == "body_too_large"

    @pytest.mark.parametrize("kind", [Kind.REQUEST, Kind.INFORM, Kind.REPLY])
    def test_empty_body_is_invalid_for_every_kind_but_done(
        self, roster: Roster, policy: Policy, kind: Kind
    ) -> None:
        req = request(roster, "architect", "builder", kind=kind, body="", in_reply_to=PARENT_ID)

        with pytest.raises(RelayError) as caught:
            policy.check(req, reply_facts(0))

        assert caught.value.code == "invalid_args"

    def test_empty_body_is_fine_for_done(self, roster: Roster, policy: Policy) -> None:
        req = request(
            roster, "architect", "builder", kind=Kind.DONE, body="", in_reply_to=PARENT_ID
        )

        assert policy.check(req, reply_facts(0)).status is Status.QUEUED

    def test_message_id_with_a_space_is_invalid(self, roster: Roster, policy: Policy) -> None:
        req = request(roster, "architect", "builder", msg_id="a b")

        with pytest.raises(RelayError) as caught:
            policy.check(req, NO_FACTS)

        assert caught.value.code == "invalid_args"

    def test_invalid_id_is_reported_before_an_unknown_recipient(
        self, roster: Roster, policy: Policy
    ) -> None:
        req = request(roster, "architect", "ghost", msg_id="a b")

        with pytest.raises(RelayError) as caught:
            policy.check(req, NO_FACTS)

        assert caught.value.code == "invalid_args"


TURN_CAP_TEXT = (
    "context {ctx} reached {cap} messages; ask your user to run: mz-relay context extend {ctx}"
)
CLOSED_TEXT = f"context {PARENT_ID} was closed by builder with kind=done"


def reply(roster: Roster, kind: Kind = Kind.REPLY, source: str = "architect") -> SendRequest:
    to = "builder" if source == "architect" else "architect"
    return request(roster, source, to, kind=kind, in_reply_to=PARENT_ID)


def refusal(policy: Policy, req: SendRequest, facts: PolicyFacts) -> PolicyViolation:
    with pytest.raises(PolicyViolation) as caught:
        policy.check(req, facts)
    return caught.value


class TestTurnCap:
    def test_the_twentieth_message_of_a_context_is_accepted(
        self, roster: Roster, policy: Policy
    ) -> None:
        assert policy.check(reply(roster), context_facts(context_turns=19)).status is Status.QUEUED

    def test_the_twenty_first_message_is_refused_with_the_extend_hint(
        self, roster: Roster, policy: Policy
    ) -> None:
        refused = refusal(policy, reply(roster), context_facts(context_turns=20))

        assert refused.code == "turn_cap"
        assert refused.message == TURN_CAP_TEXT.format(ctx=PARENT_ID, cap=20)

    def test_done_is_accepted_at_the_cap_so_a_capped_context_can_close(
        self, roster: Roster, policy: Policy
    ) -> None:
        facts = context_facts(context_turns=20)

        assert policy.check(reply(roster, Kind.DONE), facts).status is Status.QUEUED

    @pytest.mark.parametrize("cap", [40, 10], ids=["raised", "lowered"])
    def test_a_context_cap_leaves_room_below_it(
        self, roster: Roster, policy: Policy, cap: int
    ) -> None:
        facts = context_facts(context_turns=cap - 1, context_cap=cap)

        assert policy.check(reply(roster), facts).status is Status.QUEUED

    @pytest.mark.parametrize("cap", [40, 10], ids=["raised", "lowered"])
    def test_a_context_cap_replaces_the_roster_cap_in_the_refusal(
        self, roster: Roster, policy: Policy, cap: int
    ) -> None:
        facts = context_facts(context_turns=cap, context_cap=cap)

        refused = refusal(policy, reply(roster), facts)

        assert refused.message == TURN_CAP_TEXT.format(ctx=PARENT_ID, cap=cap)

    def test_the_cap_comes_from_the_roster_policy(self) -> None:
        roster = example_roster("turn_cap = 20", "turn_cap = 5")

        refused = refusal(Policy(roster), reply(roster), context_facts(context_turns=5))

        assert refused.message == TURN_CAP_TEXT.format(ctx=PARENT_ID, cap=5)

    def test_a_message_that_would_be_held_is_refused_instead(
        self, roster: Roster, policy: Policy
    ) -> None:
        refused = refusal(policy, reply(roster, source="builder"), context_facts(context_turns=20))

        assert refused.code == "turn_cap"


class TestInboxCap:
    def test_a_recipient_with_49_open_messages_takes_one_more(
        self, roster: Roster, policy: Policy
    ) -> None:
        facts = context_facts(recipient_open=49)

        assert policy.check(reply(roster), facts).status is Status.QUEUED

    def test_a_recipient_with_50_open_messages_is_full(
        self, roster: Roster, policy: Policy
    ) -> None:
        refused = refusal(policy, reply(roster), context_facts(recipient_open=50))

        assert refused.code == "inbox_full"
        assert refused.message == "builder has 50 open messages (cap 50)"

    def test_the_cap_comes_from_the_roster_policy_and_the_count_is_the_real_one(self) -> None:
        roster = example_roster("inbox_cap = 50", "inbox_cap = 3")

        refused = refusal(Policy(roster), reply(roster), context_facts(recipient_open=7))

        assert refused.message == "builder has 7 open messages (cap 3)"

    def test_done_is_not_exempt_from_a_full_inbox(self, roster: Roster, policy: Policy) -> None:
        refused = refusal(policy, reply(roster, Kind.DONE), context_facts(recipient_open=50))

        assert refused.code == "inbox_full"

    def test_a_message_that_would_be_held_is_refused_instead(
        self, roster: Roster, policy: Policy
    ) -> None:
        refused = refusal(policy, reply(roster, source="builder"), context_facts(recipient_open=50))

        assert (refused.code, refused.message) == (
            "inbox_full",
            "architect has 50 open messages (cap 50)",
        )


class TestClosedContext:
    @pytest.mark.parametrize("kind", [Kind.REPLY, Kind.REQUEST, Kind.INFORM, Kind.DONE])
    def test_a_closed_context_takes_no_message_of_any_kind(
        self, roster: Roster, policy: Policy, kind: Kind
    ) -> None:
        refused = refusal(policy, reply(roster, kind), context_facts(context_closed_by="builder"))

        assert (refused.code, refused.message) == ("context_closed", CLOSED_TEXT)

    def test_an_open_context_is_not_closed(self, roster: Roster, policy: Policy) -> None:
        facts = context_facts(context_closed_by=None)

        assert policy.check(reply(roster), facts).status is Status.QUEUED


class TestGuardOrder:
    def test_a_closed_context_is_reported_before_the_turn_and_inbox_caps(
        self, roster: Roster, policy: Policy
    ) -> None:
        facts = context_facts(context_closed_by="builder", context_turns=20, recipient_open=50)

        assert refusal(policy, reply(roster), facts).code == "context_closed"

    def test_the_turn_cap_is_reported_before_a_full_inbox(
        self, roster: Roster, policy: Policy
    ) -> None:
        facts = context_facts(context_turns=20, recipient_open=50)

        assert refusal(policy, reply(roster), facts).code == "turn_cap"

    def test_the_hop_budget_is_reported_before_a_closed_context(
        self, roster: Roster, policy: Policy
    ) -> None:
        facts = context_facts(parent_hop=4, context_closed_by="builder")

        assert refusal(policy, reply(roster), facts).code == "hop_exceeded"
