// Pi extension core: presence and reconnect, the adapter socket (deliver and seen), logging and shutdown.
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, test } from "node:test";

import { FAKE_TOKEN, FakeRelayd } from "../testing/fake_relayd.ts";
import { FakePi, piMessage, PiRig, type PiRigOptions } from "../testing/fake_pi.ts";
import { runDirLinkedTo, TEST_TIMEOUT, useRig } from "../testing/rig.ts";
import { AdapterClient, countLines, delay, exchange, socketAccepts, TempHome, waitUntil } from "../testing/support.ts";
import mzRelay from "./mz_relay.ts";

const startRig = useRig((opts?: PiRigOptions) => PiRig.start(mzRelay, opts));
// sun_path holds 107 bytes plus the trailing NUL.
const SUN_PATH_MAX = 107;
const SECRET_WORDS = "zebra-quartz-4471";

describe("module shape", () => {
  test("first line is the install marker and the default export is the factory", () => {
    const source = readFileSync(new URL("./mz_relay.ts", import.meta.url), "utf8");
    assert.equal(source.split("\n")[0], "// mz-relay adapter for Pi");
    assert.equal(typeof mzRelay, "function");
  });
});

describe("presence", () => {
  test("session_start sends hello then register on one kept-open connection", TEST_TIMEOUT, async () => {
    const r = await startRig();
    const relayd = r.fakeRelayd;
    const register = await relayd.waitFor("register");
    const hello = await relayd.waitFor("hello");
    assert.equal(hello.conn, register.conn);
    assert.ok(relayd.frames.indexOf(hello) < relayd.frames.indexOf(register), "hello comes first");
    assert.deepEqual(hello.args, { protocol: 1, client: "pi", pid: process.pid });
    assert.equal(register.args.name, "scout");
    assert.equal(register.args.harness, "pi");
    assert.equal(register.args.pid, process.pid);
    assert.equal(register.args.cwd, process.cwd());
    assert.deepEqual(register.args.delivery, { socket: r.socket, pid: process.pid });
    await delay(200);
    assert.ok(relayd.isOpen(register.conn), "the presence connection stays open after register");
  });

  test("register sends name null when MZ_RELAY_NAME is unset", TEST_TIMEOUT, async () => {
    const r = await startRig({ env: { MZ_RELAY_NAME: undefined } });
    const register = await r.fakeRelayd.waitFor("register");
    assert.equal(register.args.name, null);
  });

  test("relayd coming up 100 ms late still gets the register", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false });
    await delay(100);
    const register = await (await r.startRelayd()).waitFor("register", 3000);
    assert.equal(register.args.harness, "pi");
  });

  test("a dropped presence connection is re-established with hello and register", TEST_TIMEOUT, async () => {
    const r = await startRig();
    const relayd = r.fakeRelayd;
    const first = await relayd.waitFor("register");
    relayd.dropConnection(first.conn);
    const second = await relayd.waitFor("register", 3000, (f) => f.conn !== first.conn);
    const hello = relayd.framesOf("hello").find((f) => f.conn === second.conn);
    assert.ok(hello, "the new connection starts with hello");
  });

  test("install.json's bin is spawned with daemon --ensure once across many retries", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false, recordingBin: "install.json", env: { MZ_RELAY_RECONNECT_MS: "20" } });
    await waitUntil(() => r.spawned().length > 0, "the bin to be spawned");
    await delay(300);
    await r.startRelayd();
    // The default schedule's next dial would be 700 ms away; a register this fast proves the 20 ms retries kept running.
    await r.fakeRelayd.waitFor("register", 250);
    assert.deepEqual(r.spawned(), ["daemon --ensure"]);
  });

  for (const [what, install] of [
    ["no install.json", undefined],
    ["an install.json without bin", { version: "0.1.0" }],
  ] as const) {
    test(`with ${what} nothing is spawned, not even mz-relay from PATH`, TEST_TIMEOUT, async () => {
      const r = await startRig({ relayd: false, recordingBin: "PATH", install, env: { MZ_RELAY_RECONNECT_MS: "20" } });
      await delay(300);
      await r.startRelayd();
      // The retry loop reached relayd, so any spawn before the first retry has happened by now.
      await r.fakeRelayd.waitFor("register", 2000);
      assert.deepEqual(r.spawned(), []);
    });
  }

  test("without MZ_RELAY_HOME the sockets follow XDG_RUNTIME_DIR", TEST_TIMEOUT, async () => {
    const home = new TempHome();
    const run = join(home.root, "xdg-run", "mz-relay");
    mkdirSync(run, { recursive: true, mode: 0o700 });
    home.applyEnv({
      MZ_RELAY_HOME: undefined,
      MZ_RELAY_NAME: "scout",
      XDG_RUNTIME_DIR: join(home.root, "xdg-run"),
      XDG_CONFIG_HOME: join(home.root, "xdg-config"),
      XDG_STATE_HOME: join(home.root, "xdg-state"),
    });
    const relayd = await FakeRelayd.start(run);
    const pi = new FakePi();
    try {
      mzRelay(pi);
      await pi.sessionStart();
      const register = await relayd.waitFor("register");
      assert.deepEqual(register.args.delivery, { socket: join(run, `pi-${process.pid}.sock`), pid: process.pid });
    } finally {
      await pi.sessionShutdown();
      await relayd.close();
      home.dispose();
    }
  });
});

