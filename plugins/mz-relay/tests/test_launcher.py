"""bin/mz-relay: the interpreter it picks, the version it prints and the refusals it gives."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import BIN

BASH = "/bin/bash"
RUNNER = BIN.parent / "_mz_relay_run.py"
# Exits 1 on every call, as a Python 3.11 answers the shim's version probe.
OLD_PYTHON = "#!/bin/sh\nexit 1\n"


def write_script(folder: Path, name: str, text: str) -> Path:
    script = folder / name
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    return script


def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, encoding="utf-8", env=env, timeout=30, check=False
    )


@pytest.fixture
def version_line() -> str:
    from mz_relay import __version__

    return f"mz-relay {__version__}\n"


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    folder = tmp_path / "fakebin"
    folder.mkdir()
    return folder


class TestShim:
    def test_version_runs_on_the_python_named_by_mz_relay_python(
        self, relay_env: dict[str, str], version_line: str
    ) -> None:
        result = run([str(BIN), "version"], relay_env)
        assert (result.returncode, result.stdout) == (0, version_line), result.stderr

    def test_skips_empty_override_and_pythons_older_than_3_12(
        self, relay_env: dict[str, str], fake_bin: Path, version_line: str
    ) -> None:
        write_script(fake_bin, "python3.13", OLD_PYTHON)
        write_script(fake_bin, "python3", OLD_PYTHON)
        write_script(fake_bin, "python3.12", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        env = {**relay_env, "MZ_RELAY_PYTHON": "", "PATH": str(fake_bin)}
        result = run([BASH, str(BIN), "version"], env)
        assert (result.returncode, result.stdout) == (0, version_line), result.stderr

    def test_no_python_3_12_exits_2_with_a_hint(
        self, relay_env: dict[str, str], fake_bin: Path
    ) -> None:
        old = write_script(fake_bin, "python3.11", OLD_PYTHON)
        env = {**relay_env, "MZ_RELAY_PYTHON": str(old), "PATH": str(fake_bin)}
        result = run([BASH, str(BIN), "version"], env)
        assert result.returncode == 2
        assert "needs Python 3.12" in result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize("args", [["nonsense"], []], ids=["unknown", "missing"])
    def test_bad_subcommand_is_a_usage_error(
        self, relay_env: dict[str, str], args: list[str]
    ) -> None:
        result = run([str(BIN), *args], relay_env)
        assert result.returncode == 2
        assert result.stdout == ""

    def test_shim_is_executable_by_everyone_and_writable_by_owner(self) -> None:
        # Execute bits only: a checkout under umask 077 drops the group and other read bits.
        mode = stat.S_IMODE(BIN.stat().st_mode)
        assert mode & 0o111 == 0o111
        assert mode & 0o200 == 0o200


class TestRunner:
    def test_runner_starts_the_cli_without_the_shim(
        self, relay_env: dict[str, str], version_line: str
    ) -> None:
        # The daemon is spawned as `<python> -I -B _mz_relay_run.py daemon`, bypassing the shim.
        result = run([sys.executable, "-I", "-B", str(RUNNER), "version"], relay_env)
        assert (result.returncode, result.stdout) == (0, version_line), result.stderr
