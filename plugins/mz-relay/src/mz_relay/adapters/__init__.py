"""Harness adapters: what relayd delivers through, one per harness."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mz_relay.clock import Clock
from mz_relay.delivery import HarnessAdapter
from mz_relay.paths import Paths
from mz_relay.roster import Harness

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterContext:
    paths: Paths
    clock: Clock
    pw_home: Path
    base_env: Mapping[str, str]


def build_adapters(ctx: AdapterContext) -> dict[Harness, HarnessAdapter]:
    """The adapters relayd starts a delivery worker for; none yet, so every agent is pull-only."""
    if not isinstance(ctx, AdapterContext):
        raise ValueError(f"ctx must be an AdapterContext, got {type(ctx).__name__}")
    adapters: dict[Harness, HarnessAdapter] = {}
    log.info("built %d delivery adapters", len(adapters))
    return adapters
