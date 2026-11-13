"""Pi and OpenCode delivery: binding checks and line-JSON frames to the adapter's per-PID socket."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from itertools import accumulate
from pathlib import Path

import pytest
from mz_relay.adapters.socket_adapter import ADAPTER_SOCKET_RE, SocketAdapter
from mz_relay.delivery import (
    BACKOFF_S,
    MAX_ATTEMPTS,
    DeliveryBinding,
    DeliveryJob,
    DeliveryResult,
    HarnessAdapter,
    Outcome,
)
from mz_relay.envelope import Status
from mz_relay.errors import BindingError
from mz_relay.paths import Paths
from mz_relay.roster import AgentSpec, Harness, parse_roster
from mz_relay.store import StoredMessage
from relay_rig import DAVE_PI, PROC_WAIT_S, EngineRig, Message, envelope, mid

REPLY_TIMEOUT_S = 0.3
GRACE_S = 1.0
NOW = 1_000_000.0
MSG = mid(8001)
OTHER = mid(8002)
PID = 123
TEXT = "[mz-relay] Peer message from alice to scout.\n--- message ---\n  naïve — check\n--- end ---"
PREFIX = {Harness.PI: "pi", Harness.OPENCODE: "oc"}
BOTH = pytest.mark.parametrize("harness", [Harness.PI, Harness.OPENCODE], ids=["pi", "opencode"])

ROSTER = """\
version = 1

[agents.alice]
harness = "opencode"
label = "personal"
role = "planner"
description = "Plans the work."

[agents.piper]
harness = "pi"
label = "personal"
role = "builder"
description = "Builds in Pi."

[agents.scout]
harness = "pi"
label = "personal"
role = "scout"
description = "Reads code in Pi."
tools = "read_only"

[agents.helper]
harness = "opencode"
label = "personal"
role = "builder"
description = "Builds in OpenCode."

[agents.warden]
harness = "opencode"
label = "personal"
role = "auditor"
description = "Audits in OpenCode."
tools = "read_only"
"""
AGENT = {
    (Harness.PI, "full"): "piper",
    (Harness.PI, "read_only"): "scout",
    (Harness.OPENCODE, "full"): "helper",
    (Harness.OPENCODE, "read_only"): "warden",
}

# What the fake adapter does with a request: bytes to send, b"" to hang up, None to stay silent.
Answer = Callable[[Message], bytes | None]


def line(obj: object) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def ack(state: str | None, *, msg_id: str | None = None) -> Answer:
    def answer(req: Message) -> bytes:
        reply: Message = {"ack": msg_id or req["id"]}
        if state is not None:
            reply["state"] = state
        return line(reply)

    return answer


def nack(error: str) -> Answer:
    return lambda req: line({"nack": req["id"], "error": error})


def seen(key: str, *, msg_id: str | None = None) -> Answer:
    return lambda req: line({key: msg_id or req["id"]})


def by_op(**answers: Answer) -> Answer:
    return lambda req: answers[str(req["op"])](req)


def silent(_req: Message) -> None:
    return None


def hang_up(_req: Message) -> bytes:
    return b""


def garbage(_req: Message) -> bytes:
    return b"this is not json\n"


class FakeAdapterSocket:
    """An adapter's per-PID socket: one JSON request line in, at most one JSON line out."""

    def __init__(self, path: Path, answer: Answer) -> None:
        self.path = path
        self.answer = answer
        self.requests: list[Message] = []
        # A bad request line or an unexpected op would otherwise end only its server thread.
        self.errors: list[Exception] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen()
        self._listener.settimeout(0.05)
        self._acceptor = threading.Thread(target=self._accept_loop, daemon=True)
        self._acceptor.start()

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with contextlib.suppress(OSError), conn, conn.makefile("rb") as lines:
            raw = lines.readline()
            if not raw:
                return
            try:
                req: Message = json.loads(raw)
                with self._lock:
                    self.requests.append(req)
                reply = self.answer(req)
            except Exception as error:
                with self._lock:
                    self.errors.append(error)
                return
            if reply is None:
                # Silence: hold the connection open until the test ends.
                self._stopped.wait(PROC_WAIT_S)
                return
            if reply:
                conn.sendall(reply)

    def stop(self) -> None:
        self._stopped.set()
        self._listener.close()
        self._acceptor.join(PROC_WAIT_S)
        self.path.unlink(missing_ok=True)
        with self._lock:
            errors = list(self.errors)
        if errors:
            raise errors[0]


