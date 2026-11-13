"""Codex delivery: `codex queue` by thread id, confirmed by the message id in its rollout."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import statistics
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from itertools import accumulate
from pathlib import Path
from typing import Protocol

import pytest
from codex_rig import (
    FAKE_CODEX,
    FAKE_ROLLOUT_DAY,
    FAST_PATH,
    MSG,
    FakeCall,
    Mode,
    append_rollout,
    codex_agent,
    codex_job,
    codex_spec,
    fake_calls,
    index_line,
    job_text,
    queued_line,
    rollout_path,
    user_line,
    write_index,
    write_rollout,
)
from mz_relay.adapters.codex import QUEUED_RE, CodexAdapter
from mz_relay.delivery import (
    BACKOFF_S,
    MAX_ATTEMPTS,
    DeliveryJob,
    DeliveryResult,
    HarnessAdapter,
    Outcome,
)
from mz_relay.envelope import Status
from mz_relay.roster import AgentSpec, Harness
from relay_rig import (
    BOB_SOCKET,
    DAVE_PI,
    PROC_WAIT_S,
    EngineRig,
    FakeAdapter,
    InThread,
    mid,
    wait_until,
)

RENAME_HINT = "run /rename reviewer in the Codex TUI"
# Variables a Claude session would leak into relayd; none may reach codex.
CLAUDE_ENV = {
    "CLAUDECODE": "1",
    "CLAUDE_CONFIG_DIR": "/tmp/mzr-claude-cfg",
    "CLAUDE_CODE_SESSION_ID": "s-1",
}
# A message body that must never show up in a log record or an outcome detail.
SECRET_BODY = "body-7f3a-never-logged"


def completed(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["codex"], code, stdout=stdout, stderr=stderr)


class ScriptedRunner:
    """Stands in for running codex: records each call and returns or raises the scripted result."""

    def __init__(self, result: subprocess.CompletedProcess[str] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict[str, str], float]] = []

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(env), timeout_s))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class CodexRig:
    """A Codex home with thread "reviewer" = t-new, and CodexAdapters aimed at the fake codex."""

    def __init__(self, home: Path) -> None:
        self.pw_home = home
        self.codex_home = home / "codex"
        self.log = home / "fake-codex.log"
        self.spawn_dir = home / "fake-codex-spawn"
        self.rollout = rollout_path(self.codex_home, FAKE_ROLLOUT_DAY, "t-new")
        self.index(index_line("t-other", "other"), index_line("t-new", "reviewer"))

    def index(self, *lines: str) -> None:
        write_index(self.codex_home, *lines)

    def base_env(self, mode: Mode, *, path: str | None = None) -> dict[str, str]:
        """relayd's environment: wrong HOME and CODEX_HOME, Claude leftovers, the fake's knobs."""
        return {
            "PATH": path or FAST_PATH,
            "HOME": "/tmp/mzr-wrong-home",
            "CODEX_HOME": "/tmp/mzr-wrong-codex",
            **CLAUDE_ENV,
            "FAKE_CODEX_LOG": str(self.log),
            "FAKE_CODEX_MODE": mode,
            "FAKE_CODEX_MARKER": "m-1",
            "FAKE_CODEX_SPAWN_DIR": str(self.spawn_dir),
        }

    def adapter(
        self,
        mode: Mode = "queue",
        *,
        timeout_s: float = 10.0,
        run: ScriptedRunner | None = None,
        path: str | None = None,
    ) -> CodexAdapter:
        env = self.base_env(mode, path=path)
        built: CodexAdapter
        if run is None:
            built = CodexAdapter(pw_home=self.pw_home, base_env=env, timeout_s=timeout_s)
        else:
            built = CodexAdapter(pw_home=self.pw_home, base_env=env, timeout_s=timeout_s, run=run)
        return built

    def spec(self, codex_bin: str = str(FAKE_CODEX)) -> AgentSpec:
        return codex_spec(self.codex_home, self.pw_home, codex_bin=codex_bin)

    def job(
        self, adapter: HarnessAdapter, status: Status | None = None, *, msg_id: str = MSG
    ) -> DeliveryJob:
        return codex_job(adapter, self.spec(), status, msg_id=msg_id)

    def deliver(
        self, mode: Mode = "queue", *, codex_bin: str = str(FAKE_CODEX), timeout_s: float = 10.0
    ) -> DeliveryResult:
        adapter = self.adapter(mode, timeout_s=timeout_s)
        return adapter.deliver(codex_job(adapter, self.spec(codex_bin)))

    def calls(self) -> list[FakeCall]:
        return fake_calls(self.log)


@pytest.fixture
def codex(relay_home: Path) -> CodexRig:
    return CodexRig(relay_home)


class TestFakeCodex:
    def test_deliver_mode_writes_where_the_rig_layout_puts_the_rollout(
        self, codex: CodexRig
    ) -> None:
        env = {**os.environ, "PATH": FAST_PATH, "CODEX_HOME": str(codex.codex_home)}
        env["FAKE_CODEX_MODE"] = "deliver"
        argv = [str(FAKE_CODEX), "queue", "--thread", "t-new", "--message", "hello"]

        subprocess.run(argv, env=env, capture_output=True, timeout=PROC_WAIT_S, check=True)

        assert rollout_path(codex.codex_home, FAKE_ROLLOUT_DAY, "t-new").is_file()


class TestIdentity:
    def test_via_is_codex(self, codex: CodexRig) -> None:
        assert codex.adapter().via == "codex"

    def test_exhausted_rows_become_undeliverable(self, codex: CodexRig) -> None:
        assert codex.adapter().exhausted_status is Status.UNDELIVERABLE

    def test_the_queued_line_is_recognised_on_any_line(self) -> None:
        output = "warning: update available\nQueued message q-7 for thread t-new\n"

        match = QUEUED_RE.search(output)

        assert match is not None
        assert match.groups() == ("q-7", "t-new")


class TestBinding:
    def test_the_binding_comes_from_the_roster(self, codex: CodexRig) -> None:
        spec = codex.spec()

        binding = codex.adapter().validate_binding(spec, None)

        assert binding is not None
        assert binding.mode == "codex"
        assert Path(str(binding.data["codex_home"])) == codex.codex_home
        assert binding.data["codex_thread"] == "reviewer"
        assert binding.data["codex_bin"] == str(FAKE_CODEX)

    def test_a_client_binding_is_ignored(self, codex: CodexRig) -> None:
        adapter, spec = codex.adapter(), codex.spec()

        from_client = adapter.validate_binding(spec, {"socket": "/tmp/mzr-x.sock", "pid": 7})

        assert from_client == adapter.validate_binding(spec, None)


class TestInvocation:
    def test_codex_queue_gets_the_thread_id_and_the_text_as_one_argument(
        self, codex: CodexRig
    ) -> None:
        codex.deliver()

        argv = codex.calls()[0]["argv"]
        assert argv == [
            str(FAKE_CODEX),
            "queue",
            "--thread",
            "t-new",
            "--message",
            job_text(),
        ]
        assert "-c" not in argv

    def test_codex_runs_in_the_agents_codex_home_and_the_passwd_home(self, codex: CodexRig) -> None:
        codex.deliver()

        env = codex.calls()[0]["env"]
        assert env["CODEX_HOME"] == str(codex.codex_home)
        assert env["HOME"] == str(codex.pw_home)

    def test_no_claude_variable_reaches_codex(self, codex: CodexRig) -> None:
        codex.deliver()

        assert [key for key in codex.calls()[0]["env"] if key.startswith("CLAUDE")] == []

    def test_other_base_env_variables_reach_codex(self, codex: CodexRig) -> None:
        codex.deliver()

        assert codex.calls()[0]["env"]["FAKE_CODEX_MARKER"] == "m-1"

    def test_a_codex_bin_on_path_is_found_through_the_base_env_path(self, codex: CodexRig) -> None:
        fakes = FAKE_CODEX.parent
        adapter = codex.adapter(path=f"{fakes}{os.pathsep}{FAST_PATH}")

        result = adapter.deliver(codex_job(adapter, codex.spec(codex_bin="codex")))

        assert result.outcome is Outcome.HANDED_OFF
        assert [Path(call["argv"][0]).name for call in codex.calls()] == ["codex"]

    def test_the_runner_gets_the_configured_timeout(self, codex: CodexRig) -> None:
        runner = ScriptedRunner(completed(0, queued_line("t-new") + "\n"))
        adapter = codex.adapter(run=runner, timeout_s=7.5)

        adapter.deliver(codex.job(adapter))

        assert [timeout for _, _, timeout in runner.calls] == [7.5]

    def test_deliver_leaves_the_base_env_it_was_given_unchanged(self, codex: CodexRig) -> None:
        env = codex.base_env("queue")
        before = dict(env)
        adapter = CodexAdapter(pw_home=codex.pw_home, base_env=env)

        adapter.deliver(codex.job(adapter))

        assert env == before

    def test_each_agent_gets_its_own_codex_home(self, codex: CodexRig) -> None:
        second_home = codex.pw_home / "codex-2"
        write_index(second_home, index_line("t-2", "reviewer"))
        adapter = codex.adapter()

        adapter.deliver(codex_job(adapter, codex.spec()))
        adapter.deliver(codex_job(adapter, codex_spec(second_home, codex.pw_home)))

        homes = [call["env"]["CODEX_HOME"] for call in codex.calls()]
        assert homes == [str(codex.codex_home), str(second_home)]


class TestDeliver:
    def test_a_queued_message_is_handed_off(self, codex: CodexRig) -> None:
        assert codex.deliver("queue").outcome is Outcome.HANDED_OFF

    def test_the_queued_line_real_codex_prints_is_handed_off(self, codex: CodexRig) -> None:
        adapter = codex.adapter(run=ScriptedRunner(completed(0, queued_line("t-new") + "\n")))

        result = adapter.deliver(codex.job(adapter))

        assert result.outcome is Outcome.HANDED_OFF

    def test_a_queued_line_after_other_output_is_handed_off(self, codex: CodexRig) -> None:
        output = "warning: update available\nQueued message q-9 for thread t-new\n"
        adapter = codex.adapter(run=ScriptedRunner(completed(0, output)))

        result = adapter.deliver(codex.job(adapter))

        assert result.outcome is Outcome.HANDED_OFF

    def test_a_missing_thread_is_not_confirmed_with_the_rename_hint_and_runs_nothing(
        self, codex: CodexRig
    ) -> None:
        codex.index(index_line("t-other", "other"))

        result = codex.deliver()

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert RENAME_HINT in result.detail
        assert codex.calls() == []

    def test_a_missing_codex_bin_is_not_confirmed(self, codex: CodexRig) -> None:
        result = codex.deliver(codex_bin="/nope")

        assert (result.outcome, result.detail) == (Outcome.NOT_CONFIRMED, "codex not found: /nope")

    def test_a_codex_that_hangs_is_cut_off_at_the_timeout(self, codex: CodexRig) -> None:
        started = time.monotonic()

        result = codex.deliver("sleep", timeout_s=0.5)

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert result.detail == "codex queue timed out after 0.5 s"
        assert time.monotonic() - started < 3.0

    def test_a_timeout_also_kills_what_codex_started(self, codex: CodexRig) -> None:
        # The fake starts a child that writes its marker 1 s after it is alive, then hangs.
        result = codex.deliver("spawn", timeout_s=1.0)

        assert result.detail == "codex queue timed out after 1 s"
        assert (codex.spawn_dir / "alive").exists(), "the fake's child never started"
        assert not wait_until((codex.spawn_dir / "marker").exists, 2.0)

    def test_a_text_with_a_nul_byte_is_not_confirmed_and_never_runs_codex(
        self, codex: CodexRig
    ) -> None:
        adapter = codex.adapter()
        job = dataclasses.replace(codex.job(adapter), text=job_text() + "\x00")

        result = adapter.deliver(job)

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert "NUL" in result.detail
        assert codex.calls() == []

    def test_exit_0_without_the_queued_line_is_not_confirmed(self, codex: CodexRig) -> None:
        result = codex.deliver("garbage")

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert "unexpected codex output" in result.detail

    def test_a_failing_codex_reports_its_stderr(self, codex: CodexRig) -> None:
        result = codex.deliver("fail")

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert "thread busy" in result.detail

    def test_a_long_stderr_is_cut_to_its_last_200_chars(self, codex: CodexRig) -> None:
        stderr = "x" * 300 + "TAIL-MARK\n"
        adapter = codex.adapter(run=ScriptedRunner(completed(1, "", stderr)))

        result = adapter.deliver(codex.job(adapter))

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert "TAIL-MARK" in result.detail
        assert len(result.detail) <= 200

    def test_a_failure_with_empty_stderr_still_says_something(self, codex: CodexRig) -> None:
        adapter = codex.adapter(run=ScriptedRunner(completed(3, "", "")))

        result = adapter.deliver(codex.job(adapter))

        assert result.outcome is Outcome.NOT_CONFIRMED
        assert result.detail.strip()


class TestNeverLogged:
    @pytest.mark.parametrize(
        ("mode", "codex_bin", "timeout_s"),
        [
            ("queue", str(FAKE_CODEX), 10.0),
            ("fail", str(FAKE_CODEX), 10.0),
            ("sleep", str(FAKE_CODEX), 0.5),
            ("queue", "/nope", 10.0),
        ],
        ids=["handed-off", "codex-fails", "timeout", "missing-bin"],
    )
    def test_the_message_body_is_in_no_log_record_and_no_detail(
        self,
        codex: CodexRig,
        caplog: pytest.LogCaptureFixture,
        mode: Mode,
        codex_bin: str,
        timeout_s: float,
    ) -> None:
        adapter = codex.adapter(mode, timeout_s=timeout_s)
        job = codex_job(adapter, codex.spec(codex_bin))
        job = dataclasses.replace(job, text=f"{job.text}\n  {SECRET_BODY}")

        with caplog.at_level(logging.DEBUG):
            result = adapter.deliver(job)

        assert SECRET_BODY not in caplog.text
        assert [r for r in caplog.records if SECRET_BODY in r.getMessage()] == []
        assert SECRET_BODY not in result.detail


class TestConfirm:
    def test_the_message_id_in_the_rollout_is_confirmed(self, codex: CodexRig) -> None:
        adapter = codex.adapter("deliver")
        assert adapter.deliver(codex.job(adapter)).outcome is Outcome.HANDED_OFF

        result = adapter.confirm(codex.job(adapter, Status.HANDED_OFF))

        assert result.outcome is Outcome.CONFIRMED

    def test_no_rollout_line_yet_stays_handed_off(self, codex: CodexRig) -> None:
        adapter = codex.adapter("queue")
        adapter.deliver(codex.job(adapter))

        result = adapter.confirm(codex.job(adapter, Status.HANDED_OFF))

        assert result.outcome is Outcome.HANDED_OFF

    def test_a_rollout_of_other_messages_stays_handed_off(self, codex: CodexRig) -> None:
        other = json.dumps({"type": "response_item", "payload": {"text": f"id: {mid(9002)}"}})
        write_rollout(codex.codex_home, FAKE_ROLLOUT_DAY, "t-new", text=other + "\n")
        adapter = codex.adapter()

        result = adapter.confirm(codex.job(adapter, Status.HANDED_OFF))

        assert result.outcome is Outcome.HANDED_OFF

    def test_a_thread_that_no_longer_resolves_stays_handed_off(self, codex: CodexRig) -> None:
        adapter = codex.adapter()
        job = codex.job(adapter, Status.HANDED_OFF)
        codex.index(index_line("t-other", "other"))

        assert adapter.confirm(job).outcome is Outcome.HANDED_OFF


class TestIncrementalConfirm:
    """Confirm reads only what the rollout gained since deliver, never the history before it."""

    def test_only_rollout_lines_written_after_deliver_count(self, codex: CodexRig) -> None:
        append_rollout(codex.rollout, user_line(job_text()))
        adapter = codex.adapter("queue")
        adapter.deliver(codex.job(adapter))
        handed_off = codex.job(adapter, Status.HANDED_OFF)

        assert adapter.confirm(handed_off).outcome is Outcome.HANDED_OFF
        append_rollout(codex.rollout, user_line(job_text()))
        assert adapter.confirm(handed_off).outcome is Outcome.CONFIRMED

    def test_a_confirm_with_no_deliver_before_it_reads_the_whole_rollout(
        self, codex: CodexRig
    ) -> None:
        # After a relayd restart the adapter has no offset for the row.
        append_rollout(codex.rollout, user_line(job_text()))
        adapter = codex.adapter()

        result = adapter.confirm(codex.job(adapter, Status.HANDED_OFF))

        assert result.outcome is Outcome.CONFIRMED

    def test_a_new_rollout_of_the_thread_is_read_from_its_start(self, codex: CodexRig) -> None:
        old = write_rollout(
            codex.codex_home, FAKE_ROLLOUT_DAY, "t-new", text=user_line("x" * 4096), mtime=1.0
        )
        adapter = codex.adapter("queue")
        adapter.deliver(codex.job(adapter))
        new = write_rollout(codex.codex_home, "2026/11/14", "t-new", text=user_line(job_text()))

        result = adapter.confirm(codex.job(adapter, Status.HANDED_OFF))

        assert new.stat().st_size < old.stat().st_size
        assert result.outcome is Outcome.CONFIRMED

    def test_a_line_still_being_written_is_read_again_once_complete(self, codex: CodexRig) -> None:
        adapter = codex.adapter("queue")
        adapter.deliver(codex.job(adapter))
        handed_off = codex.job(adapter, Status.HANDED_OFF)
        line = user_line(job_text())
        cut = line.index("kind: request") + len("kind")

        append_rollout(codex.rollout, line[:cut])
        assert adapter.confirm(handed_off).outcome is Outcome.HANDED_OFF
        append_rollout(codex.rollout, line[cut:])
        assert adapter.confirm(handed_off).outcome is Outcome.CONFIRMED

    def test_confirm_passes_do_not_reread_the_rollout_before_deliver(self, codex: CodexRig) -> None:
        # A full re-read per pass costs PASSES full scans; the bound allows a quarter of that,
        # while a pass reading nothing new costs well under a tenth of one scan of 32 MB.
        passes = 20
        append_rollout(codex.rollout, user_line("x" * 1000) * 32_000)
        full_scan = statistics.median(seconds(scan_whole_rollout, codex.rollout) for _ in range(3))
        adapter = codex.adapter("queue")
        adapter.deliver(codex.job(adapter))
        handed_off = codex.job(adapter, Status.HANDED_OFF)

        started = time.perf_counter()
        outcomes = {adapter.confirm(handed_off).outcome for _ in range(passes)}
        elapsed = time.perf_counter() - started

        assert outcomes == {Outcome.HANDED_OFF}
        assert elapsed < passes * full_scan / 4
        append_rollout(codex.rollout, user_line(job_text()))
        assert adapter.confirm(handed_off).outcome is Outcome.CONFIRMED


def scan_whole_rollout(path: Path, nonce: str) -> bool:
    needle = nonce.encode("utf-8")
    with path.open("rb") as handle:
        return any(needle in line for line in handle)


def seconds(scan: Callable[[Path, str], bool], path: Path) -> float:
    started = time.perf_counter()
    scan(path, "absent-nonce")
    return time.perf_counter() - started


class TestNonce:
    """The nonce is the delivered text's `id: <id>  kind: <kind>`, not the bare id."""

    ID = "response"

    def handed_off(self, codex: CodexRig) -> tuple[CodexAdapter, DeliveryJob]:
        adapter = codex.adapter("queue")
        adapter.deliver(codex.job(adapter, msg_id=self.ID))
        return adapter, codex.job(adapter, Status.HANDED_OFF, msg_id=self.ID)

    def test_rollout_lines_holding_a_word_like_id_do_not_confirm_it(self, codex: CodexRig) -> None:
        adapter, job = self.handed_off(codex)
        tool_output = {"type": "response_item", "payload": {"type": "function_call_output"}}
        append_rollout(codex.rollout, json.dumps(tool_output) + "\n")
        append_rollout(codex.rollout, user_line(f"the response looks fine; see id: {self.ID}"))

        assert adapter.confirm(job).outcome is Outcome.HANDED_OFF

    def test_the_delivered_id_line_confirms_a_word_like_id(self, codex: CodexRig) -> None:
        adapter, job = self.handed_off(codex)
        append_rollout(codex.rollout, user_line(job.text))

        assert adapter.confirm(job).outcome is Outcome.CONFIRMED


