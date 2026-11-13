"""Installs the Pi and OpenCode adapters and builds the OpenCode and Codex config snippets."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from mz_relay import __version__
from mz_relay.errors import PathError, RelayError
from mz_relay.paths import Paths
from mz_relay.roster import NAME_RE

log = logging.getLogger(__name__)

ADAPTER_MARKER = "// mz-relay adapter"
PEER_AGENT_NAME = "mz-relay-peer"
MCP_SERVER_NAME = "mz-relay"


class InstallError(RelayError):
    CODES: ClassVar[frozenset[str]] = frozenset({"install_refused"})

    def __init__(self, message: str) -> None:
        super().__init__("install_refused", message)


@dataclass(frozen=True)
class InstallPlan:
    source: Path
    dest: Path
    record: Path
    bin: Path


def plugin_bin(plugin_root: Path) -> Path:
    return plugin_root / "bin" / "mz-relay"


class Installer:
    """Copies an adapter from the plugin into the harness and records the plugin's bin."""

    def __init__(self, paths: Paths, *, plugin_root: Path) -> None:
        if not isinstance(paths, Paths):
            raise ValueError(f"paths must be a Paths, got {type(paths).__name__}")
        if not plugin_root.is_absolute():
            raise ValueError(f"plugin_root must be absolute, got {plugin_root}")
        self._paths = paths
        self._root = plugin_root

    def pi(self, dest: Path | None = None, *, force: bool = False) -> InstallPlan:
        default = self._paths.pw_home / ".pi" / "agent" / "extensions" / "mz-relay" / "index.ts"
        return self._install("pi", dest or default, force=force)

    def opencode(self, dest: Path | None = None, *, force: bool = False) -> InstallPlan:
        default = self._paths.pw_home / ".config" / "opencode" / "plugins" / "mz-relay.ts"
        return self._install("opencode", dest or default, force=force)

    def _install(self, kind: str, dest: Path, *, force: bool) -> InstallPlan:
        if not dest.is_absolute():
            raise PathError(f"install destination must be absolute, got {dest}")
        plan = InstallPlan(
            source=self._root / "adapters" / kind / "mz_relay.ts",
            dest=dest,
            record=self._paths.install_record,
            bin=plugin_bin(self._root),
        )
        for required in (plan.source, plan.bin):
            if not required.is_file():
                raise PathError(f"the plugin at {self._root} has no {required}")
        if dest.exists() and not _is_ours(dest):
            if not force:
                log.info("install of the %s adapter refused: %s is not ours", kind, dest)
                raise InstallError(
                    f"{dest} exists and is not an mz-relay adapter; pass --force to overwrite it"
                )
            log.warning("overwriting %s, which is not an mz-relay adapter, as forced", dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plan.source, dest)
            plan.record.parent.mkdir(parents=True, exist_ok=True)
            record = {"bin": str(plan.bin), "version": __version__}
            plan.record.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise PathError(f"cannot install the {kind} adapter to {dest}: {exc}") from exc
        log.info("installed the %s adapter to %s, bin %s", kind, dest, plan.bin)
        return plan


def _is_ours(dest: Path) -> bool:
    try:
        with dest.open("rb") as handle:
            first_line = handle.readline()
    except OSError as exc:
        raise PathError(f"cannot read {dest}: {exc}") from exc
    return first_line.startswith(ADAPTER_MARKER.encode())


def _check_snippet_args(bin_path: Path, name: str) -> None:
    if not bin_path.is_absolute():
        raise ValueError(f"bin_path must be absolute, got {bin_path}")
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise ValueError(f"name must be a roster agent name ({NAME_RE.pattern}), got {name!r}")


def opencode_snippet(bin_path: Path, name: str) -> dict[str, object]:
    """The opencode.json entries the OpenCode adapter's config hook would otherwise inject."""
    _check_snippet_args(bin_path, name)
    return {
        "mcp": {
            MCP_SERVER_NAME: {
                "type": "local",
                "command": [str(bin_path), "mcp", "--harness", "opencode"],
                "environment": {"MZ_RELAY_NAME": name},
            }
        },
        "agent": {
            PEER_AGENT_NAME: {
                "description": "Answers mz-relay peer messages, read-only",
                "mode": "primary",
                "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny", "task": "deny"},
            }
        },
    }


def codex_snippet(bin_path: Path, name: str) -> str:
    """The config.toml table that starts the mz-relay MCP server in a Codex account, as TOML."""
    _check_snippet_args(bin_path, name)
    return (
        f'# Paste into <CODEX_HOME>/config.toml of the account that runs "{name}".'
        " Start Codex without -c.\n"
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f"command = {toml_string(str(bin_path))}\n"
        'args = ["mcp", "--harness", "codex"]\n'
        f"env = {{ MZ_RELAY_NAME = {toml_string(name)} }}\n"
    )


def toml_string(value: str) -> str:
    """A TOML basic string; control characters, which TOML forbids raw, become \\u escapes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        f"\\u{ord(char):04x}" if ord(char) < 0x20 or ord(char) == 0x7F else char for char in escaped
    )
    return f'"{escaped}"'
