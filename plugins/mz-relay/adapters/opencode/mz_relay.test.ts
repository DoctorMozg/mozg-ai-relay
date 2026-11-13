// OpenCode plugin core: one export, the config hook's MCP and peer-agent entries, the socket and dispose.
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, statSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import { join } from "node:path";
import { describe, test } from "node:test";

import { FakeRelayd } from "../testing/fake_relayd.ts";
import { type OcConfig, type OcPlugin, OcRig } from "../testing/fake_opencode.ts";
import { type RigOptions, runDirLinkedTo, TEST_TIMEOUT, useRig } from "../testing/rig.ts";
import { delay, exchange, socketAccepts, waitUntil } from "../testing/support.ts";
import * as adapter from "./mz_relay.ts";

const startRig = useRig((opts?: RigOptions) => OcRig.start(adapter.MzRelayPlugin as unknown as OcPlugin, opts));
const BIN = "/opt/mz-relay/bin/mz-relay";
const INSTALLED = { bin: BIN, version: "0.1.0" };
// sun_path holds 107 bytes plus the trailing NUL.
const SUN_PATH_MAX = 107;
const PEER_AGENT = {
  description: "Answers mz-relay peer messages, read-only",
  mode: "primary",
  permission: { edit: "deny", bash: "deny", webfetch: "deny", task: "deny" },
};

describe("module shape", () => {
  test("first line is the install marker", () => {
    const source = readFileSync(new URL("./mz_relay.ts", import.meta.url), "utf8");
    assert.equal(source.split("\n")[0], "// mz-relay adapter for OpenCode");
  });

  test("the module has exactly one export, the MzRelayPlugin function", () => {
    assert.deepEqual(Object.keys(adapter), ["MzRelayPlugin"]);
    assert.equal(typeof adapter.MzRelayPlugin, "function");
  });
});

describe("config hook", () => {
  test("with install.json it injects the local MCP entry and the read-only peer agent", TEST_TIMEOUT, async () => {
    const r = await startRig({ install: INSTALLED, env: { MZ_RELAY_NAME: undefined } });
    const config = await r.configured();
    assert.deepEqual(config.mcp?.["mz-relay"], {
      type: "local",
      command: [BIN, "mcp", "--harness", "opencode"],
      environment: {},
    });
    assert.deepEqual(config.agent?.["mz-relay-peer"], PEER_AGENT);
  });

  test("MZ_RELAY_NAME is passed to the MCP server's environment", TEST_TIMEOUT, async () => {
    const r = await startRig({ install: INSTALLED, env: { MZ_RELAY_NAME: "helper" } });
    const entry = (await r.configured()).mcp?.["mz-relay"] as Record<string, unknown>;
    assert.deepEqual(entry.environment, { MZ_RELAY_NAME: "helper" });
  });

  test("entries the user defined are never overwritten", TEST_TIMEOUT, async () => {
    const r = await startRig({ install: INSTALLED });
    const userAgent = { description: "mine", mode: "subagent", permission: { edit: "allow" } };
    const userMcp = { type: "local", command: ["/usr/bin/other"] };
    const config: OcConfig = await r.configured({
      agent: { "mz-relay-peer": userAgent, build: { model: "x" } },
      mcp: { "mz-relay": userMcp },
    });
    assert.equal(config.agent?.["mz-relay-peer"], userAgent);
    assert.deepEqual(userAgent, { description: "mine", mode: "subagent", permission: { edit: "allow" } });
    assert.deepEqual(config.agent?.build, { model: "x" });
    assert.equal(config.mcp?.["mz-relay"], userMcp);
    assert.deepEqual(userMcp, { type: "local", command: ["/usr/bin/other"] });
  });

  test("without install.json no MCP entry is added, but the peer agent is", TEST_TIMEOUT, async () => {
    const r = await startRig();
    const config = await r.configured();
    assert.equal(config.mcp?.["mz-relay"], undefined);
    assert.deepEqual(config.agent?.["mz-relay-peer"], PEER_AGENT);
  });

  test("an install.json without bin adds no MCP entry", TEST_TIMEOUT, async () => {
    const r = await startRig({ install: { version: "0.1.0" } });
    assert.equal((await r.configured()).mcp?.["mz-relay"], undefined);
  });
});

