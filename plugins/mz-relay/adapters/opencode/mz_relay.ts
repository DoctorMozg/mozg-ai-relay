// mz-relay adapter for OpenCode
// Mirrors the subset of @opencode-ai/plugin PluginInput/Hooks used here (OpenCode 1.19.5 runtime; typings 1.4.3 on this machine).
import { Buffer } from "node:buffer";
import { spawn } from "node:child_process";
import { appendFileSync, chmodSync, mkdirSync, readFileSync, statSync, unlinkSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import { isAbsolute, join } from "node:path";

interface PromptBody {
  parts: { type: "text"; text: string }[];
  agent?: string;
}
interface OcClient {
  session: {
    promptAsync(opts: { path: { id: string }; body: PromptBody }): Promise<{ error?: unknown; response?: { status: number } }>;
  };
}
interface OcInput {
  client: OcClient;
  directory: string;
}
interface OcConfig {
  agent?: Record<string, unknown>;
  mcp?: Record<string, unknown>;
}
interface OcEvent {
  type: string;
  properties?: Record<string, unknown>;
}
interface OcHooks {
  config(c: OcConfig): Promise<void>;
  event(i: { event: OcEvent }): Promise<void>;
  "chat.message"(i: { sessionID: string; agent?: string }, o: unknown): Promise<void>;
  dispose(): Promise<void>;
}

type Frame = Record<string, unknown>;

const APP = "mz-relay";
const PEER_AGENT_NAME = "mz-relay-peer";
const NO_SESSION = "no active OpenCode session yet";
const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];
const PRIVATE_MODE = 0o700;
// sun_path is 108 bytes including the trailing NUL.
const SOCKET_PATH_LIMIT = 108;

interface RelayPaths {
  run: string;
  config: string;
}

function xdgDir(variable: string, fallback: string): string {
  // The XDG spec says relative values are invalid and must be ignored.
  const value = process.env[variable];
  return join(value && isAbsolute(value) ? value : fallback, APP);
}

function runtimeDir(uid: number): string {
  const value = process.env.XDG_RUNTIME_DIR;
  if (value && isAbsolute(value)) return join(value, APP);
  // Harnesses may strip XDG_RUNTIME_DIR from children; /run/user keeps them on one socket.
  const runUser = `/run/user/${uid}`;
  try {
    if (statSync(runUser).isDirectory()) return join(runUser, APP);
  } catch {
    // No /run/user/<uid>: fall through to /tmp, as relayd does.
  }
  return `/tmp/${APP}-${uid}`;
}

function currentUid(): number {
  if (!process.getuid) throw new Error("this platform has no uid; Unix sockets are required");
  return process.getuid();
}

// S1 paths; the passwd home, never $HOME, because a second Claude install runs with its own HOME.
function relayPaths(): RelayPaths {
  const home = process.env.MZ_RELAY_HOME;
  if (home) {
    // relayd and the MCP child refuse a relative home, so falling back would split this agent across two relayds.
    if (!isAbsolute(home)) throw new Error(`MZ_RELAY_HOME must be absolute, got '${home}'`);
    return { run: join(home, "run"), config: home };
  }
  const pwHome = os.userInfo().homedir;
  return { run: runtimeDir(currentUid()), config: xdgDir("XDG_CONFIG_HOME", join(pwHome, ".config")) };
}

// As paths.py: our own run dir is tightened to 0700; one owned by another uid could hold a planted relayd.sock.
function ownRunDir(run: string, log: AdapterLog): void {
  mkdirSync(run, { recursive: true, mode: PRIVATE_MODE });
  const info = statSync(run);
  const uid = currentUid();
  if (info.uid !== uid) throw new Error(`runtime dir ${run} is owned by uid ${info.uid}, not ${uid}`);
  const mode = info.mode & 0o777;
  if (mode !== PRIVATE_MODE) {
    chmodSync(run, PRIVATE_MODE);
    log.info(`tightened ${run} from ${mode.toString(8)} to ${PRIVATE_MODE.toString(8)}`);
  }
}

class AdapterLog {
  path: string;

  constructor(path: string) {
    this.path = path;
  }

  info(message: string): void {
    this.write("INFO", message);
  }

  warn(message: string): void {
    this.write("WARNING", message);
  }

  error(message: string): void {
    this.write("ERROR", message);
  }

  private write(level: string, message: string): void {
    const line = `${new Date().toISOString()} mz-relay-oc[${process.pid}] ${level} ${message}\n`;
    try {
      appendFileSync(this.path, line, { mode: 0o600 });
    } catch {
      // The log is the only error channel; the plugin must not throw into OpenCode when it fails.
    }
  }
}

