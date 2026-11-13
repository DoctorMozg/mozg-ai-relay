// A fake OpenCode plugin client: records session.promptAsync calls and answers with scripted results.
import { AdapterRig, type RigOptions } from "./rig.ts";

export interface PromptOpts {
  path: { id: string };
  body: Record<string, unknown> & { parts: { type: string; text: string }[] };
}

export type PromptOutcome = { error?: unknown; response?: { status: number } } | { reject: Error };

export class FakeOcClient {
  calls: PromptOpts[] = [];
  // 204 is what OpenCode answers when it accepted an async prompt.
  outcome: PromptOutcome = { response: { status: 204 } };
  session: { promptAsync(opts: PromptOpts): Promise<{ error?: unknown; response?: { status: number } }> };

  constructor() {
    this.session = {
      promptAsync: async (opts: PromptOpts) => {
        this.calls.push(opts);
        const outcome = this.outcome;
        if ("reject" in outcome) throw outcome.reject;
        return outcome;
      },
    };
  }
}

export function sessionEvent(
  type: "session.created" | "session.updated",
  id: string,
  parentID?: string,
): { event: { type: string; properties: Record<string, unknown> } } {
  const info: Record<string, unknown> = { id, projectID: "p1", directory: process.cwd(), title: id };
  if (parentID !== undefined) info.parentID = parentID;
  return { event: { type, properties: { info } } };
}

export interface OcHooksLike {
  config(c: Record<string, unknown>): Promise<void>;
  event(i: { event: { type: string; properties?: Record<string, unknown> } }): Promise<void>;
  "chat.message"(i: { sessionID: string; agent?: string }, o: unknown): Promise<void>;
  dispose(): Promise<void>;
}

export type OcPlugin = (input: { client: FakeOcClient; directory: string; worktree: string }) => Promise<OcHooksLike>;

export type OcConfig = Record<string, Record<string, unknown>>;

// One OpenCode plugin instance per test: the shared rig plus a FakeOcClient and the initialised hooks.
export class OcRig extends AdapterRig {
  client: FakeOcClient = new FakeOcClient();
  hooks: OcHooksLike | null = null;

  constructor() {
    super("oc");
  }

  static async start(plugin: OcPlugin, opts: RigOptions = {}): Promise<OcRig> {
    const rig = new OcRig();
    try {
      await rig.prepare(opts, { MZ_RELAY_NAME: "helper" });
      rig.hooks = await plugin({ client: rig.client, directory: process.cwd(), worktree: process.cwd() });
    } catch (err) {
      await rig.stop();
      throw err;
    }
    return rig;
  }

  get plugin(): OcHooksLike {
    if (!this.hooks) throw new Error("the plugin did not initialise");
    return this.hooks;
  }

  override async closeAdapter(): Promise<void> {
    await this.hooks?.dispose();
  }

  dispose(): Promise<void> {
    return this.closeAdapterOnce();
  }

  async chat(sessionID: string): Promise<void> {
    await this.plugin["chat.message"]({ sessionID }, { message: {}, parts: [] });
  }

  // Runs the config hook on seed, which it mutates in place as OpenCode expects, and returns it.
  async configured(seed: OcConfig = {}): Promise<OcConfig> {
    await this.plugin.config(seed);
    return seed;
  }
}
