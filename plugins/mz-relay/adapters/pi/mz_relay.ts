// mz-relay adapter for Pi
// Mirrors the subset of Pi 1.5.2 ExtensionAPI used here (pi-coding-agent dist/core/extensions/types.d.ts).
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { appendFileSync, chmodSync, mkdirSync, readFileSync, statSync, unlinkSync } from "node:fs";
import net from "node:net";
import { userInfo } from "node:os";
import { isAbsolute, join } from "node:path";

interface CustomMessage {
  role: "custom";
  customType: string;
  content: unknown;
  details?: { mzRelayId?: string; gate?: string };
}

type ContextMessage = { role: string } & Partial<CustomMessage>;

interface ToolResult {
  content: { type: "text"; text: string }[];
  details: Record<string, unknown>;
}

interface PiTool {
  name: string;
  label: string;
  description: string;
  parameters: object;
  execute(toolCallId: string, params: Record<string, unknown>, signal?: AbortSignal): Promise<ToolResult>;
}

interface GateDecision {
  block?: boolean;
  reason?: string;
}

interface AgentStartEvent {
  systemPrompt: string;
  messageType: "user" | "custom";
  message?: CustomMessage;
}

interface PiApi {
  on(event: "session_start" | "session_shutdown" | "agent_settled", handler: (e: unknown) => unknown): unknown;
  // Fired before every LLM call with the non-system messages.
  on(event: "context", handler: (e: { messages: ContextMessage[] }) => undefined): unknown;
  on(event: "before_agent_start", handler: (e: AgentStartEvent) => { systemPrompt?: string } | undefined): unknown;
  on(event: "tool_call", handler: (e: { toolName: string }) => Promise<GateDecision | undefined>): unknown;
  registerTool(tool: PiTool): void;
  sendMessage(
    msg: { customType: string; content: string; display: boolean; details: Record<string, unknown> },
    opts: { triggerTurn: boolean; deliverAs: "followUp" },
  ): void;
}

const APP = "mz-relay";
const CUSTOM_TYPE = "mz-relay";
const PROTOCOL_VERSION = 1;
// sun_path is 108 bytes including the trailing NUL.
const SOCKET_PATH_LIMIT = 108;
const MAX_LINE_BYTES = 1_048_576;

// The limit is in bytes, as relayd counts it; a string's length counts UTF-16 code units.
function overLineLimit(buffer: string): boolean {
  return Buffer.byteLength(buffer) > MAX_LINE_BYTES;
}
const REQUEST_TIMEOUT_MS = 10_000;
const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];
const SEEN_MAX = 1000;
const INBOX_LIMIT_DEFAULT = 20;
const NOT_CONNECTED = "mz-relay error daemon_unavailable: not connected to relayd yet";
const READ_ONLY_ALLOWED = new Set([
  "read",
  "grep",
  "find",
  "ls",
  "mz_relay_send",
  "mz_relay_inbox",
  "mz_relay_ack",
  "mz_relay_peers",
]);
// Messages that never start a run, so the walk back to the latest prompt passes over them.
const NON_PROMPT_ROLES = new Set(["assistant", "toolResult", "compactionSummary", "branchSummary"]);

function peerOnlyReason(tool: string): string {
  return (
    `mz-relay: ${tool} is blocked because a peer message is part of this run and your roster role is read_only. ` +
    "The limit lifts when the run ends or your user types."
  );
}

