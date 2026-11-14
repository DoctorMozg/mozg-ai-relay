"""Installing the Pi and OpenCode adapters, the OpenCode and Codex snippets, and adapter wiring."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Protocol

import pytest
from codex_rig import FAST_PATH, codex_job, codex_spec, fake_calls, index_line, write_index
from conftest import BIN, PLUGIN
from mz_relay import __version__, install
from mz_relay.adapters import AdapterContext, build_adapters
from mz_relay.adapters.claude import ClaudeAdapter
from mz_relay.adapters.codex import CodexAdapter
from mz_relay.adapters.socket_adapter import SocketAdapter
from mz_relay.cli import main
from mz_relay.clock import SystemClock
from mz_relay.delivery import Outcome
from mz_relay.install import (
    ADAPTER_MARKER,
    Installer,
    InstallError,
    InstallPlan,
    codex_snippet,
    opencode_snippet,
)
from mz_relay.paths import Paths
from mz_relay.roster import Harness

PI_SOURCE = "// mz-relay adapter for Pi\nexport default function mzRelay(pi) {}\n"
OC_SOURCE = "// mz-relay adapter for OpenCode\nexport const MzRelayPlugin = async () => ({})\n"
SOURCES = {"pi": PI_SOURCE, "opencode": OC_SOURCE}
FOREIGN = "export default function someoneElses() {}\n// mz-relay adapter for Pi\n"
PEER_AGENT = {
    "description": "Answers mz-relay peer messages, read-only",
    "mode": "primary",
    "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny", "task": "deny"},
}
KINDS = pytest.mark.parametrize("kind", ["pi", "opencode"])
CODEX_COMMENT = (
    '# Paste into <CODEX_HOME>/config.toml of the account that runs "reviewer".'
    " After /rename, send the thread one message."
)

DEFAULT_DEST = {
    "pi": Path(".pi") / "agent" / "extensions" / "mz-relay" / "index.ts",
    "opencode": Path(".config") / "opencode" / "plugins" / "mz-relay.ts",
}


# Codex starts MCP servers with a small env allowlist; these pass the relay's own paths through.
CODEX_ENV_VARS_LINE = (
    'env_vars = ["MZ_RELAY_HOME", "MZ_RELAY_PYTHON", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", '
    '"XDG_CONFIG_HOME"]'
)


def codex_snippet_lines(bin_path: Path) -> list[str]:
    return [
        CODEX_COMMENT,
        "[mcp_servers.mz-relay]",
        f'command = "{bin_path}"',
        'args = ["mcp", "--harness", "codex"]',
        'env = { MZ_RELAY_NAME = "reviewer" }',
        CODEX_ENV_VARS_LINE,
    ]


class Install(Protocol):
    def __call__(self, dest: Path | None = None, *, force: bool = False) -> InstallPlan: ...


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    """A plugin tree with stub adapter sources and a bin shim, standing in for the real one."""
    root = tmp_path / "plugin"
    for kind, text in SOURCES.items():
        source = root / "adapters" / kind / "mz_relay.ts"
        source.parent.mkdir(parents=True)
        source.write_text(text, encoding="utf-8")
    shim = root / "bin" / "mz-relay"
    shim.parent.mkdir()
    shim.write_text("#!/bin/bash\n", encoding="utf-8")
    shim.chmod(0o755)
    return root


@pytest.fixture
def installer(relay_paths: Paths, plugin_root: Path) -> Installer:
    return Installer(relay_paths, plugin_root=plugin_root, env={})


def install_of(installer: Installer, kind: str) -> Install:
    installs: dict[str, Install] = {"pi": installer.pi, "opencode": installer.opencode}
    return installs[kind]


def record_of(paths: Paths) -> object:
    return json.loads(paths.install_record.read_text(encoding="utf-8"))


class TestDefaultDest:
    @KINDS
    def test_the_default_dest_is_under_the_passwd_home_not_home(
        self,
        installer: Installer,
        relay_paths: Paths,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
    ) -> None:
        decoy = tmp_path / "decoy-home"
        decoy.mkdir()
        monkeypatch.setenv("HOME", str(decoy))

        plan = install_of(installer, kind)()

        expected = relay_paths.pw_home / DEFAULT_DEST[kind]
        assert plan.dest == expected
        assert expected.read_text(encoding="utf-8") == SOURCES[kind]
        assert list(decoy.iterdir()) == []


class TestXdgConfigHome:
    def test_opencode_installs_under_an_absolute_xdg_config_home(
        self, relay_paths: Paths, plugin_root: Path, tmp_path: Path
    ) -> None:
        config_home = tmp_path / "xdg"
        installer = Installer(
            relay_paths, plugin_root=plugin_root, env={"XDG_CONFIG_HOME": str(config_home)}
        )

        plan = installer.opencode()

        assert plan.dest == config_home / "opencode" / "plugins" / "mz-relay.ts"
        assert plan.dest.read_text(encoding="utf-8") == OC_SOURCE

    def test_a_relative_xdg_config_home_is_ignored(
        self, relay_paths: Paths, plugin_root: Path
    ) -> None:
        installer = Installer(relay_paths, plugin_root=plugin_root, env={"XDG_CONFIG_HOME": "rel"})

        assert installer.opencode().dest == relay_paths.pw_home / DEFAULT_DEST["opencode"]

    def test_pi_ignores_xdg_config_home(
        self, relay_paths: Paths, plugin_root: Path, tmp_path: Path
    ) -> None:
        env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
        installer = Installer(relay_paths, plugin_root=plugin_root, env=env)

        assert installer.pi().dest == relay_paths.pw_home / DEFAULT_DEST["pi"]


class TestCopy:
    @KINDS
    def test_the_adapter_is_copied_byte_for_byte(
        self, installer: Installer, plugin_root: Path, tmp_path: Path, kind: str
    ) -> None:
        dest = tmp_path / "out" / "adapter.ts"

        install_of(installer, kind)(dest)

        source = plugin_root / "adapters" / kind / "mz_relay.ts"
        assert dest.read_bytes() == source.read_bytes()

    @KINDS
    def test_the_plan_names_source_dest_record_and_bin(
        self,
        installer: Installer,
        relay_paths: Paths,
        plugin_root: Path,
        tmp_path: Path,
        kind: str,
    ) -> None:
        dest = tmp_path / "adapter.ts"

        plan = install_of(installer, kind)(dest)

        assert plan == InstallPlan(
            source=plugin_root / "adapters" / kind / "mz_relay.ts",
            dest=dest,
            record=relay_paths.install_record,
            bin=plugin_root / "bin" / "mz-relay",
        )

    @KINDS
    def test_the_install_record_names_the_absolute_bin_and_version(
        self,
        installer: Installer,
        relay_paths: Paths,
        plugin_root: Path,
        tmp_path: Path,
        kind: str,
    ) -> None:
        install_of(installer, kind)(tmp_path / "adapter.ts")

        assert record_of(relay_paths) == {
            "bin": str(plugin_root / "bin" / "mz-relay"),
            "version": __version__,
        }

    @KINDS
    def test_a_rerun_over_its_own_install_succeeds(
        self, installer: Installer, tmp_path: Path, kind: str
    ) -> None:
        dest = tmp_path / "adapter.ts"
        install = install_of(installer, kind)
        install(dest)

        install(dest)

        assert dest.read_text(encoding="utf-8") == SOURCES[kind]

    @KINDS
    def test_an_older_mz_relay_install_is_overwritten(
        self, installer: Installer, tmp_path: Path, kind: str
    ) -> None:
        dest = tmp_path / "adapter.ts"
        dest.write_text(f"{ADAPTER_MARKER} (old)\nexport {{}}\n", encoding="utf-8")

        install_of(installer, kind)(dest)

        assert dest.read_text(encoding="utf-8") == SOURCES[kind]


class TestRefusal:
    @KINDS
    def test_a_foreign_file_is_refused_and_left_alone(
        self, installer: Installer, relay_paths: Paths, tmp_path: Path, kind: str
    ) -> None:
        dest = tmp_path / "adapter.ts"
        dest.write_text(FOREIGN, encoding="utf-8")

        with pytest.raises(InstallError) as caught:
            install_of(installer, kind)(dest)

        assert caught.value.code == "install_refused"
        assert str(dest) in str(caught.value)
        assert dest.read_text(encoding="utf-8") == FOREIGN
        assert not relay_paths.install_record.exists()

    @KINDS
    def test_force_overwrites_a_foreign_file(
        self, installer: Installer, relay_paths: Paths, tmp_path: Path, kind: str
    ) -> None:
        dest = tmp_path / "adapter.ts"
        dest.write_text(FOREIGN, encoding="utf-8")

        install_of(installer, kind)(dest, force=True)

        assert dest.read_text(encoding="utf-8") == SOURCES[kind]
        assert relay_paths.install_record.exists()

    def test_an_install_counts_as_ours_when_line_1_starts_with_the_adapter_comment(self) -> None:
        assert ADAPTER_MARKER == "// mz-relay adapter"


class TestRealPluginRoot:
    """Needs the real adapter sources under adapters/<kind>/."""

    @pytest.mark.parametrize(("kind", "harness_name"), [("pi", "Pi"), ("opencode", "OpenCode")])
    def test_the_installer_copies_the_plugins_own_adapter(
        self, relay_paths: Paths, tmp_path: Path, kind: str, harness_name: str
    ) -> None:
        dest = tmp_path / "adapter.ts"

        plan = install_of(Installer(relay_paths, plugin_root=PLUGIN, env={}), kind)(dest)

        assert plan.source == PLUGIN / "adapters" / kind / "mz_relay.ts"
        assert plan.bin == BIN
        assert dest.read_bytes() == plan.source.read_bytes()
        first_line = dest.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == f"{ADAPTER_MARKER} for {harness_name}"


class TestSnippet:
    def test_the_mcp_entry_runs_bin_for_opencode_under_the_name(self, tmp_path: Path) -> None:
        bin_path = tmp_path / "bin" / "mz-relay"

        snippet = opencode_snippet(bin_path, "helper")

        assert snippet["mcp"] == {
            "mz-relay": {
                "type": "local",
                "command": [str(bin_path), "mcp", "--harness", "opencode"],
                "environment": {"MZ_RELAY_NAME": "helper"},
            }
        }

    def test_the_peer_agent_denies_edit_bash_webfetch_and_task(self, tmp_path: Path) -> None:
        snippet = opencode_snippet(tmp_path / "mz-relay", "helper")

        assert snippet["agent"] == {"mz-relay-peer": PEER_AGENT}

    def test_the_snippet_is_json_with_only_mcp_and_agent(self, tmp_path: Path) -> None:
        snippet = opencode_snippet(tmp_path / "mz-relay", "helper")

        assert set(snippet) == {"mcp", "agent"}
        assert json.loads(json.dumps(snippet)) == snippet

    def test_an_empty_name_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="name"):
            opencode_snippet(tmp_path / "mz-relay", "")


class TestCodexSnippet:
    def test_the_snippet_is_the_comment_and_the_mcp_server_table(self, tmp_path: Path) -> None:
        bin_path = tmp_path / "bin" / "mz-relay"

        assert codex_snippet(bin_path, "reviewer").splitlines() == codex_snippet_lines(bin_path)

    def test_the_snippet_parses_as_toml_into_the_mcp_server(self, tmp_path: Path) -> None:
        bin_path = tmp_path / "bin" / "mz-relay"

        parsed = tomllib.loads(codex_snippet(bin_path, "reviewer"))

        assert parsed == {
            "mcp_servers": {
                "mz-relay": {
                    "command": str(bin_path),
                    "args": ["mcp", "--harness", "codex"],
                    "env": {"MZ_RELAY_NAME": "reviewer"},
                    "env_vars": [
                        "MZ_RELAY_HOME",
                        "MZ_RELAY_PYTHON",
                        "XDG_RUNTIME_DIR",
                        "XDG_STATE_HOME",
                        "XDG_CONFIG_HOME",
                    ],
                }
            }
        }

    def test_a_bin_path_with_quotes_and_backslashes_survives_toml(self, tmp_path: Path) -> None:
        bin_path = tmp_path / 'odd "dir" \\ x' / "mz-relay"

        parsed = tomllib.loads(codex_snippet(bin_path, "reviewer"))

        assert parsed["mcp_servers"]["mz-relay"]["command"] == str(bin_path)

    @pytest.mark.parametrize("name", ["", "Bad Name", 'x" = 1'])
    def test_a_name_that_is_not_a_roster_name_is_refused(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(ValueError, match="name"):
            codex_snippet(tmp_path / "mz-relay", name)

    def test_a_relative_bin_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="bin_path"):
            codex_snippet(Path("bin/mz-relay"), "reviewer")


class TestTomlString:
    """toml_string writes any text as a TOML basic string that tomllib reads back unchanged."""

    @pytest.mark.parametrize(
        "text",
        [
            "plain",
            'odd "dir" \\ x',
            "/home/u/\U0001f680/cfg",
            "tab\there",
            "line\nbreak",
            "del\x7f",
        ],
        ids=["plain", "quotes-backslash", "non-bmp", "tab", "newline", "del"],
    )
    def test_round_trips_through_tomllib(self, text: str) -> None:
        assert tomllib.loads(f"v = {install.toml_string(text)}")["v"] == text


@pytest.mark.usefixtures("agent_free_env")
class TestCli:
    """The CLI installs from this plugin, so it needs the real adapter sources under adapters/."""

    @KINDS
    def test_install_prints_the_dest_and_records_the_plugin_bin(
        self, relay_paths: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
    ) -> None:
        dest = tmp_path / "adapter.ts"

        code = main(["install", kind, "--dest", str(dest)])

        assert code == 0
        assert capsys.readouterr().out == f"installed {dest}\n"
        assert dest.read_bytes() == (PLUGIN / "adapters" / kind / "mz_relay.ts").read_bytes()
        assert record_of(relay_paths) == {"bin": str(BIN), "version": __version__}

    def test_install_opencode_without_dest_follows_xdg_config_home(
        self,
        relay_paths: Paths,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A decoy passwd home keeps a broken lookup out of the real ~/.config.
        decoy = tmp_path / "decoy-home"
        decoy.mkdir()
        monkeypatch.setattr("mz_relay.cli.user_home", lambda: decoy)
        config_home = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

        code = main(["install", "opencode"])

        dest = config_home / "opencode" / "plugins" / "mz-relay.ts"
        assert code == 0
        assert capsys.readouterr().out == f"installed {dest}\n"
        assert list(decoy.iterdir()) == []

    def test_install_over_a_foreign_file_exits_2_and_keeps_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dest = tmp_path / "adapter.ts"
        dest.write_text(FOREIGN, encoding="utf-8")

        code = main(["install", "pi", "--dest", str(dest)])

        assert code == 2
        assert capsys.readouterr().err.startswith("mz-relay: install_refused: ")
        assert dest.read_text(encoding="utf-8") == FOREIGN

    def test_install_with_force_overwrites_a_foreign_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dest = tmp_path / "adapter.ts"
        dest.write_text(FOREIGN, encoding="utf-8")

        code = main(["install", "opencode", "--dest", str(dest), "--force"])

        assert code == 0
        assert capsys.readouterr().out == f"installed {dest}\n"
        assert dest.read_bytes() == (PLUGIN / "adapters" / "opencode" / "mz_relay.ts").read_bytes()

    def test_snippet_opencode_prints_the_snippet_indented_by_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["snippet", "opencode", "--name", "helper"])

        assert code == 0
        expected = opencode_snippet(BIN, "helper")
        assert capsys.readouterr().out == json.dumps(expected, indent=2) + "\n"

    def test_snippet_codex_prints_the_toml_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["snippet", "codex", "--name", "reviewer"])

        out = capsys.readouterr().out
        assert code == 0
        assert out.splitlines() == codex_snippet_lines(BIN)
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    def test_snippet_codex_with_a_bad_name_exits_2_and_prints_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["snippet", "codex", "--name", "Bad Name"])

        captured = capsys.readouterr()
        assert code == 2
        assert captured.out == ""
        assert captured.err.startswith("mz-relay: ")


@pytest.fixture
def ctx(relay_paths: Paths, relay_home: Path) -> AdapterContext:
    return AdapterContext(paths=relay_paths, clock=SystemClock(), pw_home=relay_home, base_env={})


class TestBuildAdapters:
    @pytest.mark.parametrize("harness", [Harness.PI, Harness.OPENCODE])
    def test_pi_and_opencode_get_a_socket_adapter(
        self, ctx: AdapterContext, harness: Harness
    ) -> None:
        adapter = build_adapters(ctx)[harness]

        assert isinstance(adapter, SocketAdapter)
        assert adapter.via == harness.value

    def test_claude_keeps_its_claude_adapter(self, ctx: AdapterContext) -> None:
        assert isinstance(build_adapters(ctx)[Harness.CLAUDE], ClaudeAdapter)

    def test_codex_gets_a_codex_adapter_that_runs_with_the_context_env_and_home(
        self, relay_paths: Paths, tmp_path: Path
    ) -> None:
        pw_home, codex_home, log = tmp_path / "pw-home", tmp_path / "codex", tmp_path / "log"
        pw_home.mkdir()
        write_index(codex_home, index_line("t-new", "reviewer"))
        base_env = {"PATH": FAST_PATH, "FAKE_CODEX_LOG": str(log), "FAKE_CODEX_MARKER": "m-ctx"}
        ctx = AdapterContext(
            paths=relay_paths, clock=SystemClock(), pw_home=pw_home, base_env=base_env
        )

        adapter = build_adapters(ctx)[Harness.CODEX]
        result = adapter.deliver(codex_job(adapter, codex_spec(codex_home, pw_home)))

        assert isinstance(adapter, CodexAdapter)
        assert result.outcome is Outcome.HANDED_OFF
        env = fake_calls(log)[0]["env"]
        assert (env["HOME"], env["FAKE_CODEX_MARKER"]) == (str(pw_home), "m-ctx")