class TestWatchedRows:
    """The adapter keeps a rollout offset per handed-off row and forgets it once the row ends."""

    def test_a_handed_off_row_is_watched_until_it_is_confirmed(self, codex: CodexRig) -> None:
        adapter = codex.adapter("deliver")
        adapter.deliver(codex.job(adapter))
        assert adapter.watched_rows == 1

        adapter.confirm(codex.job(adapter, Status.HANDED_OFF))

        assert adapter.watched_rows == 0

    def test_a_row_delivered_again_is_watched_once(self, codex: CodexRig) -> None:
        adapter = codex.adapter("queue")

        adapter.deliver(codex.job(adapter))
        adapter.deliver(codex.job(adapter))
        adapter.deliver(codex.job(adapter, msg_id=mid(9002)))

        assert adapter.watched_rows == 2

    def test_a_delivery_that_fails_leaves_nothing_watched(self, codex: CodexRig) -> None:
        adapter = codex.adapter("fail")

        adapter.deliver(codex.job(adapter))

        assert adapter.watched_rows == 0


class MakeEngine(Protocol):
    def __call__(
        self, mode: Mode = "deliver", *, codex_bin: str = ..., timeout_s: float = ...
    ) -> EngineRig: ...


@pytest.fixture
def make_engine(relay_home: Path, codex: CodexRig) -> Iterator[MakeEngine]:
    """An engine whose codex worker uses a real CodexAdapter; dave (pi) has a FakeAdapter."""
    rigs: list[EngineRig] = []

    def make(
        mode: Mode = "deliver", *, codex_bin: str = str(FAKE_CODEX), timeout_s: float = 10.0
    ) -> EngineRig:
        adapters: dict[Harness, HarnessAdapter] = {
            Harness.CODEX: codex.adapter(mode, timeout_s=timeout_s),
            Harness.PI: FakeAdapter(),
        }
        agents = codex_agent(codex.codex_home, codex_bin=codex_bin) + DAVE_PI
        built = EngineRig(relay_home, adapters, agents)
        rigs.append(built)
        return built

    yield make
    for built in rigs:
        built.store.close()


