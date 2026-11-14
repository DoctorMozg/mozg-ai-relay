"""The five version fields of the repo agree, so a release never ships two numbers."""

from __future__ import annotations

import importlib
import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = ROOT / "plugins" / "mz-relay" / ".claude-plugin" / "plugin.json"
EXPECTED = "0.1.1"


def read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def marketplace_entry() -> dict[str, Any]:
    entries = [entry for entry in read_json(MARKETPLACE)["plugins"] if entry["name"] == "mz-relay"]
    assert len(entries) == 1, f"marketplace lists mz-relay {len(entries)} times"
    entry: dict[str, Any] = entries[0]
    return entry


def version_fields() -> dict[str, object]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        "pyproject.toml": pyproject["project"]["version"],
        "marketplace metadata": read_json(MARKETPLACE)["metadata"]["version"],
        "marketplace entry": marketplace_entry()["version"],
        "plugin.json": read_json(PLUGIN_MANIFEST)["version"],
        "mz_relay.__version__": importlib.import_module("mz_relay").__version__,
    }


class TestVersions:
    def test_all_five_version_fields_are_0_1_0(self) -> None:
        assert version_fields() == {
            "pyproject.toml": EXPECTED,
            "marketplace metadata": EXPECTED,
            "marketplace entry": EXPECTED,
            "plugin.json": EXPECTED,
            "mz_relay.__version__": EXPECTED,
        }

    def test_marketplace_entry_points_at_the_plugin_directory(self) -> None:
        entry = marketplace_entry()
        assert entry["source"] == "./plugins/mz-relay"
        manifest = ROOT / entry["source"] / ".claude-plugin" / "plugin.json"
        assert read_json(manifest)["name"] == "mz-relay"