describe("adapter socket", () => {
  test("session_start listens on <run>/pi-<pid>.sock", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    assert.equal(r.socket, join(r.home.home, "run", `pi-${process.pid}.sock`));
  });

  test("a stale file at the socket path is replaced", TEST_TIMEOUT, async () => {
    const r = await startRig({ sessionStart: false });
    writeFileSync(r.socket, "stale");
    await r.pi.sessionStart();
    await r.listening();
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "m1" }), { unseen: "m1" });
  });

  test("a read_only deliver hands the text to sendMessage as a followUp and acks handed_off", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    const reply = await exchange(r.socket, { op: "deliver", id: "m1", text: "[mz-relay] hi", gate: "read_only" });
    assert.deepEqual(reply, { ack: "m1", state: "handed_off" });
    assert.deepEqual(r.pi.sent, [
      {
        msg: {
          customType: "mz-relay",
          content: "[mz-relay] hi",
          display: true,
          details: { mzRelayId: "m1", gate: "read_only" },
        },
        opts: { triggerTurn: true, deliverAs: "followUp" },
      },
    ]);
  });

  test("a full deliver carries gate full in details", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    await exchange(r.socket, { op: "deliver", id: "m2", text: "t", gate: "full" });
    assert.deepEqual(r.pi.sent[0]?.msg.details, { mzRelayId: "m2", gate: "full" });
  });

  test("a throwing sendMessage becomes a nack with String(error)", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    r.pi.sendError = new Error("boom");
    const reply = await exchange(r.socket, { op: "deliver", id: "m1", text: "t", gate: "full" });
    assert.deepEqual(reply, { nack: "m1", error: "Error: boom" });
  });

  test("seen answers unseen before a context event and seen after one holds the message", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "m1" }), { unseen: "m1" });
    await r.pi.context([piMessage.user(), piMessage.assistant(), piMessage.peer("m1", "full")]);
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "m1" }), { seen: "m1" });
  });

  test("the message the adapter handed to Pi is seen once Pi replays it in context", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    await exchange(r.socket, { op: "deliver", id: "m1", text: "[mz-relay] hi", gate: "read_only" });
    const sent = r.pi.sent[0];
    assert.ok(sent, "sendMessage was called");
    await r.pi.context([piMessage.user(), { role: "custom", ...sent.msg }]);
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "m1" }), { seen: "m1" });
  });

  test("ids on custom messages of another customType are not seen", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    await r.pi.context([piMessage.user(), piMessage.otherCustom("m9")]);
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "m9" }), { unseen: "m9" });
  });

  test("seen keeps only the newest 1000 ids", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    const peers = Array.from({ length: 1001 }, (_, i) => piMessage.peer(`p${i}`, "full"));
    await r.pi.context([piMessage.user(), ...peers]);
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "p0" }), { unseen: "p0" });
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "p1" }), { seen: "p1" });
    assert.deepEqual(await exchange(r.socket, { op: "seen", id: "p1000" }), { seen: "p1000" });
  });

  test("an unknown op is nacked with its id, or an empty id when it has none", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    assert.deepEqual(await exchange(r.socket, { op: "bogus", id: "m7" }), { nack: "m7", error: "unknown op" });
    assert.deepEqual(await exchange(r.socket, { op: "bogus" }), { nack: "", error: "unknown op" });
  });

  test("a partial line over 1 MiB in UTF-8 but under it in UTF-16 closes the connection", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    const client = await AdapterClient.connect(r.socket);
    try {
      // 600 000 two-byte chars: 1.2 MB on the wire, 600 000 UTF-16 code units.
      client.write("é".repeat(600_000));
      await waitUntil(() => client.ended, "the adapter to close the connection");
    } finally {
      client.close();
    }
    assert.ok(r.home.readLog("pi").includes("a line over 1048576 bytes"), "the oversized line is logged");
  });

  test("a malformed line gets no reply and the connection stays usable", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    const client = await AdapterClient.connect(r.socket);
    try {
      client.write("{not json\n");
      client.write({ op: "seen", id: "m1" });
      assert.deepEqual(await client.next(), { unseen: "m1" });
    } finally {
      client.close();
    }
  });
});

