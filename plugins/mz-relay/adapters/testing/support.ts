// Test support for the TS adapters: a temp MZ_RELAY_HOME, bounded waits and a client for the adapter socket.
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

export const RELAY_ENV = [
  "MZ_RELAY_HOME",
  "MZ_RELAY_NAME",
  "MZ_RELAY_RECONNECT_MS",
  "XDG_RUNTIME_DIR",
  "XDG_CONFIG_HOME",
  "XDG_STATE_HOME",
  "PATH",
] as const;

export type RelayEnv = Partial<Record<(typeof RELAY_ENV)[number], string>>;

export function delay(ms: number): Promise<void> {
  return sleep(ms);
}

// Polls until cond holds; every wait in the adapter tests is bounded so a missing behaviour fails, never hangs.
export async function waitUntil(
  cond: () => boolean | Promise<boolean>,
  what: string,
  timeoutMs = 3000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await cond()) return;
    await sleep(10);
  }
  throw new Error(`timed out after ${timeoutMs} ms waiting for ${what}`);
}

export interface HomeShape {
  // Pads the home so even relayd.sock's path is past sun_path.
  long?: boolean;
  // Pads the home so <run> is exactly this many bytes.
  runLength?: number;
}

// A fresh home under /tmp, short enough for sun_path unless a HomeShape pads it.
export class TempHome {
  readonly root: string;
  readonly home: string;
  readonly run: string;
  saved: Map<string, string | undefined> = new Map();

  constructor(shape: HomeShape = {}) {
    this.root = mkdtempSync("/tmp/mzr-");
    if (shape.runLength !== undefined) {
      const pad = shape.runLength - this.root.length - "/".length - "/run".length;
      if (pad < 1) throw new Error(`cannot build a run dir of ${shape.runLength} bytes under ${this.root}`);
      this.home = join(this.root, "p".repeat(pad));
    } else {
      this.home = shape.long ? join(this.root, "h".repeat(120)) : this.root;
    }
    this.run = join(this.home, "run");
    mkdirSync(this.run, { recursive: true, mode: 0o700 });
  }

  // Applies MZ_RELAY_HOME plus overrides; an undefined value unsets the variable.
  applyEnv(overrides: RelayEnv = {}): void {
    const wanted: Record<string, string | undefined> = {
      MZ_RELAY_HOME: this.home,
      MZ_RELAY_NAME: undefined,
      MZ_RELAY_RECONNECT_MS: "50",
      XDG_RUNTIME_DIR: undefined,
      XDG_CONFIG_HOME: undefined,
      XDG_STATE_HOME: undefined,
      ...overrides,
    };
    for (const [key, value] of Object.entries(wanted)) {
      if (!this.saved.has(key)) this.saved.set(key, process.env[key]);
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }

  adapterSocket(kind: "pi" | "oc", pid = process.pid): string {
    return join(this.run, `${kind}-${pid}.sock`);
  }

  adapterLog(kind: "pi" | "oc", pid = process.pid): string {
    return join(this.run, `${kind}-${pid}.log`);
  }

  readLog(kind: "pi" | "oc"): string {
    const path = this.adapterLog(kind);
    return existsSync(path) ? readFileSync(path, "utf8") : "";
  }

  writeInstallRecord(record: Record<string, unknown>): void {
    writeFileSync(join(this.home, "install.json"), JSON.stringify(record));
  }

  // A fake bin at `<home>/bin/<name>` that appends its argv to `<home>/spawned.txt`, so a test sees each spawn.
  writeRecordingBin(name: string): { bin: string; spawnLog: string } {
    mkdirSync(join(this.home, "bin"), { recursive: true });
    const bin = join(this.home, "bin", name);
    const spawnLog = join(this.home, "spawned.txt");
    writeFileSync(bin, `#!/bin/sh\necho "$*" >> '${spawnLog}'\n`);
    chmodSync(bin, 0o755);
    return { bin, spawnLog };
  }

  dispose(): void {
    for (const [key, value] of this.saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    this.saved.clear();
    rmSync(this.root, { recursive: true, force: true });
  }
}

// Lines of text that contain needle.
export function countLines(text: string, needle: string): number {
  return text.split("\n").filter((line) => line.includes(needle)).length;
}

export async function socketAccepts(path: string): Promise<boolean> {
  try {
    const client = await AdapterClient.connect(path, 500);
    client.close();
    return true;
  } catch {
    return false;
  }
}

// Plays relayd's side of the adapter socket: writes request lines and reads JSON reply lines.
export class AdapterClient {
  socket: net.Socket;
  lines: string[] = [];
  buffer = "";
  waiters: (() => void)[] = [];
  ended = false;

  constructor(socket: net.Socket) {
    this.socket = socket;
    socket.setEncoding("utf8");
    socket.on("data", (chunk: string) => {
      this.buffer += chunk;
      let end = this.buffer.indexOf("\n");
      while (end >= 0) {
        this.lines.push(this.buffer.slice(0, end));
        this.buffer = this.buffer.slice(end + 1);
        end = this.buffer.indexOf("\n");
      }
      this.wake();
    });
    socket.on("close", () => {
      this.ended = true;
      this.wake();
    });
    socket.on("error", () => {});
  }

  static connect(path: string, timeoutMs = 2000): Promise<AdapterClient> {
    return new Promise((resolve, reject) => {
      const socket = net.createConnection(path);
      const timer = setTimeout(() => {
        socket.destroy();
        reject(new Error(`connect to ${path} timed out`));
      }, timeoutMs);
      socket.once("connect", () => {
        clearTimeout(timer);
        resolve(new AdapterClient(socket));
      });
      socket.once("error", (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });
  }

  wake(): void {
    const waiters = this.waiters;
    this.waiters = [];
    for (const wake of waiters) wake();
  }

  write(frame: string | Record<string, unknown>): void {
    this.socket.write(typeof frame === "string" ? frame : `${JSON.stringify(frame)}\n`);
  }

  // The next reply line parsed as JSON; rejects on timeout or when the adapter hangs up first.
  async next(timeoutMs = 2000): Promise<Record<string, unknown>> {
    const deadline = Date.now() + timeoutMs;
    while (this.lines.length === 0) {
      if (this.ended) throw new Error("adapter closed the connection without a reply");
      const left = deadline - Date.now();
      if (left <= 0) throw new Error(`no reply within ${timeoutMs} ms`);
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, left);
        this.waiters.push(() => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
    return JSON.parse(this.lines.shift() as string) as Record<string, unknown>;
  }

  close(): void {
    this.socket.destroy();
  }
}

// One request and its reply on a fresh connection, the way relayd talks to an adapter.
export async function exchange(
  path: string,
  frame: Record<string, unknown>,
  timeoutMs = 2000,
): Promise<Record<string, unknown>> {
  const client = await AdapterClient.connect(path, timeoutMs);
  try {
    client.write(frame);
    return await client.next(timeoutMs);
  } finally {
    client.close();
  }
}
