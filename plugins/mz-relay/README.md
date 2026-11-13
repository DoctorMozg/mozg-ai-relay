# mz-relay

Agents in Claude Code (one or more accounts), Codex, Pi and OpenCode send each other messages by roster name. A local broker, `relayd`, holds the roster, queues the messages and wakes the receiving agent where its harness allows it.

## Status

Version 0.1.0, under construction. Only the scaffold is in place: the plugin manifest, the `bin/mz-relay` launcher and its `version` command. Messaging, the MCP server, hooks and the Pi, OpenCode and Codex adapters are not built yet.

| Part                                    | State     |
| --------------------------------------- | --------- |
| Manifest, marketplace entry             | in place  |
| `bin/mz-relay`, `mz-relay version`      | working   |
| `relayd`, roster, MCP server            | not built |
| Claude, Codex, Pi and OpenCode adapters | not built |

## Requirements

- Linux.
- Python 3.12 or newer, standard library only. The launcher tries `$MZ_RELAY_PYTHON`, then `python3.14`, `python3.13`, `python3.12` and `python3` on `PATH`, and uses the first one that is 3.12 or newer.

## Install

```text
/plugin install mz-relay --marketplace DoctorMozg/mozg-ai-relay
```

## Usage

```bash
plugins/mz-relay/bin/mz-relay version
```

A missing or unknown command prints the usage and exits with status 2. No Python 3.12 or newer on `PATH` also exits with status 2, with a hint on stderr.

## License

MIT.
