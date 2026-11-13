"""Where mz-relay keeps its state, sockets and config, and the errors it raises about them."""

from __future__ import annotations

import os
import pwd
import re
import stat
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from mz_relay.errors import (
    AuthError,
    BindingError,
    DaemonUnavailable,
    EnvelopeError,
    PathError,
    RelayError,
    StoreError,
)
from mz_relay.paths import Paths, user_home

UID = 1000
PW_HOME = Path("/home/u")


def paths_for(
    env: Mapping[str, str],
    *,
    run_user_exists: Callable[[Path], bool] = lambda _: False,
    pw_home: Path = PW_HOME,
) -> Paths:
    return Paths.from_env(env, uid=UID, pw_home=pw_home, run_user_exists=run_user_exists)


@pytest.fixture
def open_umask() -> Iterator[None]:
    # A permissive umask, so a private directory proves the code chose the mode.
    previous = os.umask(0o022)
    yield
    os.umask(previous)


class TestFromEnv:
    def test_relay_home_holds_every_file(self) -> None:
        paths = paths_for({"MZ_RELAY_HOME": "/tmp/mzr-x"})
        home = Path("/tmp/mzr-x")
        assert (paths.state_dir, paths.runtime_dir, paths.config_dir) == (
            home / "state",
            home / "run",
            home,
        )
        assert {
            "db": paths.db,
            "log": paths.log,
            "hooks_log": paths.hooks_log,
            "roster": paths.roster,
            "install_record": paths.install_record,
            "socket": paths.socket,
            "lock": paths.lock,
            "spawn_lock": paths.spawn_lock,
            "waiter_dir": paths.waiter_dir,
            "marker_dir": paths.marker_dir,
            "turn_end_dir": paths.turn_end_dir,
        } == {
            "db": home / "state" / "relay.db",
            "log": home / "state" / "relayd.log",
            "hooks_log": home / "state" / "hooks.log",
            "roster": home / "roster.toml",
            "install_record": home / "install.json",
            "socket": home / "run" / "relayd.sock",
            "lock": home / "run" / "relayd.lock",
            "spawn_lock": home / "run" / "spawn.lock",
            "waiter_dir": home / "run" / "waiters",
            "marker_dir": home / "run" / "peer-turn",
            "turn_end_dir": home / "run" / "turn-end",
        }

    def test_adapter_sockets_are_per_kind_and_pid(self) -> None:
        paths = paths_for({"MZ_RELAY_HOME": "/tmp/mzr-x"})
        assert paths.adapter_socket("pi", 123) == Path("/tmp/mzr-x/run/pi-123.sock")
        assert paths.adapter_socket("oc", 7) == Path("/tmp/mzr-x/run/oc-7.sock")

    def test_relay_home_wins_over_xdg_variables(self) -> None:
        env = {"MZ_RELAY_HOME": "/tmp/mzr-x", "XDG_STATE_HOME": "/s", "XDG_RUNTIME_DIR": "/r"}
        paths = paths_for(env)
        assert (paths.state_dir, paths.runtime_dir) == (
            Path("/tmp/mzr-x/state"),
            Path("/tmp/mzr-x/run"),
        )

    def test_xdg_variables_place_state_runtime_and_config(self) -> None:
        env = {"XDG_STATE_HOME": "/s", "XDG_RUNTIME_DIR": "/r", "XDG_CONFIG_HOME": "/c"}
        paths = paths_for(env, run_user_exists=lambda _: True)
        assert (paths.state_dir, paths.runtime_dir, paths.config_dir) == (
            Path("/s/mz-relay"),
            Path("/r/mz-relay"),
            Path("/c/mz-relay"),
        )
        assert paths.roster == Path("/c/mz-relay/roster.toml")
        assert paths.db == Path("/s/mz-relay/relay.db")

    def test_fallbacks_use_the_passwd_home_not_home_variable(self) -> None:
        paths = paths_for({"HOME": "/home/other"})
        assert paths.state_dir == Path("/home/u/.local/state/mz-relay")
        assert paths.config_dir == Path("/home/u/.config/mz-relay")
        assert paths.pw_home == PW_HOME

    def test_runtime_falls_back_to_run_user_when_it_exists(self) -> None:
        probed: list[Path] = []

        def exists(path: Path) -> bool:
            probed.append(path)
            return True

        paths = paths_for({"HOME": "/home/other"}, run_user_exists=exists)
        assert paths.runtime_dir == Path("/run/user/1000/mz-relay")
        assert Path("/run/user/1000") in probed

    def test_runtime_falls_back_to_tmp_without_run_user(self) -> None:
        paths = paths_for({"HOME": "/home/other"}, run_user_exists=lambda _: False)
        assert paths.runtime_dir == Path("/tmp/mz-relay-1000")
        assert paths.socket == Path("/tmp/mz-relay-1000/relayd.sock")