describe("adapter socket", () => {
  test("init listens on <run>/oc-<pid>.sock", TEST_TIMEOUT, async () => {
    const r = await startRig();
    assert.equal(r.socket, join(r.home.home, "run", `oc-${process.pid}.sock`));
    await r.listening();
  });

  test("a deliver with no active session is nacked", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    const reply = await exchange(r.socket, { op: "deliver", id: "m1", text: "t", gate: "full" });
    assert.deepEqual(reply, { nack: "m1", error: "no active OpenCode session yet" });
    assert.equal(r.client.calls.length, 0);
  });

  test("an unknown op is nacked", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    assert.deepEqual(await exchange(r.socket, { op: "bogus", id: "m7" }), { nack: "m7", error: "unknown op" });
  });

  test("a socket path over the sun_path limit is logged as a listen error and init still resolves", TEST_TIMEOUT, async () => {
    const r = await startRig({ long: true, relayd: false });
    assert.ok(r.socket.length > SUN_PATH_MAX, "the rig really built a socket path past sun_path");
    await waitUntil(() => r.home.readLog("oc").includes("listen error"), "a listen error in the adapter log");
    assert.deepEqual((await r.configured()).agent?.["mz-relay-peer"], PEER_AGENT);
  });

  test("dispose closes the server and removes the socket", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    await r.dispose();
    await waitUntil(() => !existsSync(r.socket), "the adapter socket to be removed");
    assert.equal(await socketAccepts(r.socket), false);
  });
});

describe("pull-only registration when the socket cannot listen", () => {
  test("a directory at the socket path makes the plugin register without delivery", TEST_TIMEOUT, async () => {
    const r = await startRig({ setup: (home) => mkdirSync(home.adapterSocket("oc")) });
    await r.chat("s1");
    const register = await r.fakeRelayd.waitFor("register");
    assert.equal("delivery" in register.args, false);
  });

  test("a socket path past sun_path makes the plugin register without delivery", TEST_TIMEOUT, async () => {
    // relayd.sock still fits in 107 bytes; oc-<pid>.sock does not.
    const r = await startRig({ runLength: SUN_PATH_MAX - "/relayd.sock".length });
    assert.ok(Buffer.byteLength(r.fakeRelayd.path) <= SUN_PATH_MAX, "the fake relayd fits");
    assert.ok(Buffer.byteLength(r.socket) > SUN_PATH_MAX, "the adapter socket does not");
    await r.chat("s1");
    const register = await r.fakeRelayd.waitFor("register");
    assert.equal("delivery" in register.args, false);
  });
});

describe("never throws into OpenCode", () => {
  test("a failing os.userInfo() disables the plugin instead of rejecting its load", TEST_TIMEOUT, async (t) => {
    const userInfo = t.mock.method(os, "userInfo", () => {
      throw Object.assign(new Error("no passwd entry for this uid"), { code: "ENOENT" });
    });
    const errors = t.mock.method(console, "error", () => {});
    // Without MZ_RELAY_HOME or XDG_CONFIG_HOME the plugin needs the passwd home.
    const r = await startRig({
      relayd: false,
      setup: (home) => ({ MZ_RELAY_HOME: undefined, XDG_RUNTIME_DIR: join(home.root, "xdg") }),
    });
    assert.ok(userInfo.mock.callCount() >= 1, "the plugin looked up the passwd entry");
    await r.configured();
    await r.plugin.event({ event: { type: "session.idle" } });
    await r.chat("s1");
    assert.equal(existsSync(join(r.home.root, "xdg", "mz-relay", `oc-${process.pid}.sock`)), false);
    assert.equal(errors.mock.callCount(), 1, "the failure is reported once");
    assert.match(String(errors.mock.calls[0]?.arguments[0]), /mz-relay/);
  });

  // The server is internal, so net.createServer is wrapped to capture it; nothing about it is changed.
  test("server errors after listening are logged, never thrown", TEST_TIMEOUT, async (t) => {
    const servers: net.Server[] = [];
    const createServer = net.createServer;
    t.mock.method(net, "createServer", (...args: unknown[]) => {
      const server = (createServer as (...a: unknown[]) => net.Server).apply(net, args);
      servers.push(server);
      return server;
    });
    const r = await startRig();
    await r.listening();
    const server = servers.find((s) => s.address() === r.socket);
    assert.ok(server, "the plugin's adapter server was captured");
    const emfile = (): Error => Object.assign(new Error("accept EMFILE"), { code: "EMFILE" });
    assert.doesNotThrow(() => {
      server.emit("error", emfile());
      server.emit("error", emfile());
    });
    assert.deepEqual(await exchange(r.socket, { op: "bogus", id: "m7" }), { nack: "m7", error: "unknown op" });
    assert.ok(r.home.readLog("oc").includes("EMFILE"), "the server error is logged");
  });
});

