"""Installs the Pi and OpenCode adapters and builds the OpenCode config snippet."""

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


def opencode_snippet(bin_path: Path, name: str) -> dict[str, object]:
    """The opencode.json entries the OpenCode adapter's config hook would otherwise inject."""
    if not bin_path.is_absolute():
        raise ValueError(f"bin_path must be absolute, got {bin_path}")
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise ValueError(f"name must be a roster agent name ({NAME_RE.pattern}), got {name!r}")
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