function errorText(err: unknown): string {
  if (err instanceof RelayCallError) return `${err.code}: ${err.message}`;
  return err instanceof Error ? err.message : String(err);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRelayMessage(message: unknown): message is ContextMessage {
  return isRecord(message) && message.role === "custom" && message.customType === CUSTOM_TYPE;
}

function isReadOnlyPeer(message: unknown): boolean {
  return isRelayMessage(message) && message.details?.gate === "read_only";
}

class RelayPaths {
  readonly run: string;
  readonly config: string;

  constructor(run: string, config: string) {
    this.run = run;
    this.config = config;
  }

  // S1 as in paths.py; the home is the passwd entry because a harness may run with another $HOME.
  static fromEnv(env: Record<string, string | undefined>): RelayPaths {
    const relayHome = env.MZ_RELAY_HOME;
    if (relayHome) {
      if (!isAbsolute(relayHome)) throw new Error(`MZ_RELAY_HOME must be absolute, got ${JSON.stringify(relayHome)}`);
      return new RelayPaths(join(relayHome, "run"), relayHome);
    }
    const configBase = absoluteOrNull(env.XDG_CONFIG_HOME) ?? join(userInfo().homedir, ".config");
    return new RelayPaths(runtimeDir(env), join(configBase, APP));
  }

  get relaydSocket(): string {
    return join(this.run, "relayd.sock");
  }

  get adapterSocket(): string {
    return join(this.run, `pi-${process.pid}.sock`);
  }

  get adapterLog(): string {
    return join(this.run, `pi-${process.pid}.log`);
  }

  get installRecord(): string {
    return join(this.config, "install.json");
  }
}

// The XDG spec says relative values are invalid and must be ignored.
function absoluteOrNull(value: string | undefined): string | null {
  return value && isAbsolute(value) ? value : null;
}

function runtimeDir(env: Record<string, string | undefined>): string {
  const xdg = absoluteOrNull(env.XDG_RUNTIME_DIR);
  if (xdg) return join(xdg, APP);
  const uid = userInfo().uid;
  // Harnesses may strip XDG_RUNTIME_DIR from children; /run/user keeps them on one socket.
  const runUser = `/run/user/${uid}`;
  return isDirectory(runUser) ? join(runUser, APP) : `/tmp/${APP}-${uid}`;
}

function isDirectory(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

// S1 as in paths.py: a run dir of another uid could collect our sends, so it is refused; returns a mode it tightened.
function claimRunDir(run: string): number | null {
  mkdirSync(run, { recursive: true, mode: 0o700 });
  const info = statSync(run);
  const uid = userInfo().uid;
  if (info.uid !== uid) throw new Error(`${run} is owned by uid ${info.uid}, not ${uid}`);
  const mode = info.mode & 0o777;
  if (mode === 0o700) return null;
  chmodSync(run, 0o700);
  return mode;
}

// Never sees message bodies or tokens: callers pass ids, codes and paths only.
class AdapterLog {
  readonly path: string;

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
    const line = `${new Date().toISOString()} mz-relay-pi[${process.pid}] ${level} ${message}\n`;
    try {
      appendFileSync(this.path, line, { mode: 0o600 });
    } catch {
      // The log is the only error channel; failing here must not throw into Pi.
    }
  }
}

class RelayCallError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

interface PendingRequest {
  op: string;
  resolve: (result: Record<string, unknown>) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

// relayd's line JSON (S4) on one connection; replies are matched to requests by rid.
class RelayConnection {
  readonly socket: net.Socket;
  onClose: (() => void) | null = null;
  private buffer = "";
  private nextRid = 1;
  private pending: Map<number, PendingRequest> = new Map();
  private closed = false;

  constructor(socket: net.Socket) {
    this.socket = socket;
    socket.setEncoding("utf8");
    socket.on("data", (chunk: string) => this.receive(chunk));
    // A close always follows an error and fails whatever is pending.
    socket.on("error", () => {});
    socket.on("close", () => this.finish());
  }

  static open(path: string): Promise<RelayConnection> {
    return new Promise((resolve, reject) => {
      const socket = net.createConnection(path);
      const timer = setTimeout(() => {
        socket.destroy();
        reject(new Error(`connect to ${path} timed out`));
      }, REQUEST_TIMEOUT_MS);
      const onError = (err: Error): void => {
        clearTimeout(timer);
        reject(err);
      };
      socket.once("error", onError);
      socket.once("connect", () => {
        clearTimeout(timer);
        socket.off("error", onError);
        resolve(new RelayConnection(socket));
      });
    });
  }

  call(op: string, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (this.closed) return Promise.reject(new Error(`relayd connection closed before ${op}`));
    const rid = this.nextRid++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(rid);
        reject(new Error(`relayd did not answer ${op} within ${REQUEST_TIMEOUT_MS} ms`));
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(rid, { op, resolve, reject, timer });
      this.socket.write(`${JSON.stringify({ v: PROTOCOL_VERSION, rid, op, args })}\n`);
    });
  }

  close(): void {
    this.socket.destroy();
  }

  private receive(chunk: string): void {
    this.buffer += chunk;
    let end = this.buffer.indexOf("\n");
    while (end >= 0) {
      this.answer(this.buffer.slice(0, end));
      this.buffer = this.buffer.slice(end + 1);
      end = this.buffer.indexOf("\n");
    }
    if (overLineLimit(this.buffer)) this.close();
  }

  private answer(line: string): void {
    let reply: unknown;
    try {
      reply = JSON.parse(line);
    } catch {
      return;
    }
    if (!isRecord(reply) || typeof reply.rid !== "number") return;
    const request = this.pending.get(reply.rid);
    if (!request) return;
    this.pending.delete(reply.rid);
    clearTimeout(request.timer);
    if (reply.ok === true && isRecord(reply.result)) {
      request.resolve(reply.result);
    } else if (reply.ok === false && isRecord(reply.error)) {
      request.reject(new RelayCallError(String(reply.error.code), String(reply.error.message)));
    } else {
      request.reject(new Error(`relayd sent a malformed reply to ${request.op}`));
    }
  }

  private finish(): void {
    if (this.closed) return;
    this.closed = true;
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.reject(new Error(`relayd closed the connection during ${request.op}`));
    }
    this.pending.clear();
    this.onClose?.();
  }
}