def run_every_attempt(rig: EngineRig) -> None:
    # Each attempt is due once the previous one's backoff has passed: +0, +2 and +10 s today.
    for offset in accumulate(BACKOFF_S[:MAX_ATTEMPTS]):
        rig.at(offset)
        rig.engine.run_once(Harness.CODEX)


class TestEngine:
    def test_a_message_is_delivered_via_codex_once_the_rollout_shows_it(
        self, make_engine: MakeEngine
    ) -> None:
        rig = make_engine("deliver")
        msg_id = rig.queue("reviewer")

        rig.engine.run_once(Harness.CODEX)
        assert rig.row(msg_id).status is Status.HANDED_OFF
        rig.at(1)
        rig.engine.run_once(Harness.CODEX)

        row = rig.row(msg_id)
        assert (row.status, row.via) == (Status.DELIVERED, "codex")

    def test_a_missing_codex_bin_ends_undeliverable_with_the_reason(
        self, make_engine: MakeEngine
    ) -> None:
        rig = make_engine(codex_bin="/nope")
        msg_id = rig.queue("reviewer")

        run_every_attempt(rig)

        row = rig.row(msg_id)
        assert (row.status, row.reason) == (Status.UNDELIVERABLE, "codex not found: /nope")

    def test_a_missing_thread_ends_undeliverable_with_the_rename_hint(
        self, make_engine: MakeEngine, codex: CodexRig
    ) -> None:
        codex.index(index_line("t-other", "other"))
        rig = make_engine()
        msg_id = rig.queue("reviewer")

        run_every_attempt(rig)

        row = rig.row(msg_id)
        assert row.status is Status.UNDELIVERABLE
        assert row.reason is not None
        assert RENAME_HINT in row.reason

    def test_a_slow_codex_does_not_hold_up_a_pi_delivery(self, make_engine: MakeEngine) -> None:
        # The fake sleeps 5 s; codex is cut at 3 s, so the pi pass has 2 s of margin.
        rig = make_engine("sleep", timeout_s=3.0)
        rig.register("dave", BOB_SOCKET)
        codex_row, pi_row = rig.queue("reviewer"), rig.queue("dave")

        codex_pass = InThread(lambda: rig.engine.run_once(Harness.CODEX))
        in_flight = wait_until(lambda: rig.row(codex_row).status is Status.DELIVERING, PROC_WAIT_S)
        assert in_flight, "the codex row was never claimed"
        InThread(lambda: rig.engine.run_once(Harness.PI)).result(2.0)

        assert rig.row(pi_row).status is Status.DELIVERED
        assert not codex_pass.done()
        codex_pass.result(PROC_WAIT_S)
