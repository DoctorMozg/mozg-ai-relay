// Shared per-test rig for the Pi and OpenCode adapters: temp home and env, fake relayd, fake bins, teardown.
import { existsSync, readFileSync, rmSync, symlinkSync } from "node:fs";
import { afterEach } from "node:test";

import { FakeRelayd } from "./fake_relayd.ts";
import { type RelayEnv, socketAccepts, TempHome, waitUntil } from "./support.ts";

// Per test and per teardown: a leaked server or timer fails one test instead of hanging the file.
export const TEST_TIMEOUT = { timeout: 15_000 };

export interface RigOptions {
  relayd?: boolean;
  long?: boolean;
  runLength?: number;
  // Runs after the temp home exists and before env is applied; may reshape <run> and return extra env.
  setup?: (home: TempHome) => RelayEnv | void;
  env?: RelayEnv;
  install?: Record<string, unknown>;
  // "install.json" points install.json's bin at a recording fake; "PATH" puts one on PATH as `mz-relay`.
  recordingBin?: "install.json" | "PATH";
}

export class AdapterRig {
  kind: "pi" | "oc";
  tempHome: TempHome | null = null;
  relayd: FakeRelayd | null = null;
  spawnLog = "";
  adapterClosed = false;

  constructor(kind: "pi" | "oc") {
    this.kind = kind;
  }

  // Everything before the adapter loads; the caller rolls back with stop() when this or the adapter throws.
  async prepare(opts: RigOptions, defaults: RelayEnv): Promise<void> {
    const home = new TempHome({ long: opts.long, runLength: opts.runLength });
    this.tempHome = home;
    let env: RelayEnv = { ...defaults, ...opts.env, ...opts.setup?.(home) };
    if (opts.install) home.writeInstallRecord(opts.install);
    if (opts.recordingBin === "install.json") {
      const { bin, spawnLog } = home.writeRecordingBin("fake-mz-relay");
      home.writeInstallRecord({ bin, version: "0.1.0" });
      this.spawnLog = spawnLog;
    } else if (opts.recordingBin === "PATH") {
      const { bin, spawnLog } = home.writeRecordingBin("mz-relay");
      env = { PATH: `${bin.slice(0, bin.lastIndexOf("/"))}:${process.env.PATH ?? ""}`, ...env };
      this.spawnLog = spawnLog;
    }
    home.applyEnv(env);
    if (opts.relayd ?? true) await this.startRelayd();
  }

  get home(): TempHome {
    if (!this.tempHome) throw new Error("the rig has no temp home");
    return this.tempHome;
  }

  get socket(): string {
    return this.home.adapterSocket(this.kind);
  }

  get fakeRelayd(): FakeRelayd {
    if (!this.relayd) throw new Error("this rig runs without a fake relayd");
    return this.relayd;
  }

  async startRelayd(): Promise<FakeRelayd> {
    this.relayd = await FakeRelayd.start(this.home.run);
    return this.relayd;
  }

  async closeRelayd(): Promise<void> {
    const relayd = this.relayd;
    this.relayd = null;
    await relayd?.close();
  }

  async listening(): Promise<void> {
    await waitUntil(() => socketAccepts(this.socket), `the adapter socket ${this.socket}`);
  }

  // argv lines the recording bin was run with, oldest first.
  spawned(): string[] {
    if (!this.spawnLog || !existsSync(this.spawnLog)) return [];
    return readFileSync(this.spawnLog, "utf8").split("\n").filter(Boolean);
  }

  // Overridden by each harness rig: session_shutdown for Pi, dispose for OpenCode.
  async closeAdapter(): Promise<void> {}

  async closeAdapterOnce(): Promise<void> {
    if (this.adapterClosed) return;
    this.adapterClosed = true;
    await this.closeAdapter();
  }

  async stop(): Promise<void> {
    try {
      await this.closeAdapterOnce();
    } finally {
      await this.closeRelayd();
      this.tempHome?.dispose();
      this.tempHome = null;
    }
  }
}

// A setup that swaps <run> for a symlink to target, e.g. a root-owned dir standing in for another user's.
export function runDirLinkedTo(target: string): (home: TempHome) => void {
  return (home) => {
    rmSync(home.run, { recursive: true, force: true });
    symlinkSync(target, home.run);
  };
}

// One rig per test in this file, stopped after each test even when the test failed.
export function useRig<TRig extends { stop(): Promise<void> }, TOpts>(
  start: (opts?: TOpts) => Promise<TRig>,
): (opts?: TOpts) => Promise<TRig> {
  let current: TRig | null = null;
  afterEach(async () => {
    const rig = current;
    current = null;
    await rig?.stop();
  }, TEST_TIMEOUT);
  return async (opts?: TOpts) => {
    current = await start(opts);
    return current;
  };
}