interface ExtToolSpec {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

interface Registration {
  token: string;
  brief: string;
  extTools: ExtToolSpec[];
}

// relayd sends ext_tools only when extensions are enabled; a malformed entry is skipped, never fatal.
function extToolSpecs(raw: unknown, log: AdapterLog): ExtToolSpec[] {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) {
    log.warn("relayd's ext_tools is not a list; no extension tools");
    return [];
  }
  const specs: ExtToolSpec[] = [];
  for (const entry of raw) {
    if (isRecord(entry) && typeof entry.name === "string" && typeof entry.description === "string" && isRecord(entry.inputSchema)) {
      specs.push({ name: entry.name, description: entry.description, inputSchema: entry.inputSchema });
    } else {
      log.warn("skipping a malformed extension tool from relayd");
    }
  }
  return specs;
}

function backoffSchedule(env: Record<string, string | undefined>): number[] {
  const override = Number(env.MZ_RELAY_RECONNECT_MS);
  return Number.isFinite(override) && override > 0 ? [override] : BACKOFF_MS;
}

function installedBin(path: string, log: AdapterLog): string | null {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch (err) {
    log.info(`no install record at ${path} (${errorText(err)}); relayd is not started from here`);
    return null;
  }
  try {
    const record: unknown = JSON.parse(raw);
    if (isRecord(record) && typeof record.bin === "string" && record.bin) return record.bin;
    log.info(`install record ${path} names no bin; relayd is not started from here`);
  } catch (err) {
    log.warn(`install record ${path} is not JSON: ${errorText(err)}`);
  }
  return null;
}

// The kept-open hello + register connection that makes this Pi session online in relayd.
class Presence {
  registration: Registration | null = null;
  onRegistered: ((registration: Registration) => void) | null = null;
  readonly paths: RelayPaths;
  readonly log: AdapterLog;
  readonly registerArgs: Record<string, unknown>;
  private delays: number[];
  private conn: RelayConnection | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  // Both reset after a successful register: one spawn per outage, one log line per refusal code.
  private spawnTried = false;
  private refusalsLogged: Set<string> = new Set();
  private stopped = false;

