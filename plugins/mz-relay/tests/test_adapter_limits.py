"""The Pi adapter's tool limits equal the Python ones; TypeScript cannot import them."""

from __future__ import annotations

import re

from conftest import PLUGIN
from mz_relay.mcp_server import INBOX_LIMIT_DEFAULT, INBOX_LIMIT_MAX, INBOX_LIMIT_MIN
from mz_relay.policy import MAX_BODY_CHARS

PI_ADAPTER = PLUGIN / "adapters" / "pi" / "mz_relay.ts"


def literal(pattern: str, source: str) -> int:
    found = re.findall(pattern, source)
    assert len(found) == 1, f"{pattern!r} matched {len(found)} times in {PI_ADAPTER}"
    return int(found[0])


class TestPiAdapterLimits:
    def test_literals_match_the_python_constants(self) -> None:
        source = PI_ADAPTER.read_text(encoding="utf-8")
        inbox_limit = re.search(r"limit: \{(.*?)\}", source, re.DOTALL)
        assert inbox_limit is not None, f"no inbox limit schema in {PI_ADAPTER}"

        assert literal(r"maxLength: (\d+)", source) == MAX_BODY_CHARS
        assert literal(r"const INBOX_LIMIT_DEFAULT = (\d+);", source) == INBOX_LIMIT_DEFAULT
        assert literal(r"minimum: (\d+)", inbox_limit.group(1)) == INBOX_LIMIT_MIN
        assert literal(r"maximum: (\d+)", inbox_limit.group(1)) == INBOX_LIMIT_MAX
