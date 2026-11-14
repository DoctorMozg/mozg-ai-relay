"""The mz-relay command line: one parser with a subcommand per tool."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass

from mz_relay import __version__
from mz_relay.client import RelayClient
from mz_relay.clock import SystemClock
from mz_relay.doctor import Doctor, Finding
from mz_relay.envelope import Status
from mz_relay.errors import (
    DaemonUnavailable,
    PathError,
    ProtocolError,
    ProtocolMismatch,
    RelayError,
)
from mz_relay.hooks import run_hook
from mz_relay.install import Installer, codex_snippet, opencode_snippet, plugin_bin
from mz_relay.launcher import PLUGIN_DIR, DaemonLauncher
from mz_relay.paths import Paths, user_home
from mz_relay.procs import run_in_group
from mz_relay.relayd import DEFAULT_IDLE_EXIT_S, run_daemon
from mz_relay.roster import Harness, load_roster
from mz_relay.service import CONTEXT_EXTEND_MAX, LOG_LIMIT_DEFAULT, LOG_LIMIT_MAX
from mz_relay.tool_backend import MCP_HARNESSES, run_mcp

log = logging.getLogger(__name__)

PROG = "mz-relay"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_RUNNING = 3
AGENT_ENV_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CODEX_THREAD_ID",
    "OPENCODE",
    "PI_CODING_AGENT",
)
REFUSAL = "approve, deny and context extend must be run by the user in a terminal, not by an agent"
LIST_PREVIEW_CHARS = 60
CONTEXT_EXTEND_BY = 20
YES = frozenset({"y", "yes"})
# C0 (but newline), DEL, C1, line separators and bidi controls could move the cursor or
# reorder the route line; U+FFFD shows them.
UNSAFE_CODES = (
    *range(0x20),
    0x7F,
    *range(0x80, 0xA0),
    0x061C,
    0x200E,
    0x200F,
    *range(0x2028, 0x202F),
    *range(0x2066, 0x206A),
)
UNSAFE_CHARS = {code: "\ufffd" for code in UNSAFE_CODES if code != 0x0A}


@dataclass(frozen=True)
class Terminal:
    """Where approve and deny ask the user: a tty check, a prompt reader and the environment."""

    isatty: Callable[[], bool]
    ask: Callable[[str], str]
    env: Mapping[str, str]


@dataclass(frozen=True)
class Decision:
    """The user's approve or deny command for one held message."""

    command: str
    msg_id: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.command not in ("approve", "deny"):
            raise ValueError(f"command must be approve or deny, got {self.command!r}")


def _ask_on_tty(prompt: str) -> str:
    # The prompt goes to /dev/tty so piped stdin or stdout can't answer it.
    with open("/dev/tty", "r+", encoding="utf-8") as tty:
        tty.write(prompt)
        tty.flush()
        return tty.readline()


def user_terminal() -> Terminal:
    return Terminal(isatty=sys.stdin.isatty, ask=_ask_on_tty, env=os.environ)


def _positive_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected seconds, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {raw!r}")
    return value


def _extend_by(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if not 1 <= value <= CONTEXT_EXTEND_MAX:
        raise argparse.ArgumentTypeError(f"must be in 1..{CONTEXT_EXTEND_MAX}, got {raw!r}")
    return value


def _log_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if not 1 <= value <= LOG_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"must be in 1..{LOG_LIMIT_MAX}, got {raw!r}")
    return value