  constructor(paths: RelayPaths, log: AdapterLog, registerArgs: Record<string, unknown>) {
    this.paths = paths;
    this.log = log;
    this.registerArgs = registerArgs;
    this.delays = backoffSchedule(process.env);
  }

  start(): void {
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.registration = null;
    const conn = this.conn;
    this.conn = null;
    conn?.close();
  }

  private async connect(): Promise<void> {
    this.timer = null;
    let conn: RelayConnection;
    try {
      conn = await RelayConnection.open(this.paths.relaydSocket);
    } catch (err) {
      this.retry(`cannot connect to relayd at ${this.paths.relaydSocket}: ${errorText(err)}`, true);
      return;
    }
    if (this.stopped) {
      conn.close();
      return;
    }
    // The open socket must not keep Pi's process alive on exit.
    conn.socket.unref();
    this.conn = conn;
    conn.onClose = () => this.lost(conn);
    try {
      await conn.call("hello", { protocol: PROTOCOL_VERSION, client: "pi", pid: process.pid });
      const result = await conn.call("register", this.registerArgs);
      if (typeof result.token !== "string" || typeof result.brief !== "string") {
        throw new Error("relayd's register result has no token or brief");
      }
      this.registration = { token: result.token, brief: result.brief, extTools: extToolSpecs(result.ext_tools, this.log) };
      this.onRegistered?.(this.registration);
      this.failures = 0;
      this.spawnTried = false;
      this.refusalsLogged.clear();
      const agent = isRecord(result.agent) ? result.agent : {};
      this.log.info(`registered as ${String(agent.name)}, delivery ${String(result.delivery_mode)}`);
    } catch (err) {
      if (this.stopped) return;
      // A roster mistake is refused on every retry; one line per code keeps the log readable.
      const key = err instanceof RelayCallError ? err.code : errorText(err);
      if (!this.refusalsLogged.has(key)) {
        this.refusalsLogged.add(key);
        this.log.warn(`register failed: ${errorText(err)}`);
      }
      conn.close();
    }
  }

  private lost(conn: RelayConnection): void {
    if (this.conn !== conn) return;
    this.conn = null;
    const wasRegistered = this.registration !== null;
    this.registration = null;
    this.retry(wasRegistered ? "relayd closed the presence connection" : "presence connection closed before register", false);
  }

  private retry(reason: string, connectFailed: boolean): void {
    if (this.stopped) return;
    // One line per outage; the retries themselves stay quiet.
    if (this.failures === 0) this.log.warn(`${reason}; reconnecting`);
    if (connectFailed) this.spawnDaemonOnce();
    const delay = this.delays[Math.min(this.failures, this.delays.length - 1)];
    this.failures += 1;
    this.timer = setTimeout(() => void this.connect(), delay);
    this.timer.unref();
  }

  // Only the recorded install is started; a stray mz-relay on PATH may belong to another checkout.
  private spawnDaemonOnce(): void {
    if (this.spawnTried) return;
    this.spawnTried = true;
    const bin = installedBin(this.paths.installRecord, this.log);
    if (!bin) return;
    try {
      const child = spawn(bin, ["daemon", "--ensure"], { detached: true, stdio: "ignore" });
      child.on("error", (err) => this.log.warn(`cannot start ${bin}: ${errorText(err)}`));
      child.unref();
      this.log.info(`started ${bin} daemon --ensure`);
    } catch (err) {
      this.log.warn(`cannot start ${bin}: ${errorText(err)}`);
    }
  }
}

// The per-PID socket relayd writes deliver and seen frames to (S5).
class AdapterServer {
  readonly path: string;
  readonly log: AdapterLog;
  readonly handle: (frame: Record<string, unknown>) => Record<string, unknown>;
  private server: net.Server | null = null;

  constructor(path: string, log: AdapterLog, handle: (frame: Record<string, unknown>) => Record<string, unknown>) {
    this.path = path;
    this.log = log;
    this.handle = handle;
  }

