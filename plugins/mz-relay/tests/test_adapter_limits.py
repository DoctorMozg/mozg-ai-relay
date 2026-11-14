"""Constants the TS adapters copy from Python stay equal; TypeScript cannot import them."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import BIN, PLUGIN
from mz_relay.install import PEER_AGENT_NAME, opencode_snippet
from mz_relay.mcp_server import INBOX_LIMIT_DEFAULT, INBOX_LIMIT_MAX, INBOX_LIMIT_MIN
from mz_relay.policy import MAX_BODY_CHARS

PI_ADAPTER = PLUGIN / "adapters" / "pi" / "mz_relay.ts"
OC_ADAPTER = PLUGIN / "adapters" / "opencode" / "mz_relay.ts"


def match(pattern: str, source: str, path: Path = PI_ADAPTER) -> str:
    found = re.findall(pattern, source, re.DOTALL)
    assert len(found) == 1, f"{pattern!r} matched {len(found)} times in {path}"
    return str(found[0])


def literal(pattern: str, source: str) -> int:
    return int(match(pattern, source))


def ts_object(text: str) -> object:
    """A TS object literal with bare keys, string values and trailing commas, read as JSON."""
    quoted = re.sub(r"(\w+):", r'"\1":', text)
    return json.loads(re.sub(r",(\s*\})", r"\1", quoted))


class TestPiAdapterLimits:
    def test_literals_match_the_python_constants(self) -> None:
        source = PI_ADAPTER.read_text(encoding="utf-8")
        inbox_limit = re.search(r"limit: \{(.*?)\}", source, re.DOTALL)
        assert inbox_limit is not None, f"no inbox limit schema in {PI_ADAPTER}"

        assert literal(r"maxLength: (\d+)", source) == MAX_BODY_CHARS
        assert literal(r"const INBOX_LIMIT_DEFAULT = (\d+);", source) == INBOX_LIMIT_DEFAULT
        assert literal(r"minimum: (\d+)", inbox_limit.group(1)) == INBOX_LIMIT_MIN
        assert literal(r"maximum: (\d+)", inbox_limit.group(1)) == INBOX_LIMIT_MAX


class TestOpenCodePeerAgent:
    def test_the_injected_agent_equals_the_snippets(self) -> None:
        source = OC_ADAPTER.read_text(encoding="utf-8")
        name = match(r'const PEER_AGENT_NAME = "([^"]+)";', source, OC_ADAPTER)
        body = match(r"c\.agent\[PEER_AGENT_NAME\] = (\{.*?\});", source, OC_ADAPTER)

        agents = opencode_snippet(BIN, "helper")["agent"]
        assert name == PEER_AGENT_NAME
        assert isinstance(agents, dict)
        assert ts_object(body) == agents[PEER_AGENT_NAME]
