"""Fixtures for live tests that drive a real harness; each skips with a reason when unavailable."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


def require_env(name: str) -> str:
    """The variable's value; skips the test when it is unset or empty."""
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is not set")
    return value


@pytest.fixture
def live_claude_dirs() -> tuple[Path, Path]:
    """Two Claude config dirs (two accounts) from MZ_RELAY_LIVE_CLAUDE_DIRS="<cfgA>:<cfgB>"."""
    raw = require_env("MZ_RELAY_LIVE_CLAUDE_DIRS")
    parts = raw.split(":")
    if len(parts) != 2 or not all(parts):
        pytest.fail(f"MZ_RELAY_LIVE_CLAUDE_DIRS must be <cfgA>:<cfgB>, got {raw!r}")
    dirs = (Path(parts[0]).expanduser(), Path(parts[1]).expanduser())
    for directory in dirs:
        if not directory.is_dir():
            pytest.fail(f"Claude config dir {directory} does not exist")
    if dirs[0].resolve() == dirs[1].resolve():
        pytest.fail("MZ_RELAY_LIVE_CLAUDE_DIRS must name two different config dirs")
    return dirs


def command_from(name: str) -> list[str]:
    """The command line in the variable, split like a shell would; skips when it is unset."""
    argv = shlex.split(require_env(name))
    if not argv:
        pytest.fail(f"{name} holds no command")
    return argv


@pytest.fixture
def live_pi_argv() -> list[str]:
    """The Pi command from MZ_RELAY_LIVE_PI, e.g. `pi -ne --no-session` plus model flags."""
    return command_from("MZ_RELAY_LIVE_PI")


@pytest.fixture
def live_opencode_argv() -> list[str]:
    """The OpenCode command from MZ_RELAY_LIVE_OPENCODE, e.g. `opencode -m <provider/model>`."""
    return command_from("MZ_RELAY_LIVE_OPENCODE")


@pytest.fixture
def tmux_bin() -> str:
    """tmux, which the live Pi and OpenCode tests drive their TUIs through."""
    found = shutil.which("tmux")
    if found is None:
        pytest.skip("tmux is not on PATH")
    return found


@pytest.fixture
def claude_bin() -> str:
    """The claude CLI, when it is on PATH and supports --plugin-dir."""
    found = shutil.which("claude")
    if found is None:
        pytest.skip("claude is not on PATH")
    usage = subprocess.run(
        [found, "--help"], capture_output=True, text=True, timeout=30, check=False
    ).stdout
    if "--plugin-dir" not in usage:
        pytest.skip("this claude has no --plugin-dir option")
    return found
