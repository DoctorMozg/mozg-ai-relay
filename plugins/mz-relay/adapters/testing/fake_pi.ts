// A fake Pi 1.5.2 ExtensionAPI: records registerTool/sendMessage/on and lets tests fire events.
import { AdapterRig, type RigOptions } from "./rig.ts";
import { waitUntil } from "./support.ts";

export type PiHandler = (event: unknown, ctx: unknown) => unknown;

export interface PiToolResult {
  content: { type: string; text: string }[];
  details: Record<string, unknown>;
}

export interface RecordedTool {
  name: string;
  label: string;
  description: string;
  parameters: Record<string, unknown>;
  execute(toolCallId: string, params: Record<string, unknown>, signal?: AbortSignal): Promise<PiToolResult>;
}

export interface SentMessage {
  msg: Record<string, unknown>;
  opts: Record<string, unknown>;
}

export interface GateResult {
  block?: boolean;
  reason?: string;
}

export class FakePi {
  handlers: Map<string, PiHandler[]> = new Map();
  tools: RecordedTool[] = [];
  sent: SentMessage[] = [];
  sendError: Error | null = null;
  // What Pi hands every handler as its second argument.
  ctx: Record<string, unknown> = { cwd: process.cwd(), hasUI: false };

  on(event: string, handler: PiHandler): void {
    const list = this.handlers.get(event) ?? [];
    list.push(handler);
    this.handlers.set(event, list);
  }

  registerTool(tool: RecordedTool): void {
    this.tools.push(tool);
  }

  sendMessage(msg: Record<string, unknown>, opts: Record<string, unknown>): void {
    if (this.sendError) throw this.sendError;
    this.sent.push({ msg, opts });
  }

  // Runs every handler for event in order and returns their awaited results.
  async fire(event: string, payload: unknown = { type: event }): Promise<unknown[]> {
    const results: unknown[] = [];
    for (const handler of this.handlers.get(event) ?? []) results.push(await handler(payload, this.ctx));
    return results;
  }

  async sessionStart(): Promise<void> {
    await this.fire("session_start", { type: "session_start", reason: "startup" });
  }

  async sessionShutdown(): Promise<void> {
    await this.fire("session_shutdown", { type: "session_shutdown" });
  }

  async context(messages: Record<string, unknown>[]): Promise<void> {
    await this.fire("context", { type: "context", messages });
  }

  // Pi applies the first handler result that blocks.
  async toolCall(toolName: string): Promise<GateResult | undefined> {
    const results = await this.fire("tool_call", { type: "tool_call", toolName, toolCallId: "call-1", input: {} });
    const blocked = results.find((r) => r && typeof r === "object" && (r as GateResult).block);
    return blocked as GateResult | undefined;
  }

  async beforeAgentStart(payload: Record<string, unknown>): Promise<{ systemPrompt?: string } | undefined> {
    const results = await this.fire("before_agent_start", { type: "before_agent_start", ...payload });
    return results.find((r) => r !== undefined) as { systemPrompt?: string } | undefined;
  }

  tool(name: string): RecordedTool {
    const tool = this.tools.find((t) => t.name === name);
    if (!tool) throw new Error(`no tool ${name} registered (have: ${this.tools.map((t) => t.name).join(", ")})`);
    return tool;
  }
}

// Message shapes Pi passes in `context` events.
export const piMessage = {
  user: (text = "hi"): Record<string, unknown> => ({ role: "user", content: text }),
  assistant: (): Record<string, unknown> => ({ role: "assistant", content: [{ type: "text", text: "ok" }] }),
  toolResult: (): Record<string, unknown> => ({ role: "toolResult", toolCallId: "call-1", content: [] }),
  compactionSummary: (): Record<string, unknown> => ({ role: "compactionSummary", summary: "earlier work" }),
  branchSummary: (): Record<string, unknown> => ({ role: "branchSummary", summary: "a branch" }),
  peer: (id: string, gate: "full" | "read_only"): Record<string, unknown> => ({
    role: "custom",
    customType: "mz-relay",
    content: `[mz-relay] Peer message ${id}`,
    display: true,
    details: { mzRelayId: id, gate },
  }),
  otherCustom: (id = "x1"): Record<string, unknown> => ({
    role: "custom",
    customType: "other-extension",
    content: "note",
    display: true,
    details: { mzRelayId: id },
  }),
};

export type PiFactory = (pi: FakePi) => unknown;

export interface PiRigOptions extends RigOptions {
  sessionStart?: boolean;
}

// One Pi session per test: the shared rig plus a FakePi with the extension loaded and, by default, started.
export class PiRig extends AdapterRig {
  pi: FakePi = new FakePi();

  constructor() {
    super("pi");
  }

  static async start(factory: PiFactory, opts: PiRigOptions = {}): Promise<PiRig> {
    const rig = new PiRig();
    try {
      await rig.prepare(opts, { MZ_RELAY_NAME: "scout" });
      await factory(rig.pi);
      if (opts.sessionStart ?? true) await rig.pi.sessionStart();
    } catch (err) {
      await rig.stop();
      throw err;
    }
    return rig;
  }

  override async closeAdapter(): Promise<void> {
    await this.pi.sessionShutdown();
  }

  shutdown(): Promise<void> {
    return this.closeAdapterOnce();
  }

  // The brief is only returned once register succeeded, so it is the public "registered" signal.
  async waitRegistered(timeoutMs = 3000): Promise<void> {
    await waitUntil(
      async () => (await this.pi.beforeAgentStart({ systemPrompt: "", messageType: "user" })) !== undefined,
      "the extension to finish registering",
      timeoutMs,
    );
  }
}