  listen(): Promise<boolean> {
    const size = Buffer.byteLength(this.path);
    if (size >= SOCKET_PATH_LIMIT) {
      this.log.error(`listen error: ${this.path} is ${size} bytes; Unix sockets need fewer than ${SOCKET_PATH_LIMIT}`);
      return Promise.resolve(false);
    }
    try {
      unlinkSync(this.path);
      this.log.info(`removed a stale ${this.path}`);
    } catch (err) {
      if ((err as { code?: unknown }).code !== "ENOENT") {
        this.log.error(`listen error: cannot remove ${this.path}: ${errorText(err)}`);
        return Promise.resolve(false);
      }
    }
    const server = net.createServer((socket) => this.accept(socket));
    return new Promise((resolve) => {
      server.once("error", (err) => {
        this.log.error(`listen error on ${this.path}: ${errorText(err)}`);
        resolve(false);
      });
      server.listen(this.path, () => {
        server.removeAllListeners("error");
        server.on("error", (err) => this.log.warn(`adapter socket error: ${errorText(err)}`));
        server.unref();
        this.server = server;
        this.log.info(`listening on ${this.path}`);
        resolve(true);
      });
    });
  }

  close(): void {
    const server = this.server;
    this.server = null;
    if (!server) return;
    server.close();
    try {
      unlinkSync(this.path);
    } catch (err) {
      this.log.warn(`cannot remove ${this.path}: ${errorText(err)}`);
    }
  }

  private accept(socket: net.Socket): void {
    let buffer = "";
    socket.setEncoding("utf8");
    socket.on("error", (err) => this.log.warn(`adapter connection error: ${errorText(err)}`));
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      let end = buffer.indexOf("\n");
      while (end >= 0) {
        const reply = this.answer(buffer.slice(0, end));
        if (reply) socket.write(`${JSON.stringify(reply)}\n`);
        buffer = buffer.slice(end + 1);
        end = buffer.indexOf("\n");
      }
      if (overLineLimit(buffer)) {
        this.log.warn(`a line over ${MAX_LINE_BYTES} bytes on the adapter socket; closing the connection`);
        socket.destroy();
      }
    });
  }

  private answer(line: string): Record<string, unknown> | null {
    let frame: unknown;
    try {
      frame = JSON.parse(line);
    } catch {
      frame = null;
    }
    if (!isRecord(frame)) {
      this.log.warn(`ignored a malformed line of ${line.length} chars on the adapter socket`);
      return null;
    }
    return this.handle(frame);
  }
}

// Ids of mz-relay messages Pi has put in front of the model; relayd's seen query confirms delivery from it.
class SeenIds {
  private ids: Set<string> = new Set();

  addFrom(messages: unknown[]): void {
    for (const message of messages) {
      if (isRelayMessage(message) && typeof message.details?.mzRelayId === "string") this.add(message.details.mzRelayId);
    }
  }

  has(id: string): boolean {
    return this.ids.has(id);
  }

  private add(id: string): void {
    this.ids.delete(id);
    this.ids.add(id);
    if (this.ids.size > SEEN_MAX) {
      const oldest = this.ids.values().next().value;
      if (oldest !== undefined) this.ids.delete(oldest);
    }
  }
}

function latestPrompt(messages: unknown[]): unknown {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (!isRecord(message) || NON_PROMPT_ROLES.has(String(message.role))) continue;
    if (message.role === "custom" && message.customType !== CUSTOM_TYPE) continue;
    return message;
  }
  return undefined;
}

// S10: a run whose latest prompt is a read_only peer message may only use READ_ONLY_ALLOWED.
class ReadOnlyGate {
  peerRun = false;
  readonly log: AdapterLog | null;

  constructor(log: AdapterLog | null) {
    this.log = log;
  }