function errorCode(err: unknown): string {
  if (err && typeof err === "object" && "code" in err && typeof err.code === "string") return err.code;
  return err instanceof Error ? err.name : "unknown";
}

// Only the name: an error's data may echo the request, and nack texts reach relayd.log.
function errorName(value: unknown): string {
  if (value && typeof value === "object" && "name" in value && typeof value.name === "string") return value.name;
  return "unknown";
}

function installedBin(config: string, log: AdapterLog): string | null {
  const path = join(config, "install.json");
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return null;
  }
  try {
    const record: unknown = JSON.parse(raw);
    if (record && typeof record === "object" && "bin" in record && typeof record.bin === "string" && record.bin) {
      return record.bin;
    }
  } catch {
    log.warn(`install record ${path} is not valid JSON`);
  }
  return null;
}

function retryDelay(attempt: number): number {
  const override = Number(process.env.MZ_RELAY_RECONNECT_MS ?? "");
  if (override > 0) return override;
  return BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] as number;
}

// Reads newline-delimited JSON from a socket; a line that is not a JSON object yields null.
function onLines(socket: net.Socket, handle: (frame: Frame | null) => void): void {
  let buffer = "";
  socket.setEncoding("utf8");
  socket.on("data", (chunk: string) => {
    buffer += chunk;
    let end = buffer.indexOf("\n");
    while (end >= 0) {
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(line);
      } catch {
        parsed = null;
      }
      handle(parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Frame) : null);
      end = buffer.indexOf("\n");
    }
  });
}

interface PresenceParams {
  paths: RelayPaths;
  log: AdapterLog;
  cwd: string;
}

// The registration connection to relayd, kept open so relayd knows this OpenCode is online.
class Presence {
  params: PresenceParams;
  socket: net.Socket | null = null;
  timer: ReturnType<typeof setTimeout> | null = null;
  started = false;
  closed = false;
  attempt = 0;
  spawnTried = false;
  outageLogged = false;
  refusalsLogged: Set<string> = new Set();
  // Unset while the adapter socket can't listen: relayd would find it unreachable and mark us offline.
  delivery: Frame | null = null;

  constructor(params: PresenceParams) {
    this.params = params;
  }

  start(): void {
    if (this.started || this.closed) return;
    this.started = true;
    this.params.log.info("first top-level chat; registering with relayd");
    this.dial();
  }

  close(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.socket?.destroy();
    this.socket = null;
  }

  dial(): void {
    if (this.closed) return;
    const { paths, log } = this.params;
    const socket = net.createConnection(join(paths.run, "relayd.sock"));
    this.socket = socket;
    let connected = false;
    let unreachable = false;
    socket.on("connect", () => {
      connected = true;
      this.outageLogged = false;
      this.send(socket, 1, "hello", { protocol: 1, client: "opencode", pid: process.pid });
    });
    onLines(socket, (frame) => this.onReply(socket, frame));
    socket.on("error", (err) => {
      const code = errorCode(err);
      // Only a failed connect means no relayd is listening, the one case a spawn can fix.
      unreachable = !connected && (code === "ENOENT" || code === "ECONNREFUSED");
      // Retries run every few seconds while relayd is down; one line per outage is enough.
      if (!this.outageLogged) log.warn(`relayd connection error: ${code}`);
      this.outageLogged = true;
    });
    socket.on("close", () => {
      if (this.socket === socket) this.socket = null;
      this.scheduleRetry(unreachable);
    });
  }

  send(socket: net.Socket, rid: number, op: string, args: Frame): void {
    socket.write(`${JSON.stringify({ v: 1, rid, op, args })}\n`);
  }

  onReply(socket: net.Socket, frame: Frame | null): void {
    const { log } = this.params;
    if (!frame) {
      log.warn("malformed line from relayd ignored");
      return;
    }
    if (frame.ok !== true) {
      const error = (frame.error ?? {}) as Frame;
      const code = String(error.code);
      // A roster mistake is refused on every retry; one line per code until a register succeeds.
      if (!this.refusalsLogged.has(code)) {
        this.refusalsLogged.add(code);
        log.warn(`relayd refused request ${String(frame.rid)}: ${code}: ${String(error.message)}`);
      }
      socket.destroy();
      return;
    }
    if (frame.rid === 1) {
      const args: Frame = {
        name: process.env.MZ_RELAY_NAME || null,
        harness: "opencode",
        cwd: this.params.cwd,
        pid: process.pid,
      };
      if (this.delivery) args.delivery = this.delivery;
      else log.warn("adapter socket is not listening; registering pull-only");
      this.send(socket, 2, "register", args);
    } else if (frame.rid === 2) {
      // The token is not kept: OpenCode reaches relayd's tools through its own MCP server.
      const agent = ((frame.result as Frame | undefined)?.agent ?? {}) as Frame;
      this.attempt = 0;
      this.spawnTried = false;
      this.refusalsLogged.clear();
      log.info(`registered as ${String(agent.name)}`);
    }
  }

