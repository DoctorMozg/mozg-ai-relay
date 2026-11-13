"""Send policy: who may message whom, reply chains, the hop budget, body limits and holds."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mz_relay.envelope import Kind, Status
from mz_relay.errors import EnvelopeError, PolicyViolation
from mz_relay.ids import valid_id
from mz_relay.roster import AgentSpec, Label, Roster

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 32_000


@dataclass(frozen=True)
class SendRequest:
    msg_id: str
    source: AgentSpec
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
    context_closed: bool = False
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
        recipient = self._recipient(req)
        self._check_body(req)
        parent = facts.parent
        if req.in_reply_to is not None and parent is None:
            raise PolicyViolation(
                "unknown_parent",
                f"no message {req.in_reply_to} was delivered to {req.source.name}",
            )
        context_id = self._context_id(req, parent)
        hop = self._hop(parent)
        return Decision(
            status=Status.HELD if self._held(req.source, recipient) else Status.QUEUED,
            hop=hop,
            context_id=context_id,
            label=req.source.label,
            ack_required=req.kind is Kind.REQUEST,
            ttl_s=self._roster.policy.default_ttl_s,
        )

    def _recipient(self, req: SendRequest) -> AgentSpec:
        recipient = self._roster.agents.get(req.to)
        if recipient is None:
            raise PolicyViolation("unknown_recipient", f"{req.to!r} is not in the roster")
        # may_send_to may name the agent itself, so echo is checked first to refuse it anyway.
        if recipient.name == req.source.name:
            raise PolicyViolation("echo_to_origin", f"{req.source.name} cannot message itself")
        if recipient.name not in req.source.may_send_to:
            raise PolicyViolation(
                "not_allowed", f"{req.source.name} may not send to {recipient.name}"
            )
        return recipient

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

    def _held(self, source: AgentSpec, recipient: AgentSpec) -> bool:
        return (
            self._roster.policy.hold_corporate_to_personal
            and source.label is Label.CORPORATE
            and recipient.label is Label.PERSONAL
        )
