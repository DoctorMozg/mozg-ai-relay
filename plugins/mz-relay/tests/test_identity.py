"""Which roster agent a registering session is: by name, config dir and longest cwd prefix."""

from __future__ import annotations

from pathlib import Path

import pytest
from mz_relay.errors import IdentityError
from mz_relay.identity import IdentityQuery, resolve_identity
from mz_relay.roster import Harness, Roster, parse_roster

PW_HOME = Path("/home/u")


def agent(name: str, harness: str, extra: str = "") -> str:
    return (
        f'[agents.{name}]\nharness = "{harness}"\nlabel = "personal"\n'
        f'role = "{name}-role"\ndescription = "{name} does things"\n{extra}'
    )


ROSTER_TEXT = "version = 1\n" + "".join(
    [
        agent("solo", "claude", 'claude_config_dir = "~/.claude-solo"\n'),
        agent("left", "claude", 'claude_config_dir = "~/shared"\ncwd = "/w/a"\n'),
        agent("deep", "claude", 'claude_config_dir = "~/shared"\ncwd = "/w/a/deep"\n'),
        agent("right", "claude", 'claude_config_dir = "~/shared"\ncwd = "/w/b"\n'),
        agent("twin-one", "claude", 'claude_config_dir = "~/twin"\n'),
        agent("twin-two", "claude", 'claude_config_dir = "~/twin"\n'),
        agent("reviewer", "codex", 'codex_thread = "reviewer"\n'),
        agent("alice", "opencode", 'cwd = "/tmp/mzr-alice"\n'),
        agent("bob", "opencode"),
    ]
)


@pytest.fixture
def roster() -> Roster:
    return parse_roster(ROSTER_TEXT, pw_home=PW_HOME)


def claude_query(config: str, cwd: str, name: str | None = None) -> IdentityQuery:
    return IdentityQuery(
        harness=Harness.CLAUDE, cwd=Path(cwd), name=name, config_dir=PW_HOME / config
    )


class TestResolveByFacts:
    def test_a_unique_config_dir_match_is_the_agent(self, roster: Roster) -> None:
        assert resolve_identity(roster, claude_query(".claude-solo", "/anywhere")).name == "solo"

    def test_a_shared_config_dir_is_split_by_cwd_prefix(self, roster: Roster) -> None:
        assert resolve_identity(roster, claude_query("shared", "/w/a/sub")).name == "left"
        assert resolve_identity(roster, claude_query("shared", "/w/b")).name == "right"

    def test_the_longest_cwd_prefix_wins(self, roster: Roster) -> None:
        assert resolve_identity(roster, claude_query("shared", "/w/a/deep/x")).name == "deep"

    def test_a_cwd_outside_every_prefix_is_unresolved(self, roster: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            resolve_identity(roster, claude_query("shared", "/elsewhere"))
        assert caught.value.code == "identity_unresolved"

    def test_a_cwd_prefix_matches_whole_path_components_only(self, roster: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            resolve_identity(roster, claude_query("shared", "/w/ab"))
        assert caught.value.code == "identity_unresolved"

    def test_equal_candidates_are_ambiguous_and_both_are_named(self, roster: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            resolve_identity(roster, claude_query("twin", "/w/a"))
        assert caught.value.code == "identity_ambiguous"
        assert "twin-one" in caught.value.message
        assert "twin-two" in caught.value.message

    def test_an_unknown_config_dir_is_unresolved(self, roster: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            resolve_identity(roster, claude_query("other", "/w/a"))
        assert caught.value.code == "identity_unresolved"

    def test_an_agent_with_a_cwd_beats_one_without(self, roster: Roster) -> None:
        query = IdentityQuery(harness=Harness.OPENCODE, cwd=Path("/tmp/mzr-alice/sub"))
        assert resolve_identity(roster, query).name == "alice"

    def test_an_agent_without_a_cwd_matches_any_cwd(self, roster: Roster) -> None:
        query = IdentityQuery(harness=Harness.OPENCODE, cwd=Path("/srv/other"))
        assert resolve_identity(roster, query).name == "bob"

    def test_a_harness_without_agents_is_unresolved(self, roster: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            resolve_identity(roster, IdentityQuery(harness=Harness.PI, cwd=Path("/w")))
        assert caught.value.code == "identity_unresolved"

    def test_the_config_dir_is_compared_after_resolving_symlinks(self, tmp_path: Path) -> None:
        real = tmp_path / "real-claude"
        real.mkdir()
        link = tmp_path / "link-claude"
        link.symlink_to(real)
        text = "version = 1\n" + agent("cc", "claude", f'claude_config_dir = "{real}"\n')
        roster = parse_roster(text, pw_home=PW_HOME)
        query = IdentityQuery(harness=Harness.CLAUDE, cwd=Path("/w"), config_dir=link)
        assert resolve_identity(roster, query).name == "cc"


class TestResolveRealCwd:
    """The roster cwd and the session cwd name one directory, spelled through a link or `..`."""

    @pytest.fixture
    def tree(self, tmp_path: Path) -> Path:
        (tmp_path / "real" / "proj" / "src").mkdir(parents=True)
        (tmp_path / "real" / "other").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")
        return tmp_path

    @staticmethod
    def resolve(roster_cwd: str, session_cwd: Path) -> str:
        text = "version = 1\n" + agent("alice", "opencode", f'cwd = "{roster_cwd}"\n')
        text += agent("bob", "opencode")
        roster = parse_roster(text, pw_home=PW_HOME)
        return resolve_identity(
            roster, IdentityQuery(harness=Harness.OPENCODE, cwd=session_cwd)
        ).name

    @pytest.mark.parametrize("sub", ["", "src"], ids=["root", "subdir"])
    def test_a_roster_cwd_through_a_symlink_matches_the_real_session_dir(
        self, tree: Path, sub: str
    ) -> None:
        session = tree / "real" / "proj" / sub if sub else tree / "real" / "proj"
        assert self.resolve(f"{tree}/link/proj", session) == "alice"

    def test_a_roster_cwd_with_dot_dot_matches_the_dir_it_names(self, tree: Path) -> None:
        assert self.resolve(f"{tree}/real/other/../proj", tree / "real" / "proj" / "src") == "alice"

    def test_a_session_cwd_through_a_symlink_matches_the_real_roster_dir(self, tree: Path) -> None:
        assert self.resolve(f"{tree}/real/proj", tree / "link" / "proj") == "alice"

    def test_a_sibling_of_the_linked_dir_still_goes_to_the_cwd_less_agent(self, tree: Path) -> None:
        assert self.resolve(f"{tree}/link/proj", tree / "real" / "other") == "bob"


class TestResolveByName:
    def test_a_name_wins_over_cwd_and_config_dir(self, roster: Roster) -> None:
        assert resolve_identity(roster, claude_query("twin", "/w/b", name="left")).name == "left"

    def test_a_name_on_the_wrong_harness_is_a_harness_mismatch(self, roster: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            resolve_identity(roster, claude_query(".claude-solo", "/w", name="reviewer"))
        assert caught.value.code == "harness_mismatch"

    def test_an_unknown_name_is_unknown_agent(self, roster: Roster) -> None:
        with pytest.raises(IdentityError) as caught:
            resolve_identity(roster, claude_query(".claude-solo", "/w", name="ghost"))
        assert caught.value.code == "unknown_agent"