class TestErrors:
    def test_socket_path_of_108_bytes_or_more_is_refused(self) -> None:
        with pytest.raises(PathError) as caught:
            paths_for({"MZ_RELAY_HOME": "/tmp/" + "a" * 120})
        assert caught.value.code == "path_invalid"
        assert "108" in caught.value.message
        assert isinstance(caught.value, RelayError)

    def test_socket_path_limit_counts_bytes_at_the_boundary(self) -> None:
        # "<home>/run/relayd.sock" adds 16 bytes: a 91-byte home gives 107, a 92-byte home 108.
        longest = paths_for({"MZ_RELAY_HOME": "/tmp/" + "a" * 86})
        assert len(bytes(longest.socket)) == 107
        with pytest.raises(PathError):
            paths_for({"MZ_RELAY_HOME": "/tmp/" + "a" * 87})

    def test_multibyte_characters_count_as_bytes(self) -> None:
        home = "/tmp/" + "é" * 44  # 65 characters in the socket path, but 109 bytes
        with pytest.raises(PathError):
            paths_for({"MZ_RELAY_HOME": home})

    def test_relative_relay_home_is_refused(self) -> None:
        with pytest.raises(PathError, match="must be absolute") as caught:
            paths_for({"MZ_RELAY_HOME": "rel/dir"})
        assert caught.value.code == "path_invalid"

    def test_relay_error_to_wire_carries_data(self) -> None:
        error = RelayError("x", "y", {"a": 1})
        assert error.to_wire() == {"code": "x", "message": "y", "data": {"a": 1}}

    @pytest.mark.parametrize(
        ("error_type", "code"),
        [
            (AuthError, "bad_token"),
            (BindingError, "binding_invalid"),
            (DaemonUnavailable, "daemon_unavailable"),
            (EnvelopeError, "invalid_args"),
            (PathError, "path_invalid"),
            (StoreError, "store_invalid"),
        ],
    )
    def test_single_code_subclasses_take_only_a_message(
        self, error_type: Callable[[str], RelayError], code: str
    ) -> None:
        # Callers build these as Sub(message) and read str(err) as the detail.
        error = error_type("socket gone")
        assert (error.code, error.message, str(error)) == (code, "socket gone", "socket gone")
        assert isinstance(error, RelayError)

    def test_relay_error_to_wire_omits_missing_data(self) -> None:
        error = RelayError("x", "y")
        assert (error.code, error.message, error.data) == ("x", "y", {})
        assert error.to_wire() == {"code": "x", "message": "y"}


class TestEnsureDirs:
    def test_state_and_runtime_dirs_are_created_private(
        self, relay_home: Path, open_umask: None
    ) -> None:
        home = relay_home / "h"
        paths = paths_for({"MZ_RELAY_HOME": str(home)}, pw_home=relay_home)
        paths.ensure_dirs()
        assert stat.S_IMODE(paths.state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(paths.runtime_dir.stat().st_mode) == 0o700

    @pytest.mark.parametrize("which", ["runtime_dir", "state_dir"])
    def test_looser_existing_dir_is_tightened(
        self, relay_home: Path, open_umask: None, which: str
    ) -> None:
        paths = paths_for({"MZ_RELAY_HOME": str(relay_home)}, pw_home=relay_home)
        loose: Path = getattr(paths, which)
        loose.mkdir()
        loose.chmod(0o755)
        paths.ensure_dirs()
        paths.ensure_dirs()
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700

    @pytest.mark.parametrize("blocker", ["home", "state"], ids=["parent-is-file", "dir-is-file"])
    def test_a_dir_that_cannot_be_created_is_a_path_error(
        self, relay_home: Path, blocker: str
    ) -> None:
        home = relay_home / "h"
        paths = paths_for({"MZ_RELAY_HOME": str(home)}, pw_home=relay_home)
        if blocker == "home":
            home.write_text("not a directory", encoding="utf-8")
        else:
            home.mkdir()
            paths.state_dir.write_text("not a directory", encoding="utf-8")

        with pytest.raises(PathError, match=re.escape(str(paths.state_dir))):
            paths.ensure_dirs()


class TestHome:
    def test_expand_resolves_tilde_against_the_passwd_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", "/home/other")
        paths = paths_for({"HOME": "/home/other"})
        assert paths.expand("~/x") == PW_HOME / "x"
        assert paths.expand("~") == PW_HOME
        assert paths.expand("/abs/y") == Path("/abs/y")

    def test_user_home_reads_passwd_and_ignores_home_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", "/nonexistent-home")
        assert user_home() == Path(pwd.getpwuid(os.getuid()).pw_dir)
        assert user_home(0) == Path(pwd.getpwuid(0).pw_dir)