class SocketRig:
    """A real SocketAdapter on relay_paths, the test roster, and fake adapter sockets in <run>."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.roster = parse_roster(ROSTER, pw_home=paths.pw_home)
        self.servers: list[FakeAdapterSocket] = []

    def adapter(self, harness: Harness, **timing: float) -> HarnessAdapter:
        """A SocketAdapter with test timings, typed as the engine sees it."""
        timing.setdefault("reply_timeout_s", REPLY_TIMEOUT_S)
        timing.setdefault("seen_grace_s", GRACE_S)
        adapter: HarnessAdapter = SocketAdapter(harness, self.paths, **timing)
        return adapter

    def spec(self, harness: Harness, tools: str = "full") -> AgentSpec:
        return self.roster.get(AGENT[(harness, tools)])

    def sock(self, harness: Harness, pid: int = PID) -> Path:
        return self.paths.runtime_dir / f"{PREFIX[harness]}-{pid}.sock"

    def serve(self, harness: Harness, answer: Answer, pid: int = PID) -> FakeAdapterSocket:
        server = FakeAdapterSocket(self.sock(harness, pid), answer)
        self.servers.append(server)
        return server

    def raw_binding(self, harness: Harness, pid: int = PID) -> dict[str, object]:
        return {"socket": str(self.sock(harness, pid)), "pid": pid}

    def job(
        self,
        harness: Harness,
        *,
        tools: str = "full",
        status: Status = Status.DELIVERING,
        handed_off_ago: float = 0.0,
    ) -> DeliveryJob:
        spec = self.spec(harness, tools)
        handed_off_at = NOW - handed_off_ago if status is Status.HANDED_OFF else None
        row = StoredMessage(
            envelope=envelope(MSG, to=spec.name, at=NOW),
            status=status,
            reason=None,
            via=None,
            attempts=0,
            next_attempt_at=None,
            handed_off_at=handed_off_at,
            lease_until=None,
            created_at=NOW,
            updated_at=NOW,
        )
        binding = DeliveryBinding("socket", self.raw_binding(harness))
        return DeliveryJob(message=row, text=TEXT, spec=spec, binding=binding, now=NOW)

    def deliver(self, harness: Harness, answer: Answer, *, tools: str = "full") -> DeliveryResult:
        self.serve(harness, answer)
        return self.adapter(harness).deliver(self.job(harness, tools=tools))

    def confirm(self, answer: Answer, *, handed_off_ago: float = 0.0) -> DeliveryResult:
        self.serve(Harness.PI, answer)
        job = self.job(Harness.PI, status=Status.HANDED_OFF, handed_off_ago=handed_off_ago)
        return self.adapter(Harness.PI).confirm(job)

    def close(self) -> None:
        """Stops every server, then raises the first error any of them recorded."""
        errors: list[Exception] = []
        for server in self.servers:
            try:
                server.stop()
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]


@pytest.fixture
def sockets(relay_paths: Paths) -> Iterator[SocketRig]:
    built = SocketRig(relay_paths)
    yield built
    built.close()


def refused_socket(path: Path) -> None:
    """Leaves a socket file nobody listens on, as a killed adapter does."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(path))


class TestAdapterSocketName:
    @pytest.mark.parametrize("name", ["pi-123.sock", "oc-1.sock", "pi-4194304.sock"])
    def test_adapter_socket_names_match(self, name: str) -> None:
        assert ADAPTER_SOCKET_RE.match(name)

    @pytest.mark.parametrize(
        "name", ["pi-.sock", "pix-1.sock", "oc-1.sock.bak", "x.sock", "relayd.sock", "pi-1a.sock"]
    )
    def test_other_names_do_not_match(self, name: str) -> None:
        assert not ADAPTER_SOCKET_RE.match(name)


