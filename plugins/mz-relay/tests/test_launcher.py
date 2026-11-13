"""bin/mz-relay: the interpreter it picks, the version it prints and the refusals it gives."""

from __future__ import annotations

import shutil
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
# The three commands hooks.json runs; exit 2 from any of them blocks a tool or wakes Claude.
HOOK_COMMANDS = [["hook", "gate"], ["hook", "wait", "Stop"], ["hook", "wait", "SessionStart"]]
HOOK_IDS = ["gate", "wait-stop", "wait-session-start"]
HOOK_STDIN = '{"session_id": "s-1", "cwd": "/tmp/x", "hook_event_name": "PreToolUse"}'
# Runs the runner on this interpreter while it claims to be 3.11.
OLD_RUNNER = (
    "import runpy, sys; sys.argv = sys.argv[1:]; sys.version_info = (3, 11, 9, 'final', 0); "
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)


def write_script(folder: Path, name: str, text: str) -> Path:
    script = folder / name
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    return script


def run(
    argv: list[str], env: dict[str, str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, input=stdin, capture_output=True, encoding="utf-8", env=env, timeout=30, check=False
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


@pytest.fixture
def broken_plugin(tmp_path: Path) -> Path:
    """A copy of bin/ beside an empty src/, so the package import fails; returns the shim."""
    root = tmp_path / "plugin"
    shutil.copytree(BIN.parent, root / "bin")
    (root / "src").mkdir()
    return root / "bin" / BIN.name


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


class TestHooksNeverExit2:
    """Claude reads exit 2 as "block this tool" or "wake me with stderr", so a broken install
    must exit 0 from every hook command; non-hook commands keep exit 2."""

    @pytest.mark.parametrize("argv", HOOK_COMMANDS, ids=HOOK_IDS)
    @pytest.mark.parametrize("override", ["old", "missing", "unset"])
    def test_a_hook_without_a_python_3_12_exits_0_with_nothing_on_stdout(
        self, relay_env: dict[str, str], fake_bin: Path, argv: list[str], override: str
    ) -> None:
        old = write_script(fake_bin, "python3.11", OLD_PYTHON)
        write_script(fake_bin, "python3", OLD_PYTHON)
        python = {"old": str(old), "missing": str(fake_bin / "nope"), "unset": ""}[override]
        env = {**relay_env, "MZ_RELAY_PYTHON": python, "PATH": str(fake_bin)}

        result = run([BASH, str(BIN), *argv], env, HOOK_STDIN)

        assert (result.returncode, result.stdout) == (0, ""), result.stderr

    @pytest.mark.parametrize("argv", HOOK_COMMANDS, ids=HOOK_IDS)
    def test_a_hook_whose_package_import_fails_exits_0_with_nothing_on_stdout(
        self, relay_env: dict[str, str], broken_plugin: Path, argv: list[str]
    ) -> None:
        result = run([BASH, str(broken_plugin), *argv], relay_env, HOOK_STDIN)

        assert (result.returncode, result.stdout) == (0, ""), result.stderr

    @pytest.mark.parametrize("argv", HOOK_COMMANDS, ids=HOOK_IDS)
    def test_the_runner_on_an_old_python_exits_0_for_a_hook(
        self, relay_env: dict[str, str], argv: list[str]
    ) -> None:
        command = [sys.executable, "-I", "-B", "-c", OLD_RUNNER, str(RUNNER), *argv]

        result = run(command, relay_env, HOOK_STDIN)

        assert (result.returncode, result.stdout) == (0, ""), result.stderr

    @pytest.mark.parametrize("argv", HOOK_COMMANDS, ids=HOOK_IDS)
    def test_a_hook_whose_runner_is_gone_exits_0_with_nothing_on_stdout(
        self, relay_env: dict[str, str], broken_plugin: Path, argv: list[str]
    ) -> None:
        # A plugin update deletes the old version's folder while hooks may still start from it.
        (broken_plugin.parent / RUNNER.name).unlink()

        result = run([BASH, str(broken_plugin), *argv], relay_env, HOOK_STDIN)

        assert (result.returncode, result.stdout) == (0, ""), result.stderr
        assert "mz-relay" in result.stderr

    def test_a_non_hook_command_whose_runner_is_gone_still_exits_2(
        self, relay_env: dict[str, str], broken_plugin: Path
    ) -> None:
        (broken_plugin.parent / RUNNER.name).unlink()

        result = run([BASH, str(broken_plugin), "version"], relay_env)

        assert result.returncode == 2

    def test_a_non_hook_command_whose_package_import_fails_still_exits_2(
        self, relay_env: dict[str, str], broken_plugin: Path
    ) -> None:
        result = run([BASH, str(broken_plugin), "version"], relay_env)

        assert result.returncode == 2
        assert result.stdout == ""

    def test_the_runner_on_an_old_python_still_exits_2_for_a_non_hook_command(
        self, relay_env: dict[str, str]
    ) -> None:
        command = [sys.executable, "-I", "-B", "-c", OLD_RUNNER, str(RUNNER), "version"]

        result = run(command, relay_env)

        assert result.returncode == 2
        assert "needs Python 3.12" in result.stderr


class TestSymlinkedShim:
    def test_a_symlink_on_path_runs_the_real_shim(
        self, relay_env: dict[str, str], tmp_path: Path, version_line: str
    ) -> None:
        # The README suggests putting the launcher on PATH; a symlink is the usual way.
        links = tmp_path / "links"
        links.mkdir()
        (links / "mz-relay").symlink_to(BIN)
        env = {**relay_env, "PATH": f"{links}:{relay_env.get('PATH', '/usr/bin:/bin')}"}

        result = run(["mz-relay", "version"], env)

        assert (result.returncode, result.stdout) == (0, version_line), result.stderr


class TestRunner:
    def test_runner_starts_the_cli_without_the_shim(
        self, relay_env: dict[str, str], version_line: str
    ) -> None:
        # The daemon is spawned as `<python> -I -B _mz_relay_run.py daemon`, bypassing the shim.
        result = run([sys.executable, "-I", "-B", str(RUNNER), "version"], relay_env)
        assert (result.returncode, result.stdout) == (0, version_line), result.stderr