  // One spawn per outage: spawnTried is reset by the next successful register.
  scheduleRetry(unreachable: boolean): void {
    if (this.closed || this.timer) return;
    if (unreachable && !this.spawnTried) {
      this.spawnTried = true;
      this.spawnDaemon();
    }
    const delay = retryDelay(this.attempt);
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.dial();
    }, delay);
    this.timer.unref();
  }

  // Only install.json's bin is trusted; an mz-relay found on PATH could be anyone's.
  spawnDaemon(): void {
    const { paths, log } = this.params;
    const bin = installedBin(paths.config, log);
    if (!bin) {
      log.warn("relayd unreachable and install.json has no bin; not spawning");
      return;
    }
    log.info(`relayd unreachable; spawning ${bin} daemon --ensure`);
    try {
      const child = spawn(bin, ["daemon", "--ensure"], { detached: true, stdio: "ignore" });
      child.on("error", (err) => log.error(`spawn of ${bin} failed: ${errorCode(err)}`));
      child.unref();
    } catch (err) {
      log.error(`spawn of ${bin} failed: ${errorCode(err)}`);
    }
  }
}

class OcAdapter {
  client: OcClient;
  paths: RelayPaths;
  socketPath: string;
  log: AdapterLog;
  presence: Presence;
  server: net.Server | null = null;
  connections: Set<net.Socket> = new Set();
  listening = false;
  childSessions: Set<string> = new Set();
  lastSession: string | null = null;

  constructor(input: OcInput, paths: RelayPaths, log: AdapterLog) {
    this.client = input.client;
    this.paths = paths;
    this.log = log;
    this.socketPath = join(paths.run, `oc-${process.pid}.sock`);
    const cwd = typeof input.directory === "string" ? input.directory : process.cwd();
    this.presence = new Presence({ paths, log, cwd });
  }

  async listen(): Promise<void> {
    const size = Buffer.byteLength(this.socketPath);
    if (size >= SOCKET_PATH_LIMIT) {
      this.log.error(`listen error on ${this.socketPath}: ${size} bytes, Unix sockets need fewer than ${SOCKET_PATH_LIMIT}`);
      return;
    }
    try {
      // The socket is keyed on our pid, so a file already there is a leftover from a killed process.
      unlinkSync(this.socketPath);
    } catch (err) {
      if (errorCode(err) !== "ENOENT") this.log.warn(`cannot remove stale ${this.socketPath}: ${errorCode(err)}`);
    }
    const server = net.createServer((socket) => this.accept(socket));
    this.server = server;
    await new Promise<void>((resolve) => {
      // Kept for the server's whole life: an 'error' with no listener would crash OpenCode.
      server.on("error", (err) => {
        if (this.listening) {
          this.log.warn(`adapter server error: ${errorCode(err)}`);
          return;
        }
        this.log.error(`listen error on ${this.socketPath}: ${errorCode(err)}`);
        resolve();
      });
      server.listen(this.socketPath, () => {
        this.listening = true;
        resolve();
      });
    });
    if (this.listening) this.presence.delivery = { socket: this.socketPath, pid: process.pid };
  }

  accept(socket: net.Socket): void {
    this.connections.add(socket);
    socket.on("close", () => this.connections.delete(socket));
    socket.on("error", (err) => this.log.warn(`adapter connection error: ${errorCode(err)}`));
    onLines(socket, (frame) => {
      if (!frame) {
        this.log.warn("malformed adapter frame ignored");
        return;
      }
      void this.answer(frame).then((reply) => {
        if (!socket.destroyed) socket.write(`${JSON.stringify(reply)}\n`);
      });
    });
  }

  async answer(frame: Frame): Promise<Frame> {
    const id = typeof frame.id === "string" ? frame.id : "";
    if (frame.op !== "deliver") return { nack: id, error: "unknown op" };
    const gate = frame.gate;
    if (!id || typeof frame.text !== "string" || (gate !== "full" && gate !== "read_only")) {
      this.log.warn(`deliver ${id || "<no id>"} nacked: invalid frame`);
      return { nack: id, error: "invalid deliver frame" };
    }
    return this.deliver(id, frame.text, gate);
  }

