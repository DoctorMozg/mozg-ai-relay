"""Claude session discovery from the MCP child's env, and the per-config-dir session registry."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from fakes_claude import PINNED_VERSION
from mz_relay.adapters.claude_registry import (
    ClaudeRegistry,
    NotInClaudeSession,
    OwnSession,
    RegistryEntry,
    build_frame,
    claude_config_dir,
    discover_own_session,
    pid_alive,
    version_supported,
)
from mz_relay.errors import PathError, RelayError
from relay_rig import FakeClock, SleepHookClock

SOCK = "/run/user/1000/cc-socks/4242.sock"


def registry_json(pid: int, session_id: str, **overrides: object) -> dict[str, object]:
    """A sessions/<pid>.json with every key a real Claude Code registry entry carries."""
    entry: dict[str, object] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": "/w/project",
        "startedAt": 1794571539833,
        "procStart": "23489205",
        "version": PINNED_VERSION,
        "peerProtocol": 1,
        "peerFeatures": ["notify_idle", "reply_across_default_dirs", "artifact_yield"],
        "kind": "interactive",
        "entrypoint": "cli",
        "pidDomain": "linux:x:pid:[4026531836]",
        "asksForConvertedSkills": False,
        "messagingSocketPath": f"/run/user/1000/cc-socks/{pid}.sock",
        "name": "some-session",
        "nameSource": "derived",
        "nameSince": 1794571539833,
        "status": "idle",
        "updatedAt": 1794571539833,
        "statusUpdatedAt": 1794571539833,
    }
    entry.update(overrides)
    return entry


def write_entry(config_dir: Path, pid: int, body: dict[str, object] | str) -> Path:
    path = config_dir / "sessions" / f"{pid}.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


class TestDiscoverOwnSession:
    def test_the_socket_env_and_parent_pid_name_the_session(self) -> None:
        env = {
            "CLAUDE_CODE_MESSAGING_SOCKET": SOCK,
            "CLAUDE_CODE_SESSION_ID": "s-1",
            "HOME": "/home/x",
        }
        assert discover_own_session(env, ppid=4242) == OwnSession(
            4242, Path(SOCK), Path("/home/x/.claude"), "s-1"
        )

    def test_claude_pid_is_ignored_in_favour_of_the_parent_pid(self) -> None:
        # CLAUDE_PID leaks in from an outer session when Claude runs nested.
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": SOCK, "CLAUDE_PID": "1", "HOME": "/home/x"}
        assert discover_own_session(env, ppid=4242).pid == 4242

    def test_claude_config_dir_wins_over_home(self) -> None:
        env = {
            "CLAUDE_CODE_MESSAGING_SOCKET": SOCK,
            "CLAUDE_CONFIG_DIR": "/home/x/.claude-work",
            "HOME": "/home/x",
        }
        assert discover_own_session(env, ppid=4242).config_dir == Path("/home/x/.claude-work")

    def test_a_missing_session_id_is_none(self) -> None:
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": SOCK, "HOME": "/home/x"}
        assert discover_own_session(env, ppid=4242).session_id is None

    def test_no_messaging_socket_is_not_a_claude_session(self) -> None:
        with pytest.raises(NotInClaudeSession) as caught:
            discover_own_session({"HOME": "/home/x", "CLAUDE_CODE_SESSION_ID": "s-1"}, ppid=4242)
        assert caught.value.code == "not_in_claude_session"
        assert isinstance(caught.value, RelayError)

    def test_an_empty_messaging_socket_is_not_a_claude_session(self) -> None:
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": "", "HOME": "/home/x"}
        with pytest.raises(NotInClaudeSession):
            discover_own_session(env, ppid=4242)


class TestClaudeConfigDir:
    def test_home_dot_claude_without_claude_config_dir(self) -> None:
        # Claude's own HOME, which may differ from the passwd home (tale-home install).
        assert claude_config_dir({"HOME": "/home/x/claude-tale-home"}) == Path(
            "/home/x/claude-tale-home/.claude"
        )

    def test_claude_config_dir_when_set(self) -> None:
        env = {"CLAUDE_CONFIG_DIR": "/cfg/b", "HOME": "/home/x"}
        assert claude_config_dir(env) == Path("/cfg/b")

    @pytest.mark.parametrize("raw", ["cfg", "./cfg", "relative/cfg"])
    def test_a_relative_claude_config_dir_is_a_path_error(self, raw: str) -> None:
        with pytest.raises(PathError) as caught:
            claude_config_dir({"CLAUDE_CONFIG_DIR": raw, "HOME": "/home/x"})
        assert caught.value.code == "path_invalid"
        assert raw in caught.value.message


class TestRegistryRead:
    def test_a_valid_entry_is_read_and_extra_keys_are_ignored(self, config_dir: Path) -> None:
        write_entry(config_dir, 4242, registry_json(4242, "s-1", status="busy"))
        assert ClaudeRegistry(config_dir).read(4242) == RegistryEntry(
            pid=4242,
            session_id="s-1",
            cwd=Path("/w/project"),
            messaging_socket=Path("/run/user/1000/cc-socks/4242.sock"),
            version=PINNED_VERSION,
            peer_protocol=1,
            status="busy",
        )

    def test_a_missing_file_is_none(self, config_dir: Path) -> None:
        assert ClaudeRegistry(config_dir).read(4242) is None

    def test_a_missing_sessions_dir_is_none(self, tmp_path: Path) -> None:
        assert ClaudeRegistry(tmp_path / "nowhere").read(4242) is None

    def test_truncated_json_is_none_with_a_warning(
        self, config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mz_relay")
        write_entry(config_dir, 4242, "{")
        assert ClaudeRegistry(config_dir).read(4242) is None
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_a_missing_key_is_none_with_a_warning(
        self, config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mz_relay")
        entry = registry_json(4242, "s-1")
        del entry["messagingSocketPath"]
        write_entry(config_dir, 4242, entry)
        assert ClaudeRegistry(config_dir).read(4242) is None
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_a_wrongly_typed_value_is_none(self, config_dir: Path) -> None:
        write_entry(config_dir, 4242, registry_json(4242, "s-1", peerProtocol="one"))
        assert ClaudeRegistry(config_dir).read(4242) is None

    def test_a_json_array_is_none(self, config_dir: Path) -> None:
        write_entry(config_dir, 4242, "[1, 2]")
        assert ClaudeRegistry(config_dir).read(4242) is None


class TestRegistryReadSettled:
    """A registry rewrite can read as missing or torn once; while the pid lives, read again."""

    def put_back(self, path: Path, body: dict[str, object]) -> Callable[[], object]:
        return lambda: path.write_text(json.dumps(body), encoding="utf-8")

    def test_an_entry_back_after_a_short_delay_is_read(self, config_dir: Path) -> None:
        pid = os.getpid()
        body = registry_json(pid, "s-1")
        clock = SleepHookClock(
            FakeClock(), self.put_back(config_dir / "sessions" / f"{pid}.json", body)
        )

        entry = ClaudeRegistry(config_dir).read_settled(pid, clock=clock)

        assert entry is not None
        assert entry.session_id == "s-1"
        assert len(clock.slept) == 1
        assert clock.slept[0] <= 1.0

    def test_a_torn_entry_fixed_after_a_short_delay_is_read(self, config_dir: Path) -> None:
        pid = os.getpid()
        path = write_entry(config_dir, pid, "{")
        clock = SleepHookClock(FakeClock(), self.put_back(path, registry_json(pid, "s-1")))

        entry = ClaudeRegistry(config_dir).read_settled(pid, clock=clock)

        assert entry is not None
        assert entry.pid == pid

    def test_an_entry_still_missing_after_one_retry_is_none(self, config_dir: Path) -> None:
        clock = SleepHookClock(FakeClock())

        assert ClaudeRegistry(config_dir).read_settled(os.getpid(), clock=clock) is None
        assert len(clock.slept) == 1

    def test_a_dead_pid_is_not_waited_for(self, config_dir: Path) -> None:
        child = subprocess.Popen(["true"])
        child.wait()
        clock = SleepHookClock(FakeClock())

        assert ClaudeRegistry(config_dir).read_settled(child.pid, clock=clock) is None
        assert clock.slept == []

    def test_a_present_entry_is_read_without_waiting(self, config_dir: Path) -> None:
        pid = os.getpid()
        write_entry(config_dir, pid, registry_json(pid, "s-1"))
        clock = SleepHookClock(FakeClock())

        entry = ClaudeRegistry(config_dir).read_settled(pid, clock=clock)

        assert entry is not None
        assert clock.slept == []


class TestEntryStatus:
    @pytest.mark.parametrize(
        ("status", "idle"),
        [("idle", True), ("busy", False), ("waiting", False), ("", False), ("IDLE", False)],
    )
    def test_only_idle_is_idle_and_anything_else_counts_as_busy(
        self, config_dir: Path, status: str, idle: bool
    ) -> None:
        write_entry(config_dir, 4242, registry_json(4242, "s-1", status=status))
        entry = ClaudeRegistry(config_dir).read(4242)
        assert entry is not None
        assert (entry.is_idle, entry.is_busy) == (idle, not idle)


class TestFindSession:
    def test_finds_the_entry_carrying_the_session_id(self, config_dir: Path) -> None:
        write_entry(config_dir, 4242, registry_json(4242, "s-1"))
        write_entry(config_dir, 5353, registry_json(5353, "s-9"))
        found = ClaudeRegistry(config_dir).find_session("s-9")
        assert found is not None
        assert found.pid == 5353

    def test_after_a_rotate_only_the_new_id_is_found(self, config_dir: Path) -> None:
        registry = ClaudeRegistry(config_dir)
        write_entry(config_dir, 4242, registry_json(4242, "s-1"))
        assert registry.find_session("s-1") is not None
        write_entry(config_dir, 4242, registry_json(4242, "s-2"))
        assert registry.find_session("s-1") is None
        found = registry.find_session("s-2")
        assert found is not None
        assert found.pid == 4242

    @pytest.mark.parametrize("malformed", ["{", "[" * 100_000], ids=["truncated", "deeply-nested"])
    def test_key_files_and_malformed_entries_are_skipped(
        self, config_dir: Path, malformed: str
    ) -> None:
        (config_dir / "sessions" / "4242.abcdef.key").write_text("secret", encoding="utf-8")
        write_entry(config_dir, 1111, malformed)
        write_entry(config_dir, 4242, registry_json(4242, "s-1"))
        found = ClaudeRegistry(config_dir).find_session("s-1")
        assert found is not None
        assert found.pid == 4242

    def test_an_unknown_session_is_none(self, config_dir: Path) -> None:
        write_entry(config_dir, 4242, registry_json(4242, "s-1"))
        assert ClaudeRegistry(config_dir).find_session("s-404") is None


class TestVersionSupported:
    @pytest.mark.parametrize("version", ["2.1.322", "2.1.323", "2.2.0", "3.0.0"])
    def test_at_or_above_the_pin_with_protocol_1_is_supported(self, version: str) -> None:
        assert version_supported(version, 1) is True

    @pytest.mark.parametrize("version", ["2.1.321", "2.0.999", "1.9.400"])
    def test_below_the_pin_is_unsupported(self, version: str) -> None:
        assert version_supported(version, 1) is False

    def test_another_peer_protocol_is_unsupported(self) -> None:
        assert version_supported(PINNED_VERSION, 2) is False

    @pytest.mark.parametrize("version", ["garbage", "", "2.1", "2.x.3"])
    def test_an_unparsable_version_is_unsupported(self, version: str) -> None:
        assert version_supported(version, 1) is False


class TestPidAlive:
    def test_this_process_is_alive(self) -> None:
        assert pid_alive(os.getpid()) is True

    def test_a_reaped_child_is_dead(self) -> None:
        child = subprocess.Popen(["/bin/true"])
        child.wait()
        assert pid_alive(child.pid) is False

    @pytest.mark.skipif(os.getuid() == 0, reason="root may signal every process")
    def test_a_process_of_another_user_counts_as_alive(self) -> None:
        # kill(1, 0) raises PermissionError for a normal user: the pid exists.
        assert pid_alive(1) is True

    @pytest.mark.parametrize("pid", [0, -1])
    def test_a_non_positive_pid_is_never_alive(self, pid: int) -> None:
        # kill(0, 0) signals the whole process group and would always succeed.
        assert pid_alive(pid) is False

    @pytest.mark.parametrize("pid", [2**31, 2**64])
    def test_a_pid_beyond_any_os_pid_is_never_alive(self, pid: int) -> None:
        # os.kill raises OverflowError for values a pid_t cannot hold.
        assert pid_alive(pid) is False


class TestBuildFrame:
    def test_the_frame_is_one_compact_json_line_with_str_content(self) -> None:
        assert (
            build_frame("hi\nthere")
            == b'{"type":"user","message":{"role":"user","content":"hi\\nthere"}}\n'
        )

    def test_the_frame_has_no_auth_line(self) -> None:
        frame = build_frame("x")
        assert frame.count(b"\n") == 1
        assert b"auth" not in frame
        assert b"token" not in frame

    def test_the_content_round_trips(self) -> None:
        text = 'quote " backslash \\ unicode — é'
        assert json.loads(build_frame(text))["message"]["content"] == text