def _context_id(raw: str) -> str:
    if not raw:
        raise argparse.ArgumentTypeError("must be a non-empty context id")
    return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Message agents in other harnesses by roster name."
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    commands.add_parser("version", help="print the plugin version")
    daemon = commands.add_parser("daemon", help="run relayd in the foreground")
    daemon.add_argument(
        "--ensure", action="store_true", help="start relayd in the background unless it serves"
    )
    daemon.add_argument(
        "--idle-exit-s",
        type=_positive_seconds,
        default=None,
        metavar="N",
        help=f"exit after N seconds without connections (default {DEFAULT_IDLE_EXIT_S:g})",
    )
    commands.add_parser("stop", help="shut down the running relayd")
    mcp = commands.add_parser("mcp", help="serve the relay tools to a harness over stdio")
    mcp.add_argument(
        "--harness", required=True, choices=[harness.value for harness in MCP_HARNESSES]
    )
    hook = commands.add_parser(
        "hook", help="run a Claude Code hook: wait <SessionStart|Stop> | gate"
    )
    hook.add_argument("hook_args", nargs=argparse.REMAINDER, metavar="...")
    roster = commands.add_parser("roster", help="work with the roster file")
    roster_commands = roster.add_subparsers(
        dest="roster_command", metavar="<action>", required=True
    )
    check = roster_commands.add_parser("check", help="validate a roster file")
    check.add_argument("path", nargs="?", default=None, help="roster file (default: configured)")
    commands.add_parser("status", help="show the running relayd, its agents and message counts")
    commands.add_parser("peers", help="list the roster agents and whether they are online")
    commands.add_parser("held", help="list messages held for your approval")
    log_cmd = commands.add_parser("log", help="list recent messages, oldest first")
    log_cmd.add_argument(
        "--context", type=_context_id, default=None, metavar="C", help="only this context's rows"
    )
    log_cmd.add_argument(
        "--limit",
        type=_log_limit,
        default=LOG_LIMIT_DEFAULT,
        metavar="N",
        help=f"newest N messages (default {LOG_LIMIT_DEFAULT})",
    )
    approve = commands.add_parser("approve", help="release a held message after confirming")
    approve.add_argument("id")
    deny = commands.add_parser("deny", help="refuse a held message after confirming")
    deny.add_argument("id")
    deny.add_argument("--reason", default=None, metavar="T", help="reason shown to the sender")
    context = commands.add_parser("context", help="work with a message context")
    context_commands = context.add_subparsers(
        dest="context_command", metavar="<action>", required=True
    )
    extend = context_commands.add_parser("extend", help="raise a context's turn cap")
    extend.add_argument("context_id", metavar="ctx")
    extend.add_argument(
        "--by",
        type=_extend_by,
        default=CONTEXT_EXTEND_BY,
        metavar="N",
        help=f"turns to add (default {CONTEXT_EXTEND_BY})",
    )
    install = commands.add_parser("install", help="install the Pi or OpenCode adapter")
    install.add_argument("kind", choices=["pi", "opencode"])
    install.add_argument("--dest", default=None, metavar="D", help="file to install to")
    install.add_argument(
        "--force", action="store_true", help="overwrite a file that is not an mz-relay adapter"
    )
    snippet = commands.add_parser("snippet", help="print harness config entries for mz-relay")
    snippet.add_argument("kind", choices=["opencode", "codex"])
    snippet.add_argument("--name", required=True, metavar="N", help="the agent's roster name")
    commands.add_parser("doctor", help="check the setup without starting relayd")
    return parser


