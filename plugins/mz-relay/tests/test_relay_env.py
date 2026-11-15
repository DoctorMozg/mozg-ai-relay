"""The relay_env fixture keeps environment values out of pytest's failure reports."""

from __future__ import annotations

import pytest

SECRET = "sk-test-0123456789abcdef"


class TestRelayEnv:
    def test_its_repr_names_variables_but_never_their_values(
        self, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> None:
        monkeypatch.setenv("MZR_TEST_API_KEY", SECRET)

        env = request.getfixturevalue("relay_env")

        assert SECRET not in repr(env)
        assert "MZR_TEST_API_KEY" in repr(env)
        assert {**env}["MZR_TEST_API_KEY"] == SECRET