  // A context with no prompt line (say, the peer message compacted away) says nothing about the run.
  onContext(messages: unknown[]): void {
    const prompt = latestPrompt(messages);
    if (prompt !== undefined) this.set(isReadOnlyPeer(prompt), "context");
  }

  onAgentStart(event: Partial<AgentStartEvent>): void {
    if (event.messageType === "custom" && isReadOnlyPeer(event.message)) this.set(true, "before_agent_start");
  }

  settle(): void {
    this.set(false, "agent_settled");
  }

  check(toolName: string): GateDecision | undefined {
    if (!this.peerRun || READ_ONLY_ALLOWED.has(toolName)) return undefined;
    this.log?.info(`blocked ${toolName} during a read_only peer run`);
    return { block: true, reason: peerOnlyReason(toolName) };
  }

  private set(peerRun: boolean, cause: string): void {
    if (peerRun !== this.peerRun) this.log?.info(`read-only gate ${peerRun ? "closed" : "opened"} by ${cause}`);
    this.peerRun = peerRun;
  }
}

interface ToolSession {
  relaydSocket: string;
  token: string;
}

function objectSchema(properties: Record<string, object>, required?: string[]): Record<string, unknown> {
  return required
    ? { type: "object", properties, required, additionalProperties: false }
    : { type: "object", properties, additionalProperties: false };
}

function textResult(text: string): ToolResult {
  return { content: [{ type: "text", text }], details: {} };
}

// The S6 tools as native Pi tools; each call opens its own relayd connection and returns relayd's text.
class RelayTools {
  readonly session: () => ToolSession | null;
  readonly log: () => AdapterLog | null;
  private readonly extensionTools = new Set<string>();

  constructor(session: () => ToolSession | null, log: () => AdapterLog | null) {
    this.session = session;
    this.log = log;
  }

  register(pi: PiApi): void {
    pi.registerTool(
      this.tool(
        "mz_relay_send",
        "mz-relay send",
        "Send a message to another agent on mz-relay by roster name. " +
          "Returns the message id and whether it was queued or held for user approval.",
        objectSchema(
          {
            to: { type: "string", description: "Roster name of the recipient." },
            body: { type: "string", maxLength: 32000, description: "The message text." },
            kind: {
              type: "string",
              enum: ["request", "inform", "reply", "done"],
              default: "request",
              description: "request expects a reply, inform does not, reply answers a message, done closes the context.",
            },
            context_id: { type: "string", description: "Only with in_reply_to; must equal the parent's context" },
            in_reply_to: { type: "string", description: "Id of the message this one answers." },
          },
          ["to", "body"],
        ),
        "send",
        (params) => {
          // Drawn per call, so relayd's dedupe sees a fresh message each time the agent sends.
          const args: Record<string, unknown> = {
            id: randomUUID(),
            to: params.to,
            kind: params.kind ?? "request",
            body: params.body,
          };
          if (params.context_id !== undefined) args.context_id = params.context_id;
          if (params.in_reply_to !== undefined) args.in_reply_to = params.in_reply_to;
          return args;
        },
      ),
    );
    pi.registerTool(
      this.tool(
        "mz_relay_inbox",
        "mz-relay inbox",
        "Read new peer messages, the ones still awaiting your ack, and the status of messages you sent.",
        objectSchema({
          limit: {
            type: "integer",
            minimum: 1,
            maximum: 50,
            default: INBOX_LIMIT_DEFAULT,
            description: "Most new messages to return.",
          },
        }),
        "inbox",
        (params) => ({ limit: params.limit ?? INBOX_LIMIT_DEFAULT }),
      ),
    );
    pi.registerTool(
      this.tool(
        "mz_relay_ack",
        "mz-relay ack",
        "Mark a peer message as handled.",
        objectSchema({ id: { type: "string", description: "Id of the message to ack." } }, ["id"]),
        "ack",
        (params) => ({ id: params.id }),
      ),
    );
    pi.registerTool(
      this.tool(
        "mz_relay_peers",
        "mz-relay peers",
        "List the other agents on mz-relay, their roles, and who is online.",
        objectSchema({}),
        "peers",
        () => ({}),
      ),
    );
  }

