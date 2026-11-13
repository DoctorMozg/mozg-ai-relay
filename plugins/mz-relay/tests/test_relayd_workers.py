"""relayd delivery workers: relative wait, retry without notify, idle CPU bound."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field

import pytest
from mz_relay.client import RelayClient
from mz_relay.delivery import DeliveryJob, Outcome
from mz_relay.envelope import Status
from mz_relay.ids import new_id
from mz_relay.paths import Paths
from mz_relay.relayd import MAX_WAIT_S, MIN_WAIT_S, RelayDaemon, worker_timeout
from mz_relay.roster import Harness
from mz_relay.store import Store, StoredMessage
from relay_rig import (
    BOB_SOCKET,
    PROC_WAIT_S,
    WAIT_S,
    FakeAdapter,
    result,
    running_daemon,
    wait_until,
)

OPENCODE = Harness.OPENCODE
NOW = 1.7e9


@dataclass
class Running:
    daemon: RelayDaemon
    paths: Paths
    store: Store
    clients: ExitStack
    by_token: dict[str, RelayClient] = field(default_factory=dict)

    def register(self, name: str, delivery: dict[str, object] | None = None) -> str:
        """Registers over a connection kept open for the test, so the agent stays online."""
        client = RelayClient(self.paths.socket, timeout_s=PROC_WAIT_S)
        self.clients.callback(client.close)
        client.connect(client="mcp")
        args: dict[str, object] = {
            "name": name,
            "harness": "opencode",
            "cwd": str(self.paths.config_dir),
            "pid": os.getpid(),
        }
        if delivery is not None:
            args["delivery"] = delivery
        reply = client.call("register", args)
        self.by_token[str(reply["token"])] = client
        return str(reply["token"])

    def send(self, token: str, to: str) -> str:
        msg_id = new_id()
        args = {"token": token, "id": msg_id, "to": to, "kind": "request", "body": "check it"}
        assert self.by_token[token].call("send", args)["status"] == "queued"
        return msg_id

    def row(self, msg_id: str) -> StoredMessage:
        got = self.store.get("alice", msg_id)
        assert got is not None, f"row alice/{msg_id} missing"
        return got


@contextmanager
def relay_with(paths: Paths, adapter: FakeAdapter) -> Iterator[Running]:
    with (
        running_daemon(paths, {OPENCODE: adapter}) as daemon,
        closing(Store(paths.db)) as store,
        ExitStack() as clients,
    ):
        yield Running(daemon=daemon, paths=paths, store=store, clients=clients)


class TestWorkerTimeout:
    def test_wait_bounds_are_50_ms_and_1_s(self) -> None:
        assert (MIN_WAIT_S, MAX_WAIT_S) == (0.05, 1.0)

    def test_no_next_wakeup_waits_the_maximum(self) -> None:
        assert worker_timeout(None, 1e9) == 1.0

    def test_next_wakeup_in_the_past_waits_the_minimum(self) -> None:
        assert worker_timeout(1e9 - 5.0, 1e9) == 0.05

    def test_near_next_wakeup_waits_the_relative_gap(self) -> None:
        assert worker_timeout(1e9 + 0.3, 1e9) == pytest.approx(0.3)

    def test_far_next_wakeup_is_capped_at_the_maximum(self) -> None:
        assert worker_timeout(1e9 + 1e6, 1e9) == 1.0

    @pytest.mark.parametrize("next_wakeup", [None, 0.0, NOW - 5.0, NOW, NOW + 0.5, NOW + 3600.0])
    def test_result_is_relative_and_never_above_one_second(self, next_wakeup: float | None) -> None:
        assert 0.05 <= worker_timeout(next_wakeup, NOW) <= 1.0


class TestWorkerLoop:
    def test_retry_fires_on_its_own_timer_and_delivers(self, relay_paths: Paths) -> None:
        call_times: list[float] = []

        def on_deliver(job: DeliveryJob) -> None:
            call_times.append(time.monotonic())

        adapter = FakeAdapter(
            outcomes=[result(Outcome.NOT_CONFIRMED, "busy"), result(Outcome.CONFIRMED)],
            on_deliver=on_deliver,
        )
        with relay_with(relay_paths, adapter) as relay:
            alice = relay.register("alice")
            relay.register("bob", delivery=BOB_SOCKET)

            msg_id = relay.send(alice, "bob")

            # Polls the Store directly: inbox or status calls could notify and mask a stalled timer.
            assert wait_until(lambda: relay.row(msg_id).status == Status.DELIVERED, WAIT_S)
            assert relay.row(msg_id).via == "fake"
            assert len(adapter.delivered) == 2
            assert call_times[1] - call_times[0] >= 1.9

    def test_pull_only_row_keeps_idle_worker_near_one_pass_per_second(
        self, relay_paths: Paths
    ) -> None:
        adapter = FakeAdapter()
        with relay_with(relay_paths, adapter) as relay:
            alice = relay.register("alice")
            relay.register("bob")

            msg_id = relay.send(alice, "bob")
            passes_0 = relay.daemon.worker_passes[OPENCODE]
            validates_0 = adapter.validate_calls
            time.sleep(2.0)
            passes = relay.daemon.worker_passes[OPENCODE] - passes_0
            validates = adapter.validate_calls - validates_0

            row = relay.row(msg_id)
            assert (row.status, row.next_attempt_at) == (Status.QUEUED, None)
            assert 1 <= passes <= 4, f"{passes} worker passes in 2 s"
            assert validates <= 1, f"{validates} validate_binding calls in 2 s"

            relay.register("bob", delivery=BOB_SOCKET)

            assert wait_until(lambda: relay.row(msg_id).status == Status.DELIVERED, WAIT_S)