  async deliver(id: string, text: string, gate: "full" | "read_only"): Promise<Frame> {
    const session = this.lastSession;
    if (!session) return { nack: id, error: NO_SESSION };
    // Never a per-message tools map: OpenCode keeps it on the session after the prompt (P7d).
    const body: PromptBody = { parts: [{ type: "text", text }] };
    if (gate === "read_only") body.agent = PEER_AGENT_NAME;
    let result: { error?: unknown; response?: { status: number } };
    try {
      result = await this.client.session.promptAsync({ path: { id: session }, body });
    } catch (err) {
      this.log.warn(`deliver ${id} nacked: promptAsync rejected (${errorCode(err)})`);
      return { nack: id, error: `promptAsync rejected: ${errorCode(err)}` };
    }
    if (result?.error !== undefined) {
      this.log.warn(`deliver ${id} nacked: promptAsync returned an error`);
      return { nack: id, error: `promptAsync error: ${errorName(result.error)}` };
    }
    const status = result?.response?.status;
    if (typeof status === "number" && status >= 300) {
      this.log.warn(`deliver ${id} nacked: promptAsync status ${status}`);
      return { nack: id, error: `promptAsync answered status ${status}` };
    }
    this.log.info(`delivered ${id} to session ${session}`);
    return { ack: id, state: "delivered" };
  }

  configure(c: OcConfig): void {
    c.agent ??= {};
    if (c.agent[PEER_AGENT_NAME] === undefined) {
      c.agent[PEER_AGENT_NAME] = {
        description: "Answers mz-relay peer messages, read-only",
        mode: "primary",
        permission: { edit: "deny", bash: "deny", webfetch: "deny", task: "deny" },
      };
    }
    if (c.mcp?.[APP] !== undefined) return;
    const bin = installedBin(this.paths.config, this.log);
    if (!bin) {
      this.log.warn("install.json has no bin; no mz-relay MCP entry added");
      return;
    }
    const name = process.env.MZ_RELAY_NAME;
    c.mcp ??= {};
    c.mcp[APP] = { type: "local", command: [bin, "mcp", "--harness", "opencode"], environment: name ? { MZ_RELAY_NAME: name } : {} };
  }

  // A session with a parentID is a subagent's; peer messages go to the top-level session that spawned it.
  onEvent(event: OcEvent): void {
    if (event.type !== "session.created" && event.type !== "session.updated") return;
    const info = event.properties?.info as Frame | undefined;
    if (!info || typeof info.id !== "string" || typeof info.parentID !== "string" || !info.parentID) return;
    if (!this.childSessions.has(info.id)) this.log.info(`session ${info.id} is a child of ${info.parentID}`);
    this.childSessions.add(info.id);
    if (this.lastSession === info.id) this.lastSession = info.parentID;
  }

  onChat(sessionID: string): void {
    if (typeof sessionID !== "string" || !sessionID || this.childSessions.has(sessionID)) return;
    this.lastSession = sessionID;
    this.presence.start();
  }

  async dispose(): Promise<void> {
    this.presence.close();
    const server = this.server;
    this.server = null;
    if (!server) return;
    for (const socket of this.connections) socket.destroy();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    if (!this.listening) return;
    try {
      unlinkSync(this.socketPath);
    } catch (err) {
      if (errorCode(err) !== "ENOENT") this.log.warn(`cannot remove ${this.socketPath}: ${errorCode(err)}`);
    }
  }

  // OpenCode must never see a throw from a hook, so every hook ends here on failure.
  async guard(hook: string, run: () => void | Promise<void>): Promise<void> {
    try {
      await run();
    } catch (err) {
      this.log.error(`${hook} hook failed: ${errorCode(err)}`);
    }
  }
}

function disabledHooks(): OcHooks {
  const idle = async (): Promise<void> => {};
  return { config: idle, event: idle, "chat.message": idle, dispose: idle };
}

export const MzRelayPlugin = async (input: OcInput): Promise<OcHooks> => {
  let adapter: OcAdapter;
  try {
    const paths = relayPaths();
    const log = new AdapterLog(join(paths.run, `oc-${process.pid}.log`));
    ownRunDir(paths.run, log);
    adapter = new OcAdapter(input, paths, log);
  } catch (err) {
    // No trusted run dir means no log file either; stderr is the only channel left.
    console.error(`mz-relay: OpenCode adapter disabled: ${err instanceof Error ? err.message : String(err)}`);
    return disabledHooks();
  }
  await adapter.listen();
  return {
    config: (c) => adapter.guard("config", () => adapter.configure(c)),
    event: (i) => adapter.guard("event", () => adapter.onEvent(i.event)),
    "chat.message": (i) => adapter.guard("chat.message", () => adapter.onChat(i.sessionID)),
    dispose: () => adapter.guard("dispose", () => adapter.dispose()),
  };
};