  // Pi takes tools after startup too (docs: refreshed in the same session); each is added once.
  registerExtensions(pi: PiApi, specs: ExtToolSpec[]): void {
    const added: string[] = [];
    for (const spec of specs) {
      if (this.extensionTools.has(spec.name)) continue;
      this.extensionTools.add(spec.name);
      pi.registerTool(
        this.tool(`mz_relay_${spec.name}`, `mz-relay ${spec.name}`, spec.description, spec.inputSchema, "ext_call", (params) => ({
          tool: spec.name,
          args: params,
        })),
      );
      added.push(spec.name);
    }
    if (added.length > 0) this.log()?.info(`registered extension tools: ${added.join(", ")}`);
  }

  private tool(
    name: string,
    label: string,
    description: string,
    parameters: Record<string, unknown>,
    op: string,
    wireArgs: (params: Record<string, unknown>) => Record<string, unknown>,
  ): PiTool {
    return {
      name,
      label,
      description,
      parameters,
      execute: async (_toolCallId, params) => textResult(await this.call(op, () => wireArgs(params ?? {}))),
    };
  }

  // Never throws: Pi shows the returned text, errors included, to the agent.
  private async call(op: string, wireArgs: () => Record<string, unknown>): Promise<string> {
    const session = this.session();
    if (!session) return NOT_CONNECTED;
    let conn: RelayConnection | null = null;
    try {
      conn = await RelayConnection.open(session.relaydSocket);
      await conn.call("hello", { protocol: PROTOCOL_VERSION, client: "pi", pid: process.pid });
      const result = await conn.call(op, { ...wireArgs(), token: session.token });
      if (typeof result.text !== "string") {
        this.log()?.warn(`relayd's ${op} result has no text`);
        return `mz-relay error invalid_request: relayd's ${op} result has no text`;
      }
      return result.text;
    } catch (err) {
      if (err instanceof RelayCallError) {
        this.log()?.info(`tool ${op} returned error ${err.code}`);
        return `mz-relay error ${err.code}: ${err.message}`;
      }
      this.log()?.warn(`tool ${op} could not reach relayd: ${errorText(err)}`);
      return `mz-relay error daemon_unavailable: cannot reach relayd: ${errorText(err)}`;
    } finally {
      conn?.close();
    }
  }
}

class MzRelayExtension {
  readonly pi: PiApi;
  readonly paths: RelayPaths | null;
  readonly log: AdapterLog | null;
  readonly seen = new SeenIds();
  readonly gate: ReadOnlyGate;
  presence: Presence | null = null;
  server: AdapterServer | null = null;
  private tools: RelayTools | null = null;
  private disabled = false;

  constructor(pi: PiApi) {
    this.pi = pi;
    let paths: RelayPaths | null = null;
    try {
      paths = RelayPaths.fromEnv(process.env);
    } catch (err) {
      // Without paths there is no run dir to log into; the tools then report "not connected".
      console.error(`mz-relay: disabled: ${errorText(err)}`);
    }
    this.paths = paths;
    this.log = paths ? new AdapterLog(paths.adapterLog) : null;
    this.gate = new ReadOnlyGate(this.log);
  }

  attach(): void {
    const tools = new RelayTools(
      () => this.toolSession(),
      () => this.log,
    );
    tools.register(this.pi);
    this.tools = tools;
    this.pi.on("session_start", () => this.start());
    this.pi.on("session_shutdown", () => this.shutdown());
    this.pi.on("context", (e) => {
      const messages = Array.isArray(e?.messages) ? e.messages : [];
      this.seen.addFrom(messages);
      this.gate.onContext(messages);
      return undefined;
    });
    this.pi.on("before_agent_start", (e) => {
      this.gate.onAgentStart(isRecord(e) ? e : {});
      const registration = this.presence?.registration;
      if (!registration) return undefined;
      return { systemPrompt: `${e.systemPrompt ?? ""}\n\n${registration.brief}` };
    });
    this.pi.on("agent_settled", () => this.gate.settle());
    this.pi.on("tool_call", async (e) => this.gate.check(String(e?.toolName)));
  }

