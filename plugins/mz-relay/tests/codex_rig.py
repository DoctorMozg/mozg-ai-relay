"""The Codex home layout the tests write (session index, rollouts) and the fake codex's log.

Imports of the Codex adapter stay out of this module, so the thread tests load without it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from relay_rig import TESTS, envelope, mid

if TYPE_CHECKING:
    from mz_relay.delivery import DeliveryJob, HarnessAdapter
    from mz_relay.envelope import Status
    from mz_relay.roster import AgentSpec

FAKE_CODEX = TESTS / "fakes" / "codex"
# Where the fake's deliver mode appends; tests/fakes/codex hardcodes the same day and stamp.
FAKE_ROLLOUT_DAY = "2026/11/13"
FAKE_ROLLOUT_TIME = "10-00-00"
# The fake's `env python3` finds this interpreter first; a pyenv shim costs ~0.3 s per run.
FAST_PATH = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
UPDATED_AT = "2026-11-12T09:00:00Z"
MSG = mid(9001)
NOW = 1_000_000.0
# The queue id real codex prints is a uuid; the fake prints this one.
QUEUE_ID = "019a7c2e-5b1d-7f00-8c3a-2d4e6f8a0b1c"

# How the fake codex behaves; see tests/fakes/codex.
Mode = Literal["queue", "deliver", "fail", "garbage", "sleep", "spawn"]


class FakeCall(TypedDict):
    """One line of the fake's log: how it was run and the env it saw."""

    argv: list[str]
    env: dict[str, str]


def index_line(thread_id: str, name: str, updated_at: str = UPDATED_AT) -> str:
    return json.dumps({"id": thread_id, "thread_name": name, "updated_at": updated_at})


def write_index(codex_home: Path, *lines: object) -> None:
    """Writes session_index.jsonl; str lines go in raw, anything else as JSON."""
    codex_home.mkdir(parents=True, exist_ok=True)
    text = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    (codex_home / "session_index.jsonl").write_text(text, encoding="utf-8")


def rollout_path(
    codex_home: Path, day: str, thread_id: str, time_of_day: str = FAKE_ROLLOUT_TIME
) -> Path:
    """sessions/<yyyy>/<mm>/<dd>/rollout-<yyyy-mm-dd>T<hh-mm-ss>-<id>.jsonl, as Codex names it."""
    stamp = f"{day.replace('/', '-')}T{time_of_day}"
    return codex_home / "sessions" / day / f"rollout-{stamp}-{thread_id}.jsonl"


def write_rollout(
    codex_home: Path, day: str, thread_id: str, *, text: str = "", mtime: float | None = None
) -> Path:
    path = rollout_path(codex_home, day, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def queued_line(thread_id: str) -> str:
    """What real codex prints on success, trailing period included."""
    return f"Queued message {QUEUE_ID} for thread {thread_id}."


def user_line(text: str) -> str:
    """A rollout line for a user message, as Codex writes it once a turn takes the message."""
    content = [{"type": "input_text", "text": text}]
    payload = {"type": "message", "role": "user", "content": content}
    item = {"timestamp": "2026-11-13T10:00:01.000Z", "type": "response_item", "payload": payload}
    return json.dumps(item) + "\n"


def append_rollout(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def job_text(msg_id: str = MSG) -> str:
    """The text relayd hands codex for msg_id; its id line is the one render_for_delivery writes."""
    return (
        "[mz-relay] Peer message from alice to reviewer.\n"
        f"id: {msg_id}  kind: request  context: {msg_id}  hop: 0\n"
        "--- message ---\n  check it\n--- end ---"
    )


def fake_calls(log: Path) -> list[FakeCall]:
    if not log.exists():
        return []
    calls: list[FakeCall] = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    return calls


def codex_agent(codex_home: Path, *, codex_bin: str = str(FAKE_CODEX)) -> str:
    """Roster agent `reviewer` on the Codex thread named "reviewer" in codex_home."""
    return f"""
[agents.reviewer]
harness = "codex"
label = "personal"
role = "reviewer"
description = "Reviews changes in a Codex thread."
codex_home = "{codex_home}"
codex_thread = "reviewer"
codex_bin = "{codex_bin}"
"""


def codex_spec(codex_home: Path, pw_home: Path, *, codex_bin: str = str(FAKE_CODEX)) -> AgentSpec:
    from mz_relay.roster import parse_roster

    roster_text = "version = 1\n" + codex_agent(codex_home, codex_bin=codex_bin)
    return parse_roster(roster_text, pw_home=pw_home).get("reviewer")


def codex_job(
    adapter: HarnessAdapter, spec: AgentSpec, status: Status | None = None, *, msg_id: str = MSG
) -> DeliveryJob:
    """A request job for msg_id through the adapter's own binding, with job_text(msg_id)."""
    from mz_relay.delivery import DeliveryJob
    from mz_relay.envelope import Status
    from mz_relay.store import StoredMessage

    status = status or Status.DELIVERING
    binding = adapter.validate_binding(spec, None)
    assert binding is not None
    row = StoredMessage(
        envelope=envelope(msg_id, to="reviewer", at=NOW),
        status=status,
        reason=None,
        via=None,
        attempts=0,
        next_attempt_at=None,
        handed_off_at=NOW if status is Status.HANDED_OFF else None,
        lease_until=None,
        created_at=NOW,
        updated_at=NOW,
    )
    text = job_text(msg_id)
    return DeliveryJob(message=row, text=text, spec=spec, binding=binding, now=NOW)