def main(argv: Sequence[str] | None = None, *, terminal: Terminal | None = None) -> int:
    """Runs one command; argparse exits with 2 and the usage on a missing or unknown command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        sys.stdout.write(f"{PROG} {__version__}\n")
        return EXIT_OK
    if args.command == "daemon" and args.ensure and args.idle_exit_s is not None:
        parser.error("--idle-exit-s applies to a foreground daemon, not to --ensure")
    if args.command == "mcp":
        # Path errors become an unavailable server there, so the harness still gets a reply.
        return run_mcp(Harness(args.harness), os.environ)
    if args.command == "hook":
        # Hooks are advisory: run_hook logs its own failures and exits 0.
        return run_hook(args.hook_args, sys.stdin, sys.stdout, sys.stderr, os.environ)
    if args.command == "snippet":
        return _snippet(args.kind, args.name)
    try:
        paths = Paths.from_env(os.environ, uid=os.getuid(), pw_home=user_home())
    except PathError as exc:
        if args.command == "doctor":
            return _report_findings([Finding("error", "paths", exc.message)])
        return _fail(exc, EXIT_USAGE)
    if args.command == "daemon":
        if args.ensure:
            return _ensure(paths)
        idle_exit_s = DEFAULT_IDLE_EXIT_S if args.idle_exit_s is None else args.idle_exit_s
        return run_daemon(paths, idle_exit_s)
    if args.command == "stop":
        return _stop(paths)
    if args.command == "install":
        return _install(paths, args.kind, args.dest, force=args.force)
    if args.command == "doctor":
        return _report_findings(Doctor(paths, run=run_in_group).check())
    user = UserCommands(paths)
    if args.command == "roster":
        return user.roster_check(args.path)
    if args.command == "status":
        return user.status()
    if args.command == "peers":
        return user.peers()
    if args.command == "held":
        return user.held()
    if args.command == "log":
        return user.log(args.context, args.limit)
    if args.command in ("approve", "deny"):
        reason = args.reason if args.command == "deny" else None
        return user.decide(Decision(args.command, args.id, reason), terminal or user_terminal())
    if args.command == "context":
        return user.context_extend(args.context_id, args.by, terminal or user_terminal())
    parser.error(f"unhandled command {args.command!r}")


def _ensure(paths: Paths) -> int:
    try:
        DaemonLauncher(paths, clock=SystemClock()).connect(client="cli").close()
    except RelayError as exc:
        return _fail(exc, EXIT_ERROR)
    return EXIT_OK


def _install(paths: Paths, kind: str, dest: str | None, *, force: bool) -> int:
    installer = Installer(paths, plugin_root=PLUGIN_DIR, env=os.environ)
    install = installer.pi if kind == "pi" else installer.opencode
    try:
        target = None if dest is None else paths.expand(dest).absolute()
        plan = install(target, force=force)
    except RelayError as exc:
        return _fail(exc, EXIT_USAGE)
    sys.stdout.write(f"installed {plan.dest}\n")
    return EXIT_OK


def _report_findings(findings: Sequence[Finding]) -> int:
    for finding in findings:
        # One line per finding, even when an error message spans several.
        text = _safe_text(" ".join(finding.text.splitlines()))
        sys.stdout.write(f"{finding.level}  {finding.topic}: {text}\n")
    return EXIT_ERROR if any(finding.level == "error" for finding in findings) else EXIT_OK


def _snippet(kind: str, name: str) -> int:
    try:
        if kind == "codex":
            text = codex_snippet(plugin_bin(PLUGIN_DIR), name)
        else:
            text = json.dumps(opencode_snippet(plugin_bin(PLUGIN_DIR), name), indent=2) + "\n"
    except ValueError as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        return EXIT_USAGE
    sys.stdout.write(text)
    return EXIT_OK


def _stop(paths: Paths) -> int:
    client = RelayClient(paths.socket)
    try:
        pid = client.connect(client="cli").get("daemon_pid")
    except DaemonUnavailable:
        return _not_running()
    except ProtocolMismatch as exc:
        # shutdown needs no hello, so a fresh connection still stops a daemon that refused ours.
        log.info(
            "relayd speaks protocol %s; stopping it without hello", exc.data.get("daemon_protocol")
        )
        pid = None
        try:
            client.open()
        except DaemonUnavailable:
            return _not_running()
    except RelayError as exc:
        return _fail(exc, EXIT_ERROR)
    try:
        client.call("shutdown", {})
    except RelayError as exc:
        return _fail(exc, EXIT_ERROR)
    finally:
        client.close()
    sys.stdout.write("stopped relayd\n" if pid is None else f"stopped relayd {pid}\n")
    return EXIT_OK


def _connect_running(paths: Paths) -> tuple[RelayClient, dict[str, object]] | int:
    """A client past hello, or the exit code after telling the user why not; never spawns."""
    client = RelayClient(paths.socket)
    try:
        hello = client.connect(client="cli")
    except DaemonUnavailable:
        return _not_running()
    except RelayError as exc:
        return _fail(exc, EXIT_ERROR)
    return client, hello


def _not_running() -> int:
    sys.stderr.write(f"{PROG}: relayd is not running\n")
    return EXIT_NOT_RUNNING


class UserCommands:
    """The commands a user runs against the roster and the running relayd."""

    def __init__(self, paths: Paths) -> None:
        self._paths = paths

    def roster_check(self, path: str | None) -> int:
        target = self._paths.roster if path is None else self._paths.expand(path)
        try:
            roster = load_roster(target, pw_home=self._paths.pw_home)
        except RelayError as exc:
            return _fail(exc, EXIT_USAGE)
        names = sorted(roster.agents)
        sys.stdout.write(f"ok: {len(names)} agents: {', '.join(names)}\n")
        return EXIT_OK

    def status(self) -> int:
        running = _connect_running(self._paths)
        if isinstance(running, int):
            return running
        with closing(running[0]) as client:
            try:
                result = client.call("status", {})
                agents = _records(result, "agents")
            except RelayError as exc:
                return _fail(exc, EXIT_ERROR)
        lines = [
            f"relayd pid {result.get('pid')}, protocol {result.get('protocol')},"
            f" version {result.get('version')}, {result.get('connections')} connection(s)",
            f"db: {result.get('db')}",
            f"roster: {result.get('roster')}",
            "agents:",
            *(
                f"  {agent.get('name')}  {agent.get('harness')}  {_presence(agent)}"
                for agent in agents
            ),
            f"counts: {_counts_text(result.get('counts'))}",
        ]
        sys.stdout.write("\n".join(lines) + "\n")
        return EXIT_OK

    def peers(self) -> int:
        try:
            with closing(
                DaemonLauncher(self._paths, clock=SystemClock()).connect(client="cli")
            ) as client:
                agents = _records(client.call("peers", {}), "agents")
        except RelayError as exc:
            return _fail(exc, EXIT_ERROR)
        for agent in agents:
            columns = ("name", "harness", "label", "role")
            sys.stdout.write("\t".join([*(str(agent.get(c)) for c in columns), _presence(agent)]))
            sys.stdout.write("\n")
        return EXIT_OK

    def held(self) -> int:
        running = _connect_running(self._paths)
        if isinstance(running, int):
            return running
        with closing(running[0]) as client:
            try:
                messages = _records(client.call("held", {}), "messages")
            except RelayError as exc:
                return _fail(exc, EXIT_ERROR)
        if not messages:
            sys.stdout.write("no held messages\n")
            return EXIT_OK
        for msg in messages:
            preview = _list_preview(str(msg.get("preview", "")))
            sys.stdout.write(f"{msg.get('id')}\t{_route(msg)}\t{msg.get('time')}\t{preview}\n")
        return EXIT_OK

    def log(self, context_id: str | None, limit: int) -> int:
        running = _connect_running(self._paths)
        if isinstance(running, int):
            return running
        args: dict[str, object] = {"limit": limit}
        if context_id is not None:
            args["context_id"] = context_id
        with closing(running[0]) as client:
            try:
                messages = _records(client.call("log", args), "messages")
            except RelayError as exc:
                return _fail(exc, EXIT_ERROR)
        if not messages:
            sys.stdout.write("no messages\n")
            return EXIT_OK
        for msg in messages:
            status = str(msg.get("status"))
            reason = msg.get("reason")
            if reason is not None:
                status += f" ({_one_line(str(reason))})"
            sys.stdout.write(
                f"{msg.get('time')}  {msg.get('id')}  {msg.get('source')} → {msg.get('to')}"
                f"  {msg.get('kind')}  {status}  {_list_preview(str(msg.get('preview', '')))}\n"
            )
        return EXIT_OK

    def context_extend(self, context_id: str, by: int, terminal: Terminal) -> int:
        """Raises a context's turn cap; like approve, it lifts a guard, so agents are refused."""
        if _refused(terminal, f"context extend {context_id}"):
            return EXIT_USAGE
        try:
            with closing(
                DaemonLauncher(self._paths, clock=SystemClock()).connect(client="cli")
            ) as client:
                result = client.call("context_extend", {"context_id": context_id, "by": by})
        except RelayError as exc:
            return _fail(exc, EXIT_ERROR)
        sys.stdout.write(f"context {context_id} turn cap is now {result.get('turn_cap')}\n")
        return EXIT_OK

    def decide(self, decision: Decision, terminal: Terminal) -> int:
        """Shows a held message and applies the user's y/N answer; agents are refused first."""
        if _refused(terminal, f"{decision.command} {decision.msg_id}"):
            return EXIT_USAGE
        running = _connect_running(self._paths)
        if isinstance(running, int):
            return running
        with closing(running[0]) as client:
            try:
                return self._confirm_and_decide(client, decision, terminal)
            except RelayError as exc:
                return _fail(exc, EXIT_ERROR)

    def _confirm_and_decide(
        self, client: RelayClient, decision: Decision, terminal: Terminal
    ) -> int:
        command, msg_id = decision.command, decision.msg_id
        # relayd raises not_found or not_held here, so nothing is decided without a prompt.
        held = _records(client.call("held", {"id": msg_id}), "messages")
        if not held:
            raise ProtocolError("invalid_request", f"relayd's held for {msg_id} listed no message")
        # Ids are unique per sender only; one row is decided per run, so each gets its own prompt.
        msg = held[0]
        if len(held) > 1:
            others = len(held) - 1
            sys.stdout.write(f"{others} more held message(s) share {msg_id}; run again for them\n")
        sys.stdout.write(f"{msg_id}: {_route(msg)}\n{_safe_text(str(msg.get('preview')))}\n")
        sys.stdout.flush()
        verb = "Approve" if command == "approve" else "Deny"
        try:
            answer = terminal.ask(f"{verb}? [y/N] ")
        except OSError as exc:
            sys.stderr.write(f"{PROG}: cannot ask on the terminal: {exc}\n")
            return EXIT_USAGE
        done = "approved" if command == "approve" else "denied"
        if answer.strip().lower() not in YES:
            log.info("user did not confirm %s of %s", command, msg_id)
            sys.stdout.write(f"not {done}\n")
            return EXIT_ERROR
        args: dict[str, object] = {"id": msg_id, "source": msg.get("source"), "decision": command}
        if decision.reason is not None:
            args["reason"] = decision.reason
        client.call("decide", args)
        log.info("user confirmed %s of %s", command, msg_id)
        sys.stdout.write(f"{done} {msg_id}\n")
        return EXIT_OK