describe("failures, logging and shutdown", () => {
  test("a socket path over the sun_path limit is logged as a listen error, never thrown into Pi", TEST_TIMEOUT, async () => {
    const r = await startRig({ long: true, relayd: false });
    assert.ok(r.socket.length > SUN_PATH_MAX, "the rig really built a socket path past sun_path");
    await waitUntil(() => r.home.readLog("pi").includes("listen error"), "a listen error in the adapter log");
  });

  test("the adapter log never holds a message body or the relayd token", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.waitRegistered();
    await r.listening();
    const text = `[mz-relay] Peer message\n--- message ---\n  ${SECRET_WORDS}\n--- end ---`;
    const client = await AdapterClient.connect(r.socket);
    try {
      client.write("{not json\n");
      client.write({ op: "deliver", id: "m1", text, gate: "full" });
      assert.deepEqual(await client.next(), { ack: "m1", state: "handed_off" });
    } finally {
      client.close();
    }
    r.pi.sendError = new Error("boom");
    await exchange(r.socket, { op: "deliver", id: "m2", text, gate: "read_only" });
    await waitUntil(() => r.home.readLog("pi").length > 0, "the malformed line to be logged");
    const log = r.home.readLog("pi");
    assert.equal(log.includes(SECRET_WORDS), false, "no message body in the log");
    assert.equal(log.includes(FAKE_TOKEN), false, "no token in the log");
  });

  test("a session_shutdown while the socket is still opening leaves no socket and no presence", TEST_TIMEOUT, async () => {
    const r = await startRig({ sessionStart: false });
    // session_start runs up to its listen await, so the shutdown lands mid-listen.
    const starting = r.pi.sessionStart();
    await r.shutdown();
    await starting;
    await delay(300);
    assert.equal(await socketAccepts(r.socket), false, "no socket listening after shutdown");
    assert.equal(existsSync(r.socket), false, "no socket file left behind");
    assert.equal(r.fakeRelayd.framesOf("register").length, 0, "no presence after shutdown");
  });

  test("session_shutdown removes the socket, closes presence and stops reconnecting", TEST_TIMEOUT, async () => {
    const r = await startRig();
    const relayd = r.fakeRelayd;
    await r.listening();
    const register = await relayd.waitFor("register");
    await r.shutdown();
    await waitUntil(() => !existsSync(r.socket), "the adapter socket to be removed");
    await waitUntil(() => !relayd.isOpen(register.conn), "the presence connection to close");
    await delay(300);
    assert.equal(relayd.framesOf("register").length, 1, "no reconnect after shutdown");
    assert.equal(await socketAccepts(r.socket), false);
  });
});

