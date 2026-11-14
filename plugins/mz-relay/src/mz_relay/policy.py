"""Send policy: who may message whom, reply chains, the hop budget, body limits and holds."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from mz_relay.envelope import Kind, Status
from mz_relay.errors import EnvelopeError, PolicyViolation
from mz_relay.ids import valid_id
from mz_relay.roster import USER_NAME, Label, Roster

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 32_000
# Nothing delivers to the user; its messages wait for the UI or `mz-relay inbox` this long.
USER_INBOX_TTL_S = 604_800


class Sender(Protocol):
    """Who sends: a roster AgentSpec, or the user."""

    @property
    def name(self) -> str: ...
    @property
    def label(self) -> Label: ...
    @property
    def may_send_to(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class UserSender:
    """The human; may message every roster agent, and its messages are never held."""

    may_send_to: frozenset[str]
    name: str = USER_NAME
    label: Label = Label.PERSONAL


@dataclass(frozen=True)
class SendRequest:
    msg_id: str
    source: Sender
    to: str
    kind: Kind
    body: str
    context_id: str | None
    in_reply_to: str | None


@dataclass(frozen=True)
class ParentFacts:
    context_id: str
    hop: int
    source: str
    to: str


@dataclass(frozen=True)
class PolicyFacts:
    parent: ParentFacts | None
    context_turns: int = 0
    context_cap: int | None = None
    context_closed_by: str | None = None
    recipient_open: int = 0


@dataclass(frozen=True)
class Decision:
    status: Status
    hop: int
    context_id: str
    label: Label
    ack_required: bool
    ttl_s: int


class Policy:
    """Decides whether a send is refused, held for the user, or queued, using one roster."""

    def __init__(self, roster: Roster) -> None:
        self._roster = roster

    def check(self, req: SendRequest, facts: PolicyFacts) -> Decision:
        try:
            decision = self._decide(req, facts)
        except (PolicyViolation, EnvelopeError) as exc:
            log.info("send %r %s -> %r refused: %s", req.msg_id, req.source.name, req.to, exc.code)
            raise
        log.info(
            "send %s %s -> %s %s at hop %d",
            req.msg_id,
            req.source.name,
            req.to,
            decision.status,
            decision.hop,
        )
        return decision

    def _decide(self, req: SendRequest, facts: PolicyFacts) -> Decision:
        if not valid_id(req.msg_id):
            raise EnvelopeError(f"message id is not valid: {req.msg_id!r}", {"field": "id"})
        recipient_label = self._recipient(req)
        self._check_body(req)
        parent = facts.parent
        if req.in_reply_to is not None and parent is None:
            raise PolicyViolation(
                "unknown_parent",
                f"no message {req.in_reply_to} was delivered to {req.source.name}",
            )
        context_id = self._context_id(req, parent)
        from_user = req.source.name == USER_NAME
        # The user's words start a fresh agent chain, so its sends never spend the hop budget.
        hop = 0 if from_user else self._hop(parent)
        self._check_caps(req, context_id, facts, from_user=from_user)
        held = recipient_label is not None and self._held(req.source.label, recipient_label)
        to_user = req.to == USER_NAME
        return Decision(
            status=Status.HELD if held else Status.QUEUED,
            hop=hop,
            context_id=context_id,
            label=req.source.label,
            ack_required=req.kind is Kind.REQUEST,
            ttl_s=USER_INBOX_TTL_S if to_user else self._roster.policy.default_ttl_s,
        )

    def _recipient(self, req: SendRequest) -> Label | None:
        """The recipient's label, None for the user, whom every agent may message unheld."""
        # may_send_to may name the agent itself, so echo is checked first to refuse it anyway.
        if req.to == req.source.name:
            raise PolicyViolation("echo_to_origin", f"{req.source.name} cannot message itself")
        if req.to == USER_NAME:
            return None
        recipient = self._roster.agents.get(req.to)
        if recipient is None:
            raise PolicyViolation("unknown_recipient", f"{req.to!r} is not in the roster")
        if recipient.name not in req.source.may_send_to:
            raise PolicyViolation(
                "not_allowed", f"{req.source.name} may not send to {recipient.name}"
            )
        return recipient.label

    def _check_body(self, req: SendRequest) -> None:
        if not req.body and req.kind is not Kind.DONE:
            raise EnvelopeError(
                f"body is empty; only a {Kind.DONE} message may have no body", {"field": "body"}
            )
        if len(req.body) > MAX_BODY_CHARS:
            raise PolicyViolation(
                "body_too_large",
                f"body has {len(req.body)} characters; the limit is {MAX_BODY_CHARS}",
                {"limit": MAX_BODY_CHARS},
            )

    def _context_id(self, req: SendRequest, parent: ParentFacts | None) -> str:
        # Only replies may name a context, so nobody can fill or close a stranger's context.
        if parent is None:
            if req.context_id is not None:
                raise EnvelopeError(
                    "context_id needs in_reply_to; a new message starts its own context",
                    {"field": "context_id"},
                )
            return req.msg_id
        if req.context_id is not None and req.context_id != parent.context_id:
            raise EnvelopeError("context_id differs from the parent's", {"field": "context_id"})
        return parent.context_id

    def _hop(self, parent: ParentFacts | None) -> int:
        if parent is None:
            return 0
        hop = parent.hop + 1
        budget = self._roster.policy.hop_budget
        if hop > budget:
            raise PolicyViolation(
                "hop_exceeded",
                f"hop {hop} exceeds the hop budget {budget}; ask your user before continuing "
                "this chain",
                {"hop": hop, "hop_budget": budget},
            )
        return hop

    def _check_caps(
        self, req: SendRequest, context_id: str, facts: PolicyFacts, *, from_user: bool
    ) -> None:
        # Runs before the hold, so a message that would be held is refused, not held.
        if facts.context_closed_by is not None:
            raise PolicyViolation(
                "context_closed",
                f"context {context_id} was closed by {facts.context_closed_by} with kind=done",
            )
        policy = self._roster.policy
        cap = policy.turn_cap if facts.context_cap is None else facts.context_cap
        # done is exempt so a context at its cap can still be closed; the user is never capped.
        if not from_user and req.kind is not Kind.DONE and facts.context_turns >= cap:
            raise PolicyViolation(
                "turn_cap",
                f"context {context_id} reached {cap} messages; "
                f"ask your user to run: mz-relay context extend {context_id}",
                {"turn_cap": cap},
            )
        if facts.recipient_open >= policy.inbox_cap:
            raise PolicyViolation(
                "inbox_full",
                f"{req.to} has {facts.recipient_open} open messages (cap {policy.inbox_cap})",
                {"inbox_cap": policy.inbox_cap},
            )

    def _held(self, source: Label, recipient: Label) -> bool:
        return (
            self._roster.policy.hold_corporate_to_personal
            and source is Label.CORPORATE
            and recipient is Label.PERSONAL
        )
