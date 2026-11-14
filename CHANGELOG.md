# Changelog

All notable changes are listed here. The format follows Keep a Changelog, and versions follow semver.

## [Unreleased]

### Changed

- README: the OpenCode `config` hook, the agent markers of `approve`, `deny` and `context extend`, and the `codex queue` stderr caveat are now marked as tested; the UNTESTED section lists the two claims still open.

## [0.1.1] - 2026-11-14

### Added

- `mz-relay approve|deny <id> --source NAME` picks one of several held messages that share an id; without it, `approve` names the other senders.
- `mz-relay doctor` reports a state or runtime directory owned by another uid as an error; relayd refuses such a directory.
- `mz-relay doctor` warns, for a `read_only` Codex agent, about each `<CODEX_HOME>/<name>.config.toml` profile whose `sandbox_mode` is not `read-only`; `codex --profile <name>` layers it over `config.toml`.

### Fixed

- The MCP server registers again on each tool call after a failed start (no roster yet, relayd down, unknown name) instead of answering `unavailable` until its process ends; the first answer after a late registration carries the session brief.
- A Claude session's first peer message is no longer dropped: Claude 2.1.325 writes `sessions/<pid>.json` about half a second before it adds `status`, and such an entry now reads as busy instead of invalid.
- `mz-relay snippet codex` adds `env_vars` for `MZ_RELAY_HOME`, `MZ_RELAY_PYTHON` and the XDG variables, which Codex otherwise drops from the MCP server's environment.
- The demo answers Codex's startup pickers (update offer, daemon choice, hook review) with the key that changes no setting, instead of typing `/rename` into them, and starts Codex with `--disable daemon_auto_start` so the demo's `MZ_RELAY_HOME` reaches its MCP server.
- A heartbeat on an unchanged registration no longer re-arms a pull-only message for one extra delivery pass.
- The OpenCode adapter nacks a failed `promptAsync` with the error name only, so error data that echoes the request cannot reach relayd.log.
- The Pi adapter closes a `session_shutdown` that lands while its socket is still opening, instead of leaving the socket and presence running, and counts its 1 MiB line limit in bytes.
- `mz-relay held`, `approve`, `deny` and `log` show line separators (U+2028, U+2029) and bidi controls in peer text as U+FFFD, like other control characters.
- `mz-relay log --context ""` is a usage error (exit 2) instead of a relayd error (exit 1).
- `mz-relay install opencode` installs under `$XDG_CONFIG_HOME/opencode/` when that is set, where OpenCode looks for it.

### Changed

- README: a Codex started with `-c` is reachable after all. relayd queues by thread id, which reaches the thread before its first turn and with `-c` (tested on Codex 0.169.0); only a hand-typed `codex queue --thread <name>` needs a thread that has had a turn.
- Tested versions: Claude 2.1.325, Codex 0.169.0. Every live test and the demo passed against them, with Pi 1.5.2 and OpenCode 1.19.5.
- OpenCode adapter log lines carry `mz-relay-oc[<pid>]` and a level, like the Pi adapter's; `MZ_RELAY_RECONNECT_MS` is read with `Number`, as Pi reads it.
- Store schema 2 adds an index on `messages(context_id, created_at)` for `log --context`; a schema 1 database is migrated when relayd opens it.

## [0.1.0] - 2026-11-14

### Added

- Relay core and pull messaging: `relayd` behind an owner-only Unix socket with a SQLite WAL store, the TOML roster, message kinds `request`, `inform`, `reply` and `done`, per-agent routes (`may_send_to`), reply chains with a hop budget, the stdio MCP server with the `send`, `inbox`, `ack` and `peers` tools, and the `version`, `daemon`, `stop`, `roster check`, `status`, `peers`, `held`, `approve` and `deny` commands. Corporate to personal messages are held until the user approves them in a terminal.
- Claude adapter, waiter and gate: delivery to the session's inbox socket confirmed through the transcript, an `asyncRewake` waiter as fallback, and a `PreToolUse` gate for `tools = "read_only"` agents. The plugin ships `.mcp.json`, `hooks/hooks.json` and the `relay_name` option.
- Pi and OpenCode adapters: a Pi extension and an OpenCode plugin that receive messages on a per-process socket, `mz-relay install pi|opencode` and `mz-relay snippet opencode --name N`. Read-only is enforced by a tool allowlist in Pi and the `mz-relay-peer` agent in OpenCode.
- Codex adapter: delivery with `codex queue --thread` confirmed through the rollout file, thread lookup by `/rename` name, and `mz-relay snippet codex --name N`.
- Guards, TTL expiry and notices: `turn_cap`, `inbox_cap`, `hop_exceeded` and `context_closed` refusals, `done` closing a context, `mz-relay context extend <ctx> [--by N]`, expiry of undelivered messages after `default_ttl_s`, and notices to the sender about messages that ended undeliverable, expired or denied.
- `mz-relay log [--context C] [--limit N]`: recent messages with route, kind, status, reason and preview.
- `mz-relay doctor`: checks paths, roster, relayd, Claude config dirs, Codex threads and sandbox, harness versions against the tested ones, and the recorded install.
- Demo: `plugins/mz-relay/scripts/demo.py` runs an architect, a builder and a reviewer on two Claude accounts and Codex, and `make live-demo` checks it against the real harnesses.
- Root README with the architecture, per-harness install, roster, guards, read-only table, threat model and the list of untested behaviour.