def _refused(terminal: Terminal, what: str) -> bool:
    """True, after telling the user why, when an agent or a non-terminal runs a user command."""
    markers = [marker for marker in AGENT_ENV_MARKERS if marker in terminal.env]
    if not markers and terminal.isatty():
        return False
    log.info("%s refused: no tty or agent markers %s", what, markers)
    sys.stderr.write(f"{PROG}: {REFUSAL}\n")
    return True


def _records(result: Mapping[str, object], key: str) -> list[dict[str, object]]:
    raw = result.get(key)
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ProtocolError("invalid_request", f"relayd sent a malformed {key!r} list")
    return raw


def _presence(agent: Mapping[str, object]) -> str:
    return "online" if agent.get("online") else "offline"


def _route(msg: Mapping[str, object]) -> str:
    # A recipient dropped from the roster since the send has no label.
    to_label = msg.get("to_label") or "not in roster"
    return f"{msg.get('source')} ({msg.get('source_label')}) → {msg.get('to')} ({to_label})"


def _safe_text(text: str) -> str:
    """Peer-controlled text with each terminal control char but newline shown as U+FFFD."""
    return text.translate(UNSAFE_CHARS)


def _one_line(text: str) -> str:
    return _safe_text(text.replace("\r\n", " ").replace("\n", " "))


def _list_preview(preview: str) -> str:
    flat = _one_line(preview)
    if len(flat) <= LIST_PREVIEW_CHARS:
        return flat
    return flat[:LIST_PREVIEW_CHARS] + "…"


def _counts_text(raw: object) -> str:
    counts = raw if isinstance(raw, dict) else {}
    parts = [
        f"{status.value}={counts[status.value]}" for status in Status if counts.get(status.value)
    ]
    return " ".join(parts) if parts else "none"


def _fail(exc: RelayError, code: int) -> int:
    sys.stderr.write(f"{PROG}: {exc.code}: {exc.message}\n")
    return code
