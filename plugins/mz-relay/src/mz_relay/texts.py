"""Tool-result texts: the one place that words what agents read after send, inbox, ack and peers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

QUEUED = "queued"
HELD = "held"
NO_MESSAGES = "no new messages"
NO_PEERS = "no peers"


def send_text(
    *, msg_id: str, to: str, status: str, recipient_online: bool, ttl_s: int, dedupe: bool
) -> str:
    """Text for a send result; a dedupe hit on a queued or held row repeats the original text."""
    if status == HELD:
        return f"held {msg_id} for user approval — ask your user to run: mz-relay approve {msg_id}"
    if status == QUEUED:
        if recipient_online:
            return f"queued {msg_id} for {to}"
        return f"queued {msg_id} for {to} (offline; waits until it connects, ttl {ttl_s} s)"
    if not dedupe:
        raise ValueError(f"a new send is queued or held, not {status!r}")
    return f"already sent {msg_id} to {to}: {status}"


def _status_line(row: Mapping[str, object]) -> str:
    line = f"- {row['id']} → {row['to']}: {row['status']}"
    reason = row.get("reason")
    return f"{line} ({reason})" if reason else line


def inbox_text(
    messages: Sequence[str],
    awaiting: Sequence[Mapping[str, object]],
    sent: Sequence[Mapping[str, object]],
    notices: Sequence[Mapping[str, object]] = (),
) -> str:
    """Rendered messages first, then the awaiting-ack, sent and notices blocks that have lines."""
    blocks = list(messages) or [NO_MESSAGES]
    if awaiting:
        lines = [f"- {r['id']} from {r['source']} ({r['kind']}, {r['time']})" for r in awaiting]
        blocks.append("\n".join(["awaiting ack:", *lines]))
    if sent:
        blocks.append("\n".join(["sent:", *map(_status_line, sent)]))
    if notices:
        blocks.append("\n".join(["notices:", *map(_status_line, notices)]))
    return "\n\n".join(blocks)


def ack_text(msg_id: str) -> str:
    return f"acked {msg_id}"


def peers_text(me: str, agents: Sequence[Mapping[str, object]]) -> str:
    """One line per agent other than `me`, sorted by name."""
    lines = []
    for agent in sorted(agents, key=lambda a: str(a["name"])):
        if agent["name"] == me:
            continue
        presence = "online" if agent["online"] else "offline"
        line = f"{agent['name']} : {agent['role']} — {agent['description']} [{presence}]"
        if not agent["may_receive_from_me"]:
            line += " (not allowed to message)"
        lines.append(line)
    return "\n".join(lines) if lines else NO_PEERS


def with_notice_suffix(text: str, pending: int) -> str:
    if pending < 0:
        raise ValueError(f"pending notice count must be >= 0, got {pending}")
    if pending == 0:
        return text
    return f"{text}\n({pending} undelivered notice(s) — call inbox)"