class TestIdentity:
    @BOTH
    def test_via_is_the_harness_value(self, sockets: SocketRig, harness: Harness) -> None:
        assert sockets.adapter(harness).via == harness.value

    @BOTH
    def test_exhausted_rows_become_undeliverable(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        assert sockets.adapter(harness).exhausted_status is Status.UNDELIVERABLE

    @pytest.mark.parametrize("harness", [Harness.CLAUDE, Harness.CODEX])
    def test_a_harness_without_an_adapter_socket_is_refused(
        self, relay_paths: Paths, harness: Harness
    ) -> None:
        with pytest.raises(ValueError, match=harness.value):
            SocketAdapter(harness, relay_paths)

    def test_a_non_positive_reply_timeout_is_refused(self, relay_paths: Paths) -> None:
        with pytest.raises(ValueError, match="reply_timeout_s"):
            SocketAdapter(Harness.PI, relay_paths, reply_timeout_s=0)


class TestBinding:
    @BOTH
    def test_no_binding_gives_no_delivery_channel(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        assert sockets.adapter(harness).validate_binding(sockets.spec(harness), None) is None

    @BOTH
    def test_own_socket_in_run_with_a_pid_is_socket_mode(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        raw = sockets.raw_binding(harness)

        binding = sockets.adapter(harness).validate_binding(sockets.spec(harness), raw)

        assert binding is not None
        assert binding.mode == "socket"
        assert str(binding.data["socket"]) == str(sockets.sock(harness))
        assert binding.data["pid"] == PID

    @BOTH
    def test_a_socket_outside_run_is_refused(self, sockets: SocketRig, harness: Harness) -> None:
        raw = {"socket": f"/tmp/{PREFIX[harness]}-{PID}.sock", "pid": PID}

        with pytest.raises(BindingError) as caught:
            sockets.adapter(harness).validate_binding(sockets.spec(harness), raw)

        assert "inside" in str(caught.value)
        assert str(sockets.paths.runtime_dir) in str(caught.value)
        assert caught.value.code == "binding_invalid"

    @BOTH
    def test_a_socket_escaping_run_through_dotdot_is_refused(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        escaped = sockets.paths.runtime_dir / ".." / f"{PREFIX[harness]}-{PID}.sock"

        with pytest.raises(BindingError):
            sockets.adapter(harness).validate_binding(
                sockets.spec(harness), {"socket": str(escaped), "pid": PID}
            )

    @pytest.mark.parametrize(
        ("harness", "name"),
        [
            (Harness.PI, "x.sock"),
            (Harness.PI, "oc-1.sock"),
            (Harness.OPENCODE, "pi-1.sock"),
            (Harness.OPENCODE, "relayd.sock"),
        ],
    )
    def test_a_socket_name_not_of_this_harness_is_refused(
        self, sockets: SocketRig, harness: Harness, name: str
    ) -> None:
        raw = {"socket": str(sockets.paths.runtime_dir / name), "pid": PID}

        with pytest.raises(BindingError):
            sockets.adapter(harness).validate_binding(sockets.spec(harness), raw)

    @pytest.mark.parametrize(
        "pid",
        ["1", 0, -5, 1.5, None, True],
        ids=["str", "zero", "negative", "float", "none", "bool"],
    )
    def test_a_pid_that_is_not_a_positive_int_is_refused(
        self, sockets: SocketRig, pid: object
    ) -> None:
        raw = {"socket": str(sockets.sock(Harness.PI)), "pid": pid}

        with pytest.raises(BindingError):
            sockets.adapter(Harness.PI).validate_binding(sockets.spec(Harness.PI), raw)

    @pytest.mark.parametrize(
        "raw",
        [{"pid": PID}, {"socket": 5, "pid": PID}, {"socket": "pi-123.sock", "pid": PID}],
        ids=["no-socket", "socket-not-str", "relative"],
    )
    def test_a_missing_or_malformed_socket_is_refused(
        self, sockets: SocketRig, raw: Mapping[str, object]
    ) -> None:
        with pytest.raises(BindingError):
            sockets.adapter(Harness.PI).validate_binding(sockets.spec(Harness.PI), raw)

    def test_a_missing_pid_is_refused(self, sockets: SocketRig) -> None:
        raw = {"socket": str(sockets.sock(Harness.PI))}

        with pytest.raises(BindingError):
            sockets.adapter(Harness.PI).validate_binding(sockets.spec(Harness.PI), raw)


class TestDeliverFrame:
    @BOTH
    def test_a_full_tools_agent_gets_one_deliver_frame_with_gate_full(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        server = sockets.serve(harness, ack("delivered"))

        sockets.adapter(harness).deliver(sockets.job(harness))

        assert server.requests == [{"op": "deliver", "id": MSG, "text": TEXT, "gate": "full"}]

    @BOTH
    def test_a_read_only_agent_gets_gate_read_only(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        server = sockets.serve(harness, ack("delivered"))

        sockets.adapter(harness).deliver(sockets.job(harness, tools="read_only"))

        assert server.requests == [{"op": "deliver", "id": MSG, "text": TEXT, "gate": "read_only"}]


class TestDeliverOutcome:
    @BOTH
    def test_ack_delivered_is_confirmed(self, sockets: SocketRig, harness: Harness) -> None:
        assert sockets.deliver(harness, ack("delivered")).outcome is Outcome.CONFIRMED

    @BOTH
    def test_ack_handed_off_is_handed_off(self, sockets: SocketRig, harness: Harness) -> None:
        assert sockets.deliver(harness, ack("handed_off")).outcome is Outcome.HANDED_OFF

    @pytest.mark.parametrize(
        "answer",
        [ack(None), ack("maybe"), ack("delivered", msg_id=OTHER), garbage],
        ids=["no-state", "unknown-state", "other-id", "not-json"],
    )
    def test_a_bad_reply_is_not_confirmed(self, sockets: SocketRig, answer: Answer) -> None:
        result = sockets.deliver(Harness.OPENCODE, answer)

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert "bad reply" in result.detail

    @BOTH
    def test_a_nack_is_not_confirmed_with_its_error(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        result = sockets.deliver(harness, nack("no session"))

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert "no session" in result.detail

    def test_silence_past_the_reply_timeout_is_not_confirmed(self, sockets: SocketRig) -> None:
        started = time.monotonic()

        result = sockets.deliver(Harness.OPENCODE, silent)

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert "no reply" in result.detail
        elapsed = time.monotonic() - started
        assert REPLY_TIMEOUT_S * 0.9 <= elapsed < REPLY_TIMEOUT_S + 2.0

    def test_a_hang_up_without_reply_is_not_confirmed(self, sockets: SocketRig) -> None:
        assert sockets.deliver(Harness.OPENCODE, hang_up).outcome is Outcome.NOT_CONFIRMED

    @BOTH
    def test_a_missing_socket_is_unreachable(self, sockets: SocketRig, harness: Harness) -> None:
        result = sockets.adapter(harness).deliver(sockets.job(harness))

        assert result.outcome is Outcome.UNREACHABLE

    @BOTH
    def test_a_socket_nobody_listens_on_is_unreachable(
        self, sockets: SocketRig, harness: Harness
    ) -> None:
        refused_socket(sockets.sock(harness))

        result = sockets.adapter(harness).deliver(sockets.job(harness))

        assert result.outcome is Outcome.UNREACHABLE


class TestConfirm:
    def test_confirm_asks_seen_for_the_message_id(self, sockets: SocketRig) -> None:
        sockets.confirm(seen("seen"))

        assert sockets.servers[0].requests == [{"op": "seen", "id": MSG}]

    def test_seen_is_confirmed(self, sockets: SocketRig) -> None:
        assert sockets.confirm(seen("seen")).outcome is Outcome.CONFIRMED

    def test_unseen_within_the_grace_stays_handed_off(self, sockets: SocketRig) -> None:
        result = sockets.confirm(seen("unseen"), handed_off_ago=GRACE_S / 2)

        assert result.outcome is Outcome.HANDED_OFF

    def test_unseen_at_the_grace_is_not_confirmed(self, sockets: SocketRig) -> None:
        result = sockets.confirm(seen("unseen"), handed_off_ago=GRACE_S)

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert result.detail == "not seen within 1 s"

    def test_the_default_grace_is_600_s(self, sockets: SocketRig) -> None:
        sockets.serve(Harness.PI, seen("unseen"))
        adapter = SocketAdapter(Harness.PI, sockets.paths, reply_timeout_s=REPLY_TIMEOUT_S)
        within = sockets.job(Harness.PI, status=Status.HANDED_OFF, handed_off_ago=599.0)
        past = sockets.job(Harness.PI, status=Status.HANDED_OFF, handed_off_ago=600.0)

        assert adapter.confirm(within).outcome is Outcome.HANDED_OFF
        result = adapter.confirm(past)
        assert result.outcome is Outcome.NOT_CONFIRMED
        assert result.detail == "not seen within 600 s"

    def test_a_missing_socket_is_unreachable(self, sockets: SocketRig) -> None:
        job = sockets.job(Harness.PI, status=Status.HANDED_OFF)

        assert sockets.adapter(Harness.PI).confirm(job).outcome is Outcome.UNREACHABLE

    def test_a_socket_nobody_listens_on_is_unreachable(self, sockets: SocketRig) -> None:
        refused_socket(sockets.sock(Harness.PI))
        job = sockets.job(Harness.PI, status=Status.HANDED_OFF)

        assert sockets.adapter(Harness.PI).confirm(job).outcome is Outcome.UNREACHABLE

    @pytest.mark.parametrize(
        "answer",
        [seen("seen", msg_id=OTHER), nack("unknown op"), silent, hang_up, garbage],
        ids=["other-id", "nack", "silence", "hang-up", "not-json"],
    )
    def test_a_failed_seen_query_is_not_confirmed(self, sockets: SocketRig, answer: Answer) -> None:
        assert sockets.confirm(answer).outcome is Outcome.NOT_CONFIRMED


@pytest.fixture
def engine_rig(relay_home: Path, sockets: SocketRig) -> Iterator[EngineRig]:
    """The engine with real SocketAdapters for pi (dave) and opencode (bob)."""
    adapters = {
        Harness.PI: sockets.adapter(Harness.PI),
        Harness.OPENCODE: sockets.adapter(Harness.OPENCODE),
    }
    built = EngineRig(relay_home, adapters, DAVE_PI)
    yield built
    built.store.close()


class TestEngine:
    def test_an_opencode_ack_makes_the_row_delivered_via_opencode(
        self, sockets: SocketRig, engine_rig: EngineRig
    ) -> None:
        sockets.serve(Harness.OPENCODE, ack("delivered"))
        engine_rig.register("bob", sockets.raw_binding(Harness.OPENCODE))
        msg_id = engine_rig.queue("bob")

        engine_rig.engine.run_once(Harness.OPENCODE)

        row = engine_rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "opencode")

    def test_a_pi_hand_off_is_delivered_via_pi_once_seen(
        self, sockets: SocketRig, engine_rig: EngineRig
    ) -> None:
        sockets.serve(Harness.PI, by_op(deliver=ack("handed_off"), seen=seen("seen")))
        engine_rig.register("dave", sockets.raw_binding(Harness.PI))
        msg_id = engine_rig.queue("dave")

        engine_rig.engine.run_once(Harness.PI)
        assert engine_rig.row(msg_id).status is Status.HANDED_OFF
        engine_rig.at(1)
        engine_rig.engine.run_once(Harness.PI)

        row = engine_rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "pi")

    def test_a_pi_message_unseen_past_the_grace_is_retried(
        self, sockets: SocketRig, engine_rig: EngineRig
    ) -> None:
        sockets.serve(Harness.PI, by_op(deliver=ack("handed_off"), seen=seen("unseen")))
        engine_rig.register("dave", sockets.raw_binding(Harness.PI))
        msg_id = engine_rig.queue("dave")

        engine_rig.engine.run_once(Harness.PI)
        engine_rig.at(GRACE_S)
        engine_rig.engine.run_once(Harness.PI)

        row = engine_rig.row(msg_id)
        assert row.status is Status.QUEUED
        assert row.reason == "attempt 1 failed: not seen within 1 s"

    def test_nacks_on_every_attempt_make_the_row_undeliverable(
        self, sockets: SocketRig, engine_rig: EngineRig
    ) -> None:
        sockets.serve(Harness.OPENCODE, nack("no session"))
        engine_rig.register("bob", sockets.raw_binding(Harness.OPENCODE))
        msg_id = engine_rig.queue("bob")

        for offset in accumulate(BACKOFF_S[:MAX_ATTEMPTS]):
            engine_rig.at(offset)
            engine_rig.engine.run_once(Harness.OPENCODE)

        row = engine_rig.row(msg_id)
        assert row.status is Status.UNDELIVERABLE
        assert row.reason is not None
        assert "no session" in row.reason

    def test_a_gone_socket_parks_the_row_and_takes_the_registration_offline(
        self, sockets: SocketRig, engine_rig: EngineRig
    ) -> None:
        token = engine_rig.register("dave", sockets.raw_binding(Harness.PI))
        msg_id = engine_rig.queue("dave")

        engine_rig.engine.run_once(Harness.PI)

        row = engine_rig.row(msg_id)
        assert row.status is Status.QUEUED
        assert row.reason is not None
        assert row.reason.startswith("unreachable: ")
        assert row.reason.endswith("; waits for dave to reconnect")
        assert not engine_rig.online(token)
