"""Pi and OpenCode delivery: one line-JSON exchange with the adapter's per-PID socket."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import socket
from collections.abc import Mapping
from pathlib import Path

from mz_relay.delivery import DeliveryBinding, DeliveryJob, DeliveryResult, Outcome
from mz_relay.envelope import Status
from mz_relay.errors import BindingError, ProtocolError
from mz_relay.paths import Paths
from mz_relay.protocol import LineReader
from mz_relay.roster import USER_NAME, AgentSpec, Harness, ToolMode

log = logging.getLogger(__name__)

# \Z instead of $, so a name with a trailing newline does not pass re.match.
ADAPTER_SOCKET_RE = re.compile(r"^(pi|oc)-[0-9]+\.sock\Z")
SOCKET = "socket"
_PREFIX: Mapping[Harness, str] = {Harness.PI: "pi", Harness.OPENCODE: "oc"}
_ACK_STATES: Mapping[object, Outcome] = {
    "handed_off": Outcome.HANDED_OFF,
    "delivered": Outcome.CONFIRMED,
}
# Adapter error texts are shown in reasons and logs; a runaway one is cut.
_ERROR_MAX = 200


class _Unreachable(Exception):
    """The adapter socket is gone or nobody listens on it."""


class _Failed(Exception):
    """The exchange happened but proved nothing; str(exc) is the detail."""


class SocketAdapter:
    """Delivers to a Pi extension or OpenCode plugin through its socket in the runtime dir."""

    exhausted_status: Status = Status.UNDELIVERABLE

    def __init__(
        self,
        harness: Harness,
        paths: Paths,
        *,
        reply_timeout_s: float = 5.0,
        seen_grace_s: float = 600.0,
    ) -> None:
        if harness not in _PREFIX:
            raise ValueError(f"harness {harness} has no adapter socket (pi or opencode only)")
        if not isinstance(paths, Paths):
            raise ValueError(f"paths must be a Paths, got {type(paths).__name__}")
        for name, value in (("reply_timeout_s", reply_timeout_s), ("seen_grace_s", seen_grace_s)):
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        self.via: str = harness.value
        self._prefix = _PREFIX[harness]
        self._runtime_dir = Path(os.path.normpath(paths.runtime_dir))
        self._reply_timeout_s = reply_timeout_s
        self._seen_grace_s = seen_grace_s

    def validate_binding(
        self, spec: AgentSpec, binding: Mapping[str, object] | None
    ) -> DeliveryBinding | None:
        if binding is None:
            return None
        if not isinstance(binding, Mapping):
            raise BindingError(
                f"{self.via} delivery must be an object, got {type(binding).__name__}"
            )
        self._socket_path(binding)
        pid = binding.get("pid")
        # bool is an int subclass; true/false is not a pid.
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise BindingError(f"{self.via} delivery needs pid as a positive integer, got {pid!r}")
        return DeliveryBinding(SOCKET, dict(binding))

    def deliver(self, job: DeliveryJob) -> DeliveryResult:
        env = job.message.envelope
        # The user's own words never run under the read-only gate meant for peers.
        gate = ToolMode.FULL.value if env.source == USER_NAME else job.spec.tools.value
        frame = {"op": "deliver", "id": env.id, "text": job.text, "gate": gate}
        try:
            reply = self._exchange(job.binding, frame)
            outcome = self._ack_outcome(reply, env.id)
        except _Unreachable as exc:
            log.info("%s to %s unreachable: %s", env.id, env.to, exc)
            return DeliveryResult(Outcome.UNREACHABLE, str(exc))
        except _Failed as exc:
            log.info("%s to %s not confirmed: %s", env.id, env.to, exc)
            return DeliveryResult(Outcome.NOT_CONFIRMED, str(exc))
        log.info("%s to %s acked by the %s adapter: %s", env.id, env.to, self.via, outcome.value)
        return DeliveryResult(outcome)

    def confirm(self, job: DeliveryJob) -> DeliveryResult:
        env = job.message.envelope
        try:
            reply = self._exchange(job.binding, {"op": "seen", "id": env.id})
            seen = self._seen(reply, env.id)
        except _Unreachable as exc:
            log.info("%s to %s unreachable while handed off: %s", env.id, env.to, exc)
            return DeliveryResult(Outcome.UNREACHABLE, str(exc))
        except _Failed as exc:
            log.info("%s to %s seen query failed: %s", env.id, env.to, exc)
            return DeliveryResult(Outcome.NOT_CONFIRMED, str(exc))
        if seen:
            log.info("%s to %s seen by the %s adapter", env.id, env.to, self.via)
            return DeliveryResult(Outcome.CONFIRMED)
        handed_off_at = job.message.handed_off_at
        if handed_off_at is not None and job.now - handed_off_at < self._seen_grace_s:
            return DeliveryResult(Outcome.HANDED_OFF)
        detail = f"not seen within {self._seen_grace_s:g} s"
        log.info("%s to %s not confirmed: %s", env.id, env.to, detail)
        return DeliveryResult(Outcome.NOT_CONFIRMED, detail)

    def _socket_path(self, binding: Mapping[str, object]) -> Path:
        raw = binding.get(SOCKET)
        if not isinstance(raw, str) or not raw:
            raise BindingError(f"{self.via} delivery needs socket as a non-empty string")
        if not os.path.isabs(raw):
            raise BindingError(f"adapter socket {raw} must be an absolute path")
        # Lexical normalization, so <run>/../pi-1.sock is judged by where it really points.
        path = Path(os.path.normpath(raw))
        if path.parent != self._runtime_dir:
            raise BindingError(f"adapter socket {raw} must be directly inside {self._runtime_dir}")
        match = ADAPTER_SOCKET_RE.match(path.name)
        if match is None or match.group(1) != self._prefix:
            raise BindingError(
                f"adapter socket name {path.name} is not {self._prefix}-<pid>.sock for {self.via}"
            )
        return path

    def _exchange(self, binding: DeliveryBinding, frame: Mapping[str, object]) -> dict[str, object]:
        """Sends one request line and reads one reply line, per S5's one exchange per connection."""
        path = self._socket_path(binding.data)
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self._reply_timeout_s)
            try:
                conn.connect(str(path))
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                raise _Unreachable(f"adapter socket {path}: {exc.strerror}") from exc
            except OSError as exc:
                raise _Failed(f"connect to {path} failed: {type(exc).__name__}") from exc
            try:
                conn.sendall(data)
                line = LineReader(conn).next_line()
            except TimeoutError as exc:
                raise _Failed(f"no reply within {self._reply_timeout_s:g} s") from exc
            except ProtocolError as exc:
                raise _Failed(f"bad reply: {exc}") from exc
            except OSError as exc:
                raise _Failed(f"exchange with {path} failed: {type(exc).__name__}") from exc
        if line is None:
            raise _Failed("no reply: the adapter hung up")
        try:
            reply = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise _Failed("bad reply: not JSON") from exc
        if not isinstance(reply, dict):
            raise _Failed(f"bad reply: expected an object, got {type(reply).__name__}")
        return reply

    def _ack_outcome(self, reply: Mapping[str, object], msg_id: str) -> Outcome:
        _raise_on_nack(reply, msg_id)
        if reply.get("ack") != msg_id:
            raise _Failed("bad reply: no ack for this message")
        outcome = _ACK_STATES.get(reply.get("state"))
        if outcome is None:
            raise _Failed("bad reply: ack state is not handed_off or delivered")
        return outcome

    def _seen(self, reply: Mapping[str, object], msg_id: str) -> bool:
        _raise_on_nack(reply, msg_id)
        if reply.get("seen") == msg_id:
            return True
        if reply.get("unseen") == msg_id:
            return False
        raise _Failed("bad reply: no seen or unseen for this message")


def _raise_on_nack(reply: Mapping[str, object], msg_id: str) -> None:
    if "nack" not in reply:
        return
    # An unknown op is nacked with an empty id, since the adapter may not know which message.
    if reply["nack"] not in (msg_id, ""):
        raise _Failed("bad reply: nack for another message")
    error = reply.get("error")
    text = error[:_ERROR_MAX] if isinstance(error, str) and error else "no error text"
    raise _Failed(f"adapter refused: {text}")