  private toolSession(): ToolSession | null {
    const registration = this.presence?.registration;
    if (!this.paths || !registration) return null;
    return { relaydSocket: this.paths.relaydSocket, token: registration.token };
  }

  private async start(): Promise<void> {
    const paths = this.paths;
    const log = this.log;
    if (!paths || !log || this.presence || this.disabled) return;
    let loosened: number | null;
    try {
      loosened = claimRunDir(paths.run);
    } catch (err) {
      // The run dir can't be trusted with a log file, so the refusal goes to stderr once.
      this.disabled = true;
      console.error(`mz-relay: disabled: ${errorText(err)}`);
      return;
    }
    if (loosened !== null) log.info(`tightened ${paths.run} from ${loosened.toString(8)} to 700`);
    try {
      const server = new AdapterServer(paths.adapterSocket, log, (frame) => this.answer(frame));
      this.server = server;
      const listening = await server.listen();
      if (this.server !== server) {
        // session_shutdown ran during listen and found no server to close yet.
        server.close();
        log.info("session ended while the adapter socket was opening");
        return;
      }
      // Without a listening socket relayd could only fail deliveries, so the session registers pull-only.
      const delivery = listening ? { socket: paths.adapterSocket, pid: process.pid } : undefined;
      this.presence = new Presence(paths, log, {
        name: process.env.MZ_RELAY_NAME || null,
        harness: "pi",
        cwd: process.cwd(),
        pid: process.pid,
        delivery,
      });
      this.presence.onRegistered = (registration) => this.tools?.registerExtensions(this.pi, registration.extTools);
      this.presence.start();
    } catch (err) {
      log.error(`session_start failed: ${errorText(err)}`);
    }
  }

  private shutdown(): void {
    try {
      this.presence?.stop();
      this.server?.close();
    } catch (err) {
      this.log?.error(`session_shutdown failed: ${errorText(err)}`);
    } finally {
      this.presence = null;
      this.server = null;
    }
  }

  private answer(frame: Record<string, unknown>): Record<string, unknown> {
    const id = typeof frame.id === "string" ? frame.id : "";
    if (frame.op === "deliver") return this.deliver(id, frame);
    if (frame.op === "seen") {
      if (!id) return { nack: "", error: "seen needs an id" };
      return this.seen.has(id) ? { seen: id } : { unseen: id };
    }
    this.log?.warn(`unknown op on the adapter socket for ${id || "<no id>"}`);
    return { nack: id, error: "unknown op" };
  }

  private deliver(id: string, frame: Record<string, unknown>): Record<string, unknown> {
    const { text, gate } = frame;
    if (!id || typeof text !== "string" || (gate !== "full" && gate !== "read_only")) {
      this.log?.warn(`bad deliver frame for ${id || "<no id>"}`);
      return { nack: id, error: "deliver needs id, text and gate full or read_only" };
    }
    try {
      this.pi.sendMessage(
        { customType: CUSTOM_TYPE, content: text, display: true, details: { mzRelayId: id, gate } },
        { triggerTurn: true, deliverAs: "followUp" },
      );
    } catch (err) {
      // Pi's error message is harness-controlled text, so only its name goes in the log.
      this.log?.warn(`sendMessage failed for ${id}: ${err instanceof Error ? err.name : typeof err}`);
      return { nack: id, error: String(err) };
    }
    // sendMessage returns void, so this only says Pi took it; relayd's seen query confirms later.
    this.log?.info(`handed ${id} to Pi (gate ${gate})`);
    return { ack: id, state: "handed_off" };
  }
}

export default function mzRelay(pi: PiApi): void {
  new MzRelayExtension(pi).attach();
}
