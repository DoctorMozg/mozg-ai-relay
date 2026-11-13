// A fake relayd for the TS adapter tests: relayd's line JSON on <run>/relayd.sock, recorded frames, scripted replies.
import { rmSync } from "node:fs";
import net from "node:net";
import { join } from "node:path";

import { waitUntil } from "./support.ts";

export interface Frame {
  conn: number;
  rid: number | null;
  op: string;
  args: Record<string, unknown>;
  raw: string;
}

export interface ErrorReply {
  error: { code: string; message: string; data?: Record<string, unknown> };
}

// "silent" sends nothing; "close" hangs up on the client instead of answering.
export type Reply = { result: Record<string, unknown> } | ErrorReply | "silent" | "close";
export type Handler = (args: Record<string, unknown>, frame: Frame) => Reply;

// 32 hex like a real token, distinctive enough to grep for in adapter logs.
export const FAKE_TOKEN = "5f3c9a7e1b2d4c6f8a0e9d7c5b3a1f2e";

export function fakeBrief(name: string): string {
  return [
    `You are "${name}" on mz-relay, role ${name}: Reads code and reports findings`,
    "Peers you may message (name : role — description):",
    "- architect : architect — Designs the system",
    "Rules:",
    "- kind=request expects a reply; inform never does; done closes the context.",
  ].join("\n");
}

const NO_HELLO_OPS = new Set(["hello", "shutdown"]);

interface Conn {
  id: number;
  socket: net.Socket;
  helloDone: boolean;
  open: boolean;
}

export class FakeRelayd {
  readonly runDir: string;
  readonly path: string;
  frames: Frame[] = [];
  handlers: Map<string, Handler> = new Map();
  refusals: Map<string, string> = new Map();
  conns: Map<number, Conn> = new Map();
  server: net.Server | null = null;
  nextConn = 1;

  constructor(runDir: string) {
    this.runDir = runDir;
    this.path = join(runDir, "relayd.sock");
    this.handlers.set("hello", () => ({
      result: {
        protocol: 1,
        daemon_pid: process.pid,
        version: "0.1.0",
        db: join(runDir, "relay.db"),
        roster: join(runDir, "roster.toml"),
      },
    }));
    this.handlers.set("register", (args) => {
      const name = typeof args.name === "string" ? args.name : "scout";
      return {
        result: {
          token: FAKE_TOKEN,
          agent: { name, role: name, harness: args.harness, label: "personal", description: "Reads code", tools: "full" },
          delivery_mode: args.delivery ? "socket" : null,
          brief: fakeBrief(name),
        },
      };
    });
  }

  static async start(runDir: string): Promise<FakeRelayd> {
    const relayd = new FakeRelayd(runDir);
    await relayd.listen();
    return relayd;
  }

  // Separate from the constructor so a test can bring relayd up after the adapter started.
  listen(): Promise<void> {
    rmSync(this.path, { force: true });
    const server = net.createServer((socket) => this.accept(socket));
    this.server = server;
    return new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(this.path, () => {
        server.off("error", reject);
        resolve();
      });
    });
  }

  onOp(op: string, handler: Handler): void {
    this.handlers.set(op, handler);
  }

  // Refuses op with code until called again with null; the handler set for op stays in place.
  refuse(op: string, code: string | null): void {
    if (code === null) this.refusals.delete(op);
    else this.refusals.set(op, code);
  }

  accept(socket: net.Socket): void {
    const conn: Conn = { id: this.nextConn++, socket, helloDone: false, open: true };
    this.conns.set(conn.id, conn);
    let buffer = "";
    socket.setEncoding("utf8");
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      let end = buffer.indexOf("\n");
      while (end >= 0 && conn.open) {
        this.handle(conn, buffer.slice(0, end));
        buffer = buffer.slice(end + 1);
        end = buffer.indexOf("\n");
      }
    });
    socket.on("close", () => {
      conn.open = false;
    });
    socket.on("error", () => {});
  }

  handle(conn: Conn, raw: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = null;
    }
    const obj = (parsed && typeof parsed === "object" ? parsed : {}) as Record<string, unknown>;
    const frame: Frame = {
      conn: conn.id,
      rid: typeof obj.rid === "number" ? obj.rid : null,
      op: typeof obj.op === "string" ? obj.op : "<malformed>",
      args: (obj.args && typeof obj.args === "object" ? obj.args : {}) as Record<string, unknown>,
      raw,
    };
    this.frames.push(frame);
    let reply: Reply;
    if (frame.op === "<malformed>" || frame.rid === null || obj.v !== 1) {
      reply = { error: { code: "invalid_request", message: "malformed request" } };
    } else if (!NO_HELLO_OPS.has(frame.op) && !conn.helloDone) {
      reply = { error: { code: "invalid_request", message: `${frame.op}: hello first` } };
    } else {
      const handler = this.handlers.get(frame.op);
      const refusal = this.refusals.get(frame.op);
      reply = refusal
        ? { error: { code: refusal, message: "refused by the test relayd" } }
        : handler
        ? handler(frame.args, frame)
        : { error: { code: "unknown_op", message: `unknown op '${frame.op}'` } };
      if (frame.op === "hello" && typeof reply === "object" && "result" in reply) conn.helloDone = true;
    }
    if (reply === "silent") return;
    if (reply === "close") {
      this.dropConnection(conn.id);
      return;
    }
    const body = "result" in reply ? { ok: true, result: reply.result } : { ok: false, error: reply.error };
    conn.socket.write(`${JSON.stringify({ v: 1, rid: frame.rid, ...body })}\n`);
  }

  framesOf(op: string): Frame[] {
    return this.frames.filter((f) => f.op === op);
  }

  // Resolves with the first recorded frame matching op (and pred), waiting up to timeoutMs for it.
  async waitFor(op: string, timeoutMs = 3000, pred: (f: Frame) => boolean = () => true): Promise<Frame> {
    const find = (): Frame | undefined => this.frames.find((f) => f.op === op && pred(f));
    try {
      await waitUntil(() => find() !== undefined, `a ${op} frame`, timeoutMs);
    } catch (err) {
      const seen = this.frames.map((f) => f.op).join(", ") || "none";
      throw new Error(`fake relayd: ${(err as Error).message} (frames so far: ${seen})`);
    }
    return find() as Frame;
  }

  isOpen(connId: number): boolean {
    return this.conns.get(connId)?.open ?? false;
  }

  dropConnection(connId: number): void {
    const conn = this.conns.get(connId);
    if (!conn) return;
    conn.open = false;
    conn.socket.destroy();
  }

  dropAll(): void {
    for (const id of this.conns.keys()) this.dropConnection(id);
  }

  async close(): Promise<void> {
    this.dropAll();
    const server = this.server;
    this.server = null;
    if (server) await new Promise<void>((resolve) => server.close(() => resolve()));
    rmSync(this.path, { force: true });
  }
}
