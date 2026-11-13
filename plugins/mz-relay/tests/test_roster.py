"""roster.toml parsing, validation and the mtime-reloading cache."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from conftest import PLUGIN
from mz_relay.errors import IdentityError, RosterError
from mz_relay.roster import (
    Harness,
    Label,
    PolicySettings,
    Roster,
    RosterCache,
    ToolMode,
    load_roster,
    parse_roster,
)

EXAMPLE = PLUGIN / "data" / "roster.example.toml"
PW_HOME = Path("/home/u")

OPENCODE_X = '[agents.x]\nharness = "opencode"\nlabel = "personal"\nrole = "r"\ndescription = "d"\n'
OPENCODE_Y = '[agents.y]\nharness = "opencode"\nlabel = "personal"\nrole = "r"\ndescription = "d"\n'
CLAUDE_X = (
    '[agents.x]\nharness = "claude"\nlabel = "personal"\nrole = "r"\ndescription = "d"\n'
    'claude_config_dir = "~/.claude"\n'
)
CODEX_X = (
    '[agents.x]\nharness = "codex"\nlabel = "personal"\nrole = "r"\ndescription = "d"\n'
    'codex_thread = "x"\n'
)


def versioned(body: str) -> str:
    return "version = 1\n" + body


class TestExample:
    @pytest.fixture
    def example(self) -> Roster:
        return load_roster(EXAMPLE, pw_home=PW_HOME)

    def test_the_shipped_example_has_five_agents(self, example: Roster) -> None:
        assert sorted(example.agents) == ["architect", "builder", "helper", "reviewer", "scout"]

    def test_tilde_paths_expand_against_the_passwd_home(self, example: Roster) -> None:
        assert example.get("architect").claude_config_dir == PW_HOME / ".claude"
        assert example.get("builder").claude_config_dir == PW_HOME / "claude-corp" / ".claude"
        assert example.get("reviewer").codex_home == PW_HOME / ".codex"
        assert example.get("helper").cwd == PW_HOME / "Work" / "docs-site"

    def test_codex_bin_defaults_to_codex(self, example: Roster) -> None:
        reviewer = example.get("reviewer")
        assert reviewer.codex_bin == "codex"
        assert reviewer.codex_thread == "reviewer"
        assert reviewer.harness is Harness.CODEX

    def test_tools_read_only_is_parsed_and_full_is_the_default(self, example: Roster) -> None:
        assert example.get("scout").tools is ToolMode.READ_ONLY
        assert example.get("architect").tools is ToolMode.FULL

    def test_missing_may_send_to_means_every_other_agent(self, example: Roster) -> None:
        assert example.get("helper").may_send_to == frozenset(
            {"architect", "builder", "reviewer", "scout"}
        )
        assert example.get("scout").may_send_to == frozenset({"architect"})

    def test_policy_values_match_the_defaults(self, example: Roster) -> None:
        assert example.policy == PolicySettings(
            hold_corporate_to_personal=True,
            hop_budget=4,
            turn_cap=20,
            inbox_cap=50,
            default_ttl_s=3600,
        )

    def test_card_carries_the_public_agent_fields(self, example: Roster) -> None:
        assert example.get("builder").card() == {
            "name": "builder",
            "role": "builder",
            "harness": "claude",
            "label": "corporate",
            "description": "Writes code in the corporate repo and sends diffs for review.",
            "tools": "full",
        }

    def test_labels_are_parsed(self, example: Roster) -> None:
        assert example.get("builder").label is Label.CORPORATE
        assert example.get("scout").label is Label.LOCAL

    def test_get_of_an_unknown_name_raises_unknown_agent(self, example: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            example.get("ghost")
        assert caught.value.code == "unknown_agent"


class TestValidation:
    @pytest.mark.parametrize(
        ("text", "key"),
        [
            ("version = 2\n" + OPENCODE_X, "version"),
            (OPENCODE_X, "version"),
            ("version = 1\n", "agents"),
            (versioned(OPENCODE_X.replace("agents.x", "agents.Bad")), "agents.Bad"),
            (versioned(OPENCODE_X.replace("agents.x", "agents." + "a" * 33)), "agents." + "a" * 33),
            (versioned(OPENCODE_X.replace('"opencode"', '"gemini"')), "agents.x.harness"),
            (versioned(OPENCODE_X.replace('"personal"', '"secret"')), "agents.x.label"),
            (versioned(OPENCODE_X + 'tools = "write"\n'), "agents.x.tools"),
            (versioned(OPENCODE_X + 'colour = "red"\n'), "agents.x.colour"),
            (versioned('colour = "red"\n' + OPENCODE_X), "colour"),
            (versioned(OPENCODE_X + '[policy]\ncolour = "red"\n'), "policy.colour"),
            (
                versioned(CLAUDE_X.replace('claude_config_dir = "~/.claude"\n', "")),
                "agents.x.claude_config_dir",
            ),
            (versioned(CODEX_X.replace('codex_thread = "x"\n', "")), "agents.x.codex_thread"),
            (versioned(CLAUDE_X + 'codex_thread = "t"\n'), "agents.x.codex_thread"),
            (
                versioned(OPENCODE_X + 'claude_config_dir = "~/.claude"\n'),
                "agents.x.claude_config_dir",
            ),
            (versioned(OPENCODE_X.replace('role = "r"', 'role = ""')), "agents.x.role"),
            (versioned(OPENCODE_X.replace('role = "r"', f'role = "{"r" * 41}"')), "agents.x.role"),
            (
                versioned(OPENCODE_X.replace('description = "d"', f'description = "{"d" * 301}"')),
                "agents.x.description",
            ),
            (versioned(OPENCODE_X + 'may_send_to = ["nobody"]\n'), "agents.x.may_send_to"),
            (versioned(OPENCODE_X + "[policy]\nhop_budget = 0\n"), "policy.hop_budget"),
            (versioned(OPENCODE_X + "[policy]\nhop_budget = 17\n"), "policy.hop_budget"),
            (versioned(OPENCODE_X + "[policy]\nturn_cap = 1001\n"), "policy.turn_cap"),
            (versioned(OPENCODE_X + "[policy]\ninbox_cap = 0\n"), "policy.inbox_cap"),
            (versioned(OPENCODE_X + "[policy]\ndefault_ttl_s = 59\n"), "policy.default_ttl_s"),
        ],
        ids=[
            "version-2",
            "version-missing",
            "no-agents",
            "uppercase-name",
            "name-33-chars",
            "unknown-harness",
            "unknown-label",
            "unknown-tools",
            "unknown-key",
            "unknown-top-level-key",
            "unknown-policy-key",
            "claude-without-config-dir",
            "codex-without-thread",
            "codex-key-on-claude",
            "claude-key-on-opencode",
            "empty-role",
            "role-41-chars",
            "description-301-chars",
            "may-send-to-unknown",
            "hop-budget-0",
            "hop-budget-17",
            "turn-cap-1001",
            "inbox-cap-0",
            "ttl-59",
        ],
    )
    def test_invalid_rosters_name_the_offending_key(self, text: str, key: str) -> None:
        with pytest.raises(RosterError) as caught:
            parse_roster(text, pw_home=PW_HOME)
        assert caught.value.key == key
        assert caught.value.code == "roster_invalid"

    def test_version_2_says_it_is_unsupported(self) -> None:
        with pytest.raises(RosterError, match="unsupported roster version 2"):
            parse_roster("version = 2\n" + OPENCODE_X, pw_home=PW_HOME)

    def test_unknown_harness_message_lists_the_four_harnesses(self) -> None:
        with pytest.raises(RosterError) as caught:
            parse_roster(versioned(OPENCODE_X.replace('"opencode"', '"gemini"')), pw_home=PW_HOME)
        for harness in ("claude", "codex", "pi", "opencode"):
            assert harness in caught.value.message

    def test_invalid_toml_reports_the_tomllib_line_number(self) -> None:
        with pytest.raises(RosterError) as caught:
            parse_roster('version = 1\n[agents.x]\nharness "claude"\n', pw_home=PW_HOME)
        assert "line 3" in caught.value.message

    def test_missing_file_carries_its_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.toml"
        with pytest.raises(RosterError) as caught:
            load_roster(missing, pw_home=PW_HOME)
        assert caught.value.path == missing

    def test_boundary_values_are_accepted(self) -> None:
        name = "a" + "b-9" * 10 + "c"
        text = versioned(
            OPENCODE_X.replace("agents.x", f"agents.{name}")
            .replace('role = "r"', f'role = "{"r" * 40}"')
            .replace('description = "d"', f'description = "{"d" * 300}"')
            + "[policy]\nhop_budget = 16\nturn_cap = 1000\ninbox_cap = 1000\ndefault_ttl_s = 60\n"
        )
        roster = parse_roster(text, pw_home=PW_HOME)
        assert len(name) == 32
        assert len(roster.get(name).role) == 40
        assert roster.policy.hop_budget == 16
        assert roster.policy.default_ttl_s == 60

    def test_defaults_fill_optional_agent_keys(self) -> None:
        roster = parse_roster(versioned(OPENCODE_X + OPENCODE_Y), pw_home=PW_HOME)
        x = roster.get("x")
        assert x.may_send_to == frozenset({"y"})
        assert x.tools is ToolMode.FULL
        assert x.cwd is None
        assert x.codex_bin == "codex"

    def test_empty_may_send_to_means_nobody(self) -> None:
        roster = parse_roster(
            versioned(OPENCODE_X + "may_send_to = []\n" + OPENCODE_Y), pw_home=PW_HOME
        )
        assert roster.get("x").may_send_to == frozenset()

    def test_codex_home_defaults_to_dot_codex_under_the_passwd_home(self) -> None:
        roster = parse_roster(versioned(CODEX_X), pw_home=PW_HOME)
        assert roster.get("x").codex_home == PW_HOME / ".codex"

    def test_hold_policy_can_be_turned_off(self) -> None:
        text = versioned(OPENCODE_X + "[policy]\nhold_corporate_to_personal = false\n")
        assert parse_roster(text, pw_home=PW_HOME).policy.hold_corporate_to_personal is False


@pytest.fixture
def loop(tmp_path: Path) -> Path:
    """A symlink to itself, so resolving it or any path below it never ends."""
    link = tmp_path / "loop"
    link.symlink_to(link)
    return link


def with_looping_path(loop: Path, key: str = "cwd") -> str:
    """Agent y plus agent x whose `key` points below the loop."""
    claude = key == "claude_config_dir"
    agent = CLAUDE_X.replace('claude_config_dir = "~/.claude"\n', "") if claude else OPENCODE_X
    return versioned(OPENCODE_Y + agent + f'{key} = "{loop / "project"}"\n')


class TestSymlinkLoops:
    """A looping path fails the roster at load, not every later identity lookup."""

    @pytest.mark.parametrize("key", ["cwd", "claude_config_dir"])
    def test_a_looping_path_is_a_roster_error_naming_its_key(
        self, loop: Path, tmp_path: Path, key: str
    ) -> None:
        with pytest.raises(RosterError) as caught:
            parse_roster(with_looping_path(loop, key), pw_home=tmp_path)

        assert caught.value.key == f"agents.x.{key}"
        assert f"agents.x.{key}" in caught.value.message

    def test_the_link_itself_as_cwd_is_a_roster_error(self, loop: Path, tmp_path: Path) -> None:
        text = versioned(OPENCODE_X + f'cwd = "{loop}"\n')

        with pytest.raises(RosterError) as caught:
            parse_roster(text, pw_home=tmp_path)

        assert caught.value.key == "agents.x.cwd"

    def test_load_roster_carries_the_file_and_the_key(self, loop: Path, tmp_path: Path) -> None:
        path = tmp_path / "roster.toml"
        path.write_text(with_looping_path(loop), encoding="utf-8")

        with pytest.raises(RosterError) as caught:
            load_roster(path, pw_home=tmp_path)

        assert (caught.value.path, caught.value.key) == (path, "agents.x.cwd")

    def test_an_edit_adding_a_looping_cwd_keeps_the_last_good_roster(
        self, loop: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "roster.toml"
        TestRosterCache.write(path, versioned(OPENCODE_Y), 1_700_000_000)
        cache = RosterCache(path, tmp_path)

        TestRosterCache.write(path, with_looping_path(loop), 1_700_000_100)

        assert sorted(cache.current().agents) == ["y"]


class TestRosterCache:
    @staticmethod
    def write(path: Path, text: str, mtime_s: int) -> None:
        path.write_text(text, encoding="utf-8")
        os.utime(path, ns=(mtime_s * 1_000_000_000, mtime_s * 1_000_000_000))

    def test_a_new_mtime_makes_a_new_agent_visible(self, tmp_path: Path) -> None:
        path = tmp_path / "roster.toml"
        self.write(path, versioned(OPENCODE_X), 1_700_000_000)
        cache = RosterCache(path, PW_HOME)
        assert sorted(cache.current().agents) == ["x"]

        self.write(path, versioned(OPENCODE_X + OPENCODE_Y), 1_700_000_100)
        assert sorted(cache.current().agents) == ["x", "y"]

    def test_a_broken_edit_keeps_the_last_good_roster_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "roster.toml"
        self.write(path, versioned(OPENCODE_X), 1_700_000_000)
        cache = RosterCache(path, PW_HOME)
        assert sorted(cache.current().agents) == ["x"]

        self.write(path, "version = 1\n[agents.x\n", 1_700_000_100)
        with caplog.at_level(logging.WARNING):
            kept = cache.current()
        assert sorted(kept.agents) == ["x"]
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_a_fixed_edit_after_a_broken_one_is_loaded(self, tmp_path: Path) -> None:
        path = tmp_path / "roster.toml"
        self.write(path, versioned(OPENCODE_X), 1_700_000_000)
        cache = RosterCache(path, PW_HOME)
        cache.current()
        self.write(path, "version = 1\n[agents.x\n", 1_700_000_100)
        cache.current()

        self.write(path, versioned(OPENCODE_X + OPENCODE_Y), 1_700_000_200)
        assert sorted(cache.current().agents) == ["x", "y"]
