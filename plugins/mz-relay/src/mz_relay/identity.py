"""Which roster agent a registering session is: by name, else by harness, config dir and cwd."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from mz_relay.errors import IdentityError
from mz_relay.roster import AgentSpec, Harness, Roster

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdentityQuery:
    harness: Harness
    cwd: Path
    name: str | None = None
    config_dir: Path | None = None


def resolve_identity(roster: Roster, query: IdentityQuery) -> AgentSpec:
    """The one agent the query names or matches; IdentityError when there is not exactly one."""
    if not query.cwd.is_absolute():
        raise IdentityError("identity_unresolved", f"cwd must be absolute, got {query.cwd}")
    if query.name is not None:
        return _by_name(roster, query.name, query.harness)
    candidates = [spec for spec in roster.agents.values() if spec.harness is query.harness]
    if query.harness is Harness.CLAUDE:
        candidates = _by_config_dir(candidates, query.config_dir)
    return _by_cwd(candidates, query)


def _by_name(roster: Roster, name: str, harness: Harness) -> AgentSpec:
    spec = roster.get(name)
    if spec.harness is not harness:
        raise IdentityError(
            "harness_mismatch", f"{spec.name!r} is a {spec.harness} agent, not {harness}"
        )
    log.info("identity %s resolved by name", spec.name)
    return spec


def _by_config_dir(candidates: list[AgentSpec], config_dir: Path | None) -> list[AgentSpec]:
    if config_dir is None:
        raise IdentityError(
            "identity_unresolved", "a claude session without a config dir matches no agent"
        )
    # Symlinked config dirs are common (~/.claude -> dotfiles); roster paths are already real.
    real = _real(config_dir)
    return [spec for spec in candidates if spec.claude_config_dir == real]


def _by_cwd(candidates: list[AgentSpec], query: IdentityQuery) -> AgentSpec:
    # An agent without a cwd is a zero-length prefix: it matches anywhere but loses to any match.
    # The roster resolves its cwds at load, so only the session side needs resolving here.
    real = _real(query.cwd)
    scored: list[tuple[int, AgentSpec]] = []
    for spec in candidates:
        if spec.cwd is None:
            scored.append((0, spec))
        elif real.is_relative_to(spec.cwd):
            scored.append((len(spec.cwd.parts), spec))
    if not scored:
        where = f" with config dir {query.config_dir}" if query.config_dir else ""
        raise IdentityError(
            "identity_unresolved",
            f"no {query.harness} agent{where} matches cwd {query.cwd}",
        )
    best = max(score for score, _ in scored)
    winners = sorted(spec.name for score, spec in scored if score == best)
    if len(winners) > 1:
        log.info("identity ambiguous in %s: %s", query.cwd, ", ".join(winners))
        raise IdentityError(
            "identity_ambiguous",
            f"cwd {query.cwd} matches {', '.join(winners)} equally; "
            "set MZ_RELAY_NAME or give each a distinct cwd",
        )
    spec = next(spec for score, spec in scored if score == best)
    log.info("identity %s resolved from cwd %s", spec.name, query.cwd)
    return spec


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except (RuntimeError, OSError) as exc:
        raise IdentityError("identity_unresolved", f"cannot resolve {path}: {exc}") from exc
