"""Where mz-relay keeps its state, sockets and config."""

from __future__ import annotations

import logging
import os
import pwd
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mz_relay.errors import PathError

log = logging.getLogger(__name__)

APP = "mz-relay"
# sun_path is 108 bytes including the trailing NUL.
SOCKET_PATH_LIMIT = 108
PRIVATE_MODE = 0o700
ADAPTER_KINDS = ("pi", "oc")


def user_home(uid: int | None = None) -> Path:
    """The passwd home of uid (default: this process), never $HOME."""
    if uid is None:
        uid = os.getuid()
    if uid < 0:
        raise PathError(f"uid must be non-negative, got {uid}")
    try:
        entry = pwd.getpwuid(uid)
    except KeyError as exc:
        raise PathError(f"no passwd entry for uid {uid}") from exc
    return Path(entry.pw_dir)


def check_socket_path(path: Path) -> Path:
    size = len(os.fsencode(path))
    if size >= SOCKET_PATH_LIMIT:
        raise PathError(
            f"socket path {path} is {size} bytes; Unix sockets need fewer than {SOCKET_PATH_LIMIT}"
        )
    return path


@dataclass(frozen=True)
class Paths:
    state_dir: Path
    runtime_dir: Path
    config_dir: Path
    pw_home: Path

    @property
    def db(self) -> Path:
        return self.state_dir / "relay.db"

    @property
    def log(self) -> Path:
        return self.state_dir / "relayd.log"

    @property
    def hooks_log(self) -> Path:
        return self.state_dir / "hooks.log"

    @property
    def roster(self) -> Path:
        return self.config_dir / "roster.toml"

    @property
    def install_record(self) -> Path:
        return self.config_dir / "install.json"

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "relayd.sock"

    @property
    def lock(self) -> Path:
        return self.runtime_dir / "relayd.lock"

    @property
    def spawn_lock(self) -> Path:
        return self.runtime_dir / "spawn.lock"

    @property
    def waiter_dir(self) -> Path:
        return self.runtime_dir / "waiters"

    @property
    def marker_dir(self) -> Path:
        return self.runtime_dir / "peer-turn"

    @property
    def turn_end_dir(self) -> Path:
        return self.runtime_dir / "turn-end"

    def adapter_socket(self, kind: Literal["pi", "oc"], pid: int) -> Path:
        if kind not in ADAPTER_KINDS:
            raise PathError(f"adapter kind must be one of {', '.join(ADAPTER_KINDS)}, got {kind!r}")
        if pid <= 0:
            raise PathError(f"adapter pid must be positive, got {pid}")
        return check_socket_path(self.runtime_dir / f"{kind}-{pid}.sock")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        uid: int,
        pw_home: Path,
        run_user_exists: Callable[[Path], bool] = Path.is_dir,
    ) -> Paths:
        if uid < 0:
            raise PathError(f"uid must be non-negative, got {uid}")
        if not pw_home.is_absolute():
            raise PathError(f"passwd home must be absolute, got {pw_home}")
        relay_home = env.get("MZ_RELAY_HOME")
        if relay_home:
            home = Path(relay_home)
            if not home.is_absolute():
                raise PathError(f"MZ_RELAY_HOME must be absolute, got {relay_home!r}")
            paths = cls(home / "state", home / "run", home, pw_home)
        else:
            paths = cls(
                state_dir=_xdg_dir(env, "XDG_STATE_HOME", pw_home / ".local" / "state"),
                runtime_dir=_runtime_dir(env, uid, run_user_exists),
                config_dir=_xdg_dir(env, "XDG_CONFIG_HOME", pw_home / ".config"),
                pw_home=pw_home,
            )
        check_socket_path(paths.socket)
        return paths

    def ensure_dirs(self) -> None:
        """Creates the state and runtime dirs owner-only, tightening looser existing ones."""
        for directory in (self.state_dir, self.runtime_dir):
            try:
                directory.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
                info = directory.stat()
                if info.st_uid != os.getuid():
                    raise PathError(f"{directory} is owned by uid {info.st_uid}, not {os.getuid()}")
                mode = stat.S_IMODE(info.st_mode)
                if mode != PRIVATE_MODE:
                    log.info("tightening %s from %o to %o", directory, mode, PRIVATE_MODE)
                    directory.chmod(PRIVATE_MODE)
            except OSError as exc:
                raise PathError(f"cannot prepare {directory}: {exc.strerror or exc}") from exc

    def expand(self, raw: str) -> Path:
        return expand_home(raw, self.pw_home)


def expand_home(raw: str, pw_home: Path) -> Path:
    """Expands a leading ~ against the passwd home; other paths are returned as given."""
    if not raw:
        raise PathError("path must be a non-empty string")
    if raw == "~":
        return pw_home
    if raw.startswith("~/"):
        return pw_home / raw[2:]
    if raw.startswith("~"):
        raise PathError(f"only ~ and ~/ are expanded, got {raw!r}")
    return Path(raw)


def _xdg_dir(env: Mapping[str, str], variable: str, fallback: Path) -> Path:
    # The XDG spec says relative values are invalid and must be ignored.
    value = env.get(variable)
    base = Path(value) if value and Path(value).is_absolute() else fallback
    return base / APP


def _runtime_dir(env: Mapping[str, str], uid: int, run_user_exists: Callable[[Path], bool]) -> Path:
    value = env.get("XDG_RUNTIME_DIR")
    if value and Path(value).is_absolute():
        return Path(value) / APP
    # Harnesses may strip XDG_RUNTIME_DIR from children; /run/user keeps them on one socket.
    run_user = Path(f"/run/user/{uid}")
    if run_user_exists(run_user):
        return run_user / APP
    log.debug("no %s; runtime dir falls back to /tmp", run_user)
    return Path(f"/tmp/{APP}-{uid}")