describe("runtime dir ownership", () => {
  test("a runtime dir owned by another user disables the plugin without writing into it", TEST_TIMEOUT, async (t) => {
    // /tmp is root-owned and world-writable: the dir another local user could have planted first.
    const planted = [`/tmp/oc-${process.pid}.sock`, `/tmp/oc-${process.pid}.log`];
    for (const path of planted) rmSync(path, { force: true });
    const errors = t.mock.method(console, "error", () => {});
    let r: OcRig | null = null;
    try {
      r = await startRig({
        relayd: false,
        recordingBin: "install.json",
        setup: runDirLinkedTo("/tmp"),
        env: { MZ_RELAY_RECONNECT_MS: "20" },
      });
      await r.chat("s1");
      await delay(300);
      assert.deepEqual(planted.filter((path) => existsSync(path)), [], "no socket or log in the foreign dir");
      // A connect attempt would fail there and spawn install.json's bin.
      assert.deepEqual(r.spawned(), [], "no relayd connect was tried");
      assert.equal(errors.mock.callCount(), 1, "the refusal is reported once");
      assert.match(String(errors.mock.calls[0]?.arguments[0]), /mz-relay/);
    } finally {
      await r?.stop();
      for (const path of planted) rmSync(path, { force: true });
    }
  });

  test("a runtime dir of ours that others can write is tightened to 0700 and used", TEST_TIMEOUT, async () => {
    const r = await startRig({ setup: (home) => chmodSync(home.run, 0o777) });
    await r.listening();
    assert.equal(statSync(r.home.run).mode & 0o777, 0o700);
    await r.chat("s1");
    const register = await r.fakeRelayd.waitFor("register");
    assert.deepEqual(register.args.delivery, { socket: r.socket, pid: process.pid });
  });

  test("a relative MZ_RELAY_HOME disables the plugin like Pi", TEST_TIMEOUT, async (t) => {
    const errors = t.mock.method(console, "error", () => {});
    const relative = `mzr-relative-${process.pid}`;
    let xdgRelayd: FakeRelayd | null = null;
    try {
      const r = await startRig({
        relayd: false,
        setup: (home) => {
          mkdirSync(join(home.root, "xdg", "mz-relay"), { recursive: true, mode: 0o700 });
          return {
            MZ_RELAY_HOME: relative,
            XDG_RUNTIME_DIR: join(home.root, "xdg"),
            XDG_CONFIG_HOME: join(home.root, "xdg-config"),
            XDG_STATE_HOME: join(home.root, "xdg-state"),
          };
        },
      });
      // A plugin that fell back to the XDG paths would listen and register here.
      const xdgRun = join(r.home.root, "xdg", "mz-relay");
      xdgRelayd = await FakeRelayd.start(xdgRun);
      await r.configured();
      await r.chat("s1");
      await delay(200);
      assert.deepEqual(xdgRelayd.frames, [], "no relayd connect");
      assert.equal(existsSync(join(xdgRun, `oc-${process.pid}.sock`)), false, "no adapter socket");
      assert.equal(existsSync(relative), false, "nothing created under the relative path");
      assert.equal(errors.mock.callCount(), 1);
      assert.match(String(errors.mock.calls[0]?.arguments[0]), /MZ_RELAY_HOME/);
    } finally {
      await xdgRelayd?.close();
      rmSync(relative, { recursive: true, force: true });
    }
  });
});

describe("log file", () => {
  test("the adapter log is created owner-only (0600)", TEST_TIMEOUT, async () => {
    const umask = process.umask(0o022);
    try {
      const r = await startRig();
      await r.chat("s1");
      await waitUntil(() => existsSync(r.home.adapterLog("oc")), "the adapter log");
      assert.equal(statSync(r.home.adapterLog("oc")).mode & 0o777, 0o600);
    } finally {
      process.umask(umask);
    }
  });
});
