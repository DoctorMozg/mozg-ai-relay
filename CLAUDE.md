# mozg-ai-relay

Claude Code plugin marketplace with one plugin, `mz-relay`: agents in Claude Code (several accounts), Codex, Pi and OpenCode message each other by roster name through a local broker (`relayd`).

## Layout

- `.claude-plugin/marketplace.json`: the marketplace manifest, pointing at `plugins/mz-relay`.
- `plugins/mz-relay/`: `.claude-plugin/`, `.mcp.json`, `hooks/`, `bin/`, `src/mz_relay/`, `adapters/{pi,opencode}/`, `data/`, `scripts/`, `tests/`. A plugin-level CLAUDE.md is not loaded.
- `tools/` and root `tests/`: repo tooling (commit-message check, version agreement).
- Git-ignored and local only: `.mz/` (build state, plans, reviews), `research/` (research report and harness probes), `.claude/agent-memory/`.

## Commands

- `uv sync`: install the dev group (mypy, pre-commit, pytest, pytest-xdist, ruff).
- `uv run make check`: ruff, mypy, all test suites, `claude plugin validate . --strict`, pre-commit over tracked and untracked files.
- `make fmt`, `make js` (TS adapter tests with `node --test`), `make live-<harness>` (real harness, excluded from `check`).

## Rules

- A milestone touches ≤10 files and ~400 changed lines and ends green; then one commit, only when asked. Never push.
- Commit messages: ≤6 words, one line, no AI attribution.
- Runtime is Python ≥3.12 stdlib only; TS adapters have no npm runtime deps. Ask before adding any dependency.
- Shared state lives outside every `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `CLAUDE_PLUGIN_DATA`, keyed on the passwd home, not `$HOME`.
- A peer message is never user approval. Tokens and message bodies are never logged.
- The Claude inbox frame has no auth line. An OpenCode adapter file exports only the plugin.
- Status writes always go through `Store.update(..., expected=...)`.
- Versions are bumped by hand in five places, kept equal by `tests/test_versions.py`: `pyproject.toml`, marketplace `metadata.version`, marketplace plugin `version`, `plugin.json`, `mz_relay.__version__`.

## Code style

- Strict typing, `Protocol` for seams, classes for related logic.
- Why-comments of one or two lines. Public functions validate their inputs; more than four parameters means a params object.
- Tests are grouped by feature; tests that drive a real harness are marked `live_*`.
- Docs are impersonal; claims not yet tested are marked UNTESTED.