describe("runtime dir ownership", () => {
  test("a runtime dir owned by another user disables the extension without writing into it", TEST_TIMEOUT, async (t) => {
    // /tmp is root-owned and world-writable: the dir another local user could have planted first.
    const planted = [`/tmp/pi-${process.pid}.sock`, `/tmp/pi-${process.pid}.log`];
    for (const path of planted) rmSync(path, { force: true });
    const errors = t.mock.method(console, "error", () => {});
    let r: PiRig | null = null;
    try {
      r = await startRig({
        relayd: false,
        recordingBin: "install.json",
        setup: runDirLinkedTo("/tmp"),
        env: { MZ_RELAY_RECONNECT_MS: "20" },
      });
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
    const register = await r.fakeRelayd.waitFor("register");
    assert.equal(statSync(r.home.run).mode & 0o777, 0o700);
    assert.deepEqual(register.args.delivery, { socket: r.socket, pid: process.pid });
  });
});

describe("retries, spawn and logging after review", () => {
  test("a refused register is retried, logged once per error code until a register succeeds", TEST_TIMEOUT, async () => {
    const r = await startRig({ sessionStart: false, env: { MZ_RELAY_RECONNECT_MS: "20" } });
    const relayd = r.fakeRelayd;
    const registers = (): number => relayd.framesOf("register").length;
    relayd.refuse("register", "unknown_agent");
    await r.pi.sessionStart();
    await waitUntil(() => registers() >= 5, "five refused registers");
    relayd.refuse("register", "identity_ambiguous");
    const switched = registers();
    await waitUntil(() => registers() >= switched + 5, "five more refused registers");
    assert.equal(countLines(r.home.readLog("pi"), "unknown_agent"), 1);
    assert.equal(countLines(r.home.readLog("pi"), "identity_ambiguous"), 1);

    relayd.refuse("register", null);
    await r.waitRegistered();
    relayd.refuse("register", "unknown_agent");
    const presence = relayd.framesOf("register").at(-1);
    assert.ok(presence);
    relayd.dropConnection(presence.conn);
    const dropped = registers();
    await waitUntil(() => registers() >= dropped + 3, "three refused registers after the success");
    assert.equal(countLines(r.home.readLog("pi"), "unknown_agent"), 2, "a success resets the once-per-code log");
  });

  test("each relayd outage spawns the daemon once more", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false, recordingBin: "install.json", env: { MZ_RELAY_RECONNECT_MS: "20" } });
    await waitUntil(() => r.spawned().length === 1, "the first outage's spawn");
    await r.startRelayd();
    await r.waitRegistered();
    await r.closeRelayd();
    await waitUntil(() => r.spawned().length >= 2, "a spawn for the second outage");
    await delay(300);
    assert.deepEqual(r.spawned(), ["daemon --ensure", "daemon --ensure"]);
  });

  test("a sendMessage failure is logged by error name, never its message", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    r.pi.sendError = Object.assign(new Error("quokka-detail-58"), { name: "PiStateError" });
    const reply = await exchange(r.socket, { op: "deliver", id: "m1", text: "t", gate: "full" });
    assert.deepEqual(reply, { nack: "m1", error: "PiStateError: quokka-detail-58" });
    const log = r.home.readLog("pi");
    assert.ok(
      log.split("\n").some((line) => line.includes("m1") && line.includes("PiStateError")),
      "the failure is logged with the id and the error name",
    );
    assert.equal(log.includes("quokka-detail-58"), false);
  });

  test("the adapter log is created owner-only (0600)", TEST_TIMEOUT, async () => {
    const umask = process.umask(0o022);
    try {
      const r = await startRig();
      await r.listening();
      await waitUntil(() => existsSync(r.home.adapterLog("pi")), "the adapter log");
      assert.equal(statSync(r.home.adapterLog("pi")).mode & 0o777, 0o600);
    } finally {
      process.umask(umask);
    }
  });
});
