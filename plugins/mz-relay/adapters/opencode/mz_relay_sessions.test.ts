// OpenCode lazy registration and daemon spawn, child-session tracking, promptAsync delivery and logging.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { FAKE_TOKEN } from "../testing/fake_relayd.ts";
import { type OcPlugin, OcRig, sessionEvent } from "../testing/fake_opencode.ts";
import { type RigOptions, TEST_TIMEOUT, useRig } from "../testing/rig.ts";
import { AdapterClient, countLines, delay, exchange, waitUntil } from "../testing/support.ts";
import { MzRelayPlugin } from "./mz_relay.ts";

const SECRET_WORDS = "zebra-quartz-4471";

// Every test here starts from a plugin whose adapter socket already accepts.
const startRig = useRig(async (opts?: RigOptions) => {
  const rig = await OcRig.start(MzRelayPlugin as unknown as OcPlugin, opts);
  try {
    await rig.listening();
  } catch (err) {
    await rig.stop();
    throw err;
  }
  return rig;
});

function deliver(r: OcRig, gate: "full" | "read_only", id = "m1"): Promise<Record<string, unknown>> {
  return exchange(r.socket, { op: "deliver", id, text: `[mz-relay] ${id}`, gate });
}

// Delivers once and returns the session id promptAsync was called with.
async function deliveryTarget(r: OcRig): Promise<string | undefined> {
  const before = r.client.calls.length;
  const reply = await deliver(r, "full");
  assert.deepEqual(reply, { ack: "m1", state: "delivered" });
  assert.equal(r.client.calls.length, before + 1);
  return r.client.calls.at(-1)?.path.id;
}

describe("lazy registration", () => {
  test("nothing registers before the first chat message", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.configured();
    await r.plugin.event(sessionEvent("session.created", "s1"));
    await delay(200);
    assert.deepEqual(r.fakeRelayd.framesOf("register"), []);
  });

  test("the first chat message registers as opencode with the oc socket binding", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    const register = await r.fakeRelayd.waitFor("register");
    const hello = r.fakeRelayd.framesOf("hello").find((f) => f.conn === register.conn);
    assert.deepEqual(hello?.args, { protocol: 1, client: "opencode", pid: process.pid });
    assert.equal(register.args.harness, "opencode");
    assert.equal(register.args.name, "helper");
    assert.equal(register.args.pid, process.pid);
    assert.equal(typeof register.args.cwd, "string");
    assert.deepEqual(register.args.delivery, { socket: r.home.adapterSocket("oc"), pid: process.pid });
  });

  test("later chat messages do not register again", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.fakeRelayd.waitFor("register");
    await r.chat("s2");
    await r.chat("s1");
    await delay(200);
    assert.equal(r.fakeRelayd.framesOf("register").length, 1);
  });

  test("a chat message from a child session does not start registration", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.plugin.event(sessionEvent("session.created", "c1", "s1"));
    await r.chat("c1");
    await delay(200);
    assert.deepEqual(r.fakeRelayd.framesOf("register"), []);
    await r.chat("s1");
    await r.fakeRelayd.waitFor("register");
  });

  test("relayd coming up late still gets the register", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false });
    await r.chat("s1");
    await delay(100);
    const register = await (await r.startRelayd()).waitFor("register", 3000);
    assert.equal(register.args.harness, "opencode");
  });

  test("a dropped presence connection is re-registered", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    const first = await r.fakeRelayd.waitFor("register");
    r.fakeRelayd.dropConnection(first.conn);
    await r.fakeRelayd.waitFor("register", 3000, (f) => f.conn !== first.conn);
  });

  test("dispose closes the presence connection and stops reconnecting", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    const register = await r.fakeRelayd.waitFor("register");
    await r.dispose();
    await waitUntil(() => !r.fakeRelayd.isOpen(register.conn), "the presence connection to close");
    await delay(300);
    assert.equal(r.fakeRelayd.framesOf("register").length, 1);
  });

  test("install.json's bin is spawned with daemon --ensure once across many retries", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false, recordingBin: "install.json", env: { MZ_RELAY_RECONNECT_MS: "20" } });
    await r.chat("s1");
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
      await r.chat("s1");
      await delay(300);
      await r.startRelayd();
      // The retry loop reached relayd, so any spawn before the first retry has happened by now.
      await r.fakeRelayd.waitFor("register", 2000);
      assert.deepEqual(r.spawned(), []);
    });
  }
});

describe("delivery", () => {
  test("a read_only deliver prompts the session under the peer agent, never with a tools map", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    const reply = await deliver(r, "read_only");
    assert.deepEqual(reply, { ack: "m1", state: "delivered" });
    assert.deepEqual(r.client.calls, [
      {
        path: { id: "s1" },
        body: { parts: [{ type: "text", text: "[mz-relay] m1" }], agent: "mz-relay-peer" },
      },
    ]);
    assert.equal("tools" in (r.client.calls[0]?.body ?? {}), false);
  });

  test("a full deliver prompts without an agent", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await deliver(r, "full");
    const body = r.client.calls[0]?.body ?? { parts: [] };
    assert.equal("agent" in body, false);
    assert.equal("tools" in body, false);
    assert.deepEqual(body.parts, [{ type: "text", text: "[mz-relay] m1" }]);
  });

  test("a deliver before any chat is nacked", TEST_TIMEOUT, async () => {
    const r = await startRig();
    assert.deepEqual(await deliver(r, "full"), { nack: "m1", error: "no active OpenCode session yet" });
    assert.equal(r.client.calls.length, 0);
  });

  test("a promptAsync error result is nacked with its name only, never the data it echoes", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    // A validation error may echo the request, and the nack text ends up in relayd.log.
    r.client.outcome = { error: { name: "BadRequest", data: { received: SECRET_WORDS } } };
    const reply = await deliver(r, "full");
    assert.equal(reply.nack, "m1");
    assert.equal("ack" in reply, false);
    assert.equal(reply.error, "promptAsync error: BadRequest");
  });

  test("a promptAsync status of 300 or more is nacked", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    r.client.outcome = { response: { status: 500 } };
    const reply = await deliver(r, "full");
    assert.equal(reply.nack, "m1");
    assert.match(String(reply.error), /500/);
  });

  test("a rejected promptAsync is nacked, not thrown", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    r.client.outcome = { reject: new Error(`server gone ${SECRET_WORDS}`) };
    const reply = await deliver(r, "full");
    assert.equal(reply.nack, "m1");
    assert.equal(reply.error, "promptAsync rejected: Error");
  });

  test("every adapter log line carries the pid and a level", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.fakeRelayd.waitFor("register");
    r.client.outcome = { error: { name: "BadRequest" } };
    await deliver(r, "full");
    const lines = r.home.readLog("oc").split("\n").filter(Boolean);
    assert.ok(lines.length >= 2, "the registration and the nack are logged");
    const prefix = new RegExp(`^\\S+ mz-relay-oc\\[${process.pid}\\] (INFO|WARNING|ERROR) `);
    for (const line of lines) assert.match(line, prefix);
    assert.ok(lines.some((line) => / WARNING deliver m1 nacked/.test(line)), "a nack is a warning");
  });
});

describe("session tracking", () => {
  test("the latest top-level chat session is the target", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.chat("s2");
    assert.equal(await deliveryTarget(r), "s2");
  });

  test("a child announced by session.created before its chat message is never the target", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.plugin.event(sessionEvent("session.created", "c1", "s1"));
    await r.chat("c1");
    assert.equal(await deliveryTarget(r), "s1");
  });

  test("a child whose chat message came before session.created reverts the target to its parent", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.chat("c1");
    await r.plugin.event(sessionEvent("session.created", "c1", "s1"));
    assert.equal(await deliveryTarget(r), "s1");
  });

  test("session.updated with a parentID marks a child the same way, in either order", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.plugin.event(sessionEvent("session.updated", "c1", "s1"));
    await r.chat("c1");
    assert.equal(await deliveryTarget(r), "s1");
    await r.chat("c2");
    await r.plugin.event(sessionEvent("session.updated", "c2", "s1"));
    assert.equal(await deliveryTarget(r), "s1");
  });

  test("a session event without parentID does not stop that session becoming the target", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.plugin.event(sessionEvent("session.created", "s2"));
    await r.chat("s2");
    assert.equal(await deliveryTarget(r), "s2");
  });

  test("unrelated events leave the target alone", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.plugin.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } });
    await r.plugin.event({ event: { type: "message.updated" } });
    assert.equal(await deliveryTarget(r), "s1");
  });
});

describe("logging", () => {
  test("the adapter log never holds a message body or the relayd token", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.chat("s1");
    await r.fakeRelayd.waitFor("register");
    const text = `[mz-relay] Peer message\n--- message ---\n  ${SECRET_WORDS}\n--- end ---`;
    const client = await AdapterClient.connect(r.socket);
    try {
      client.write("{not json\n");
      client.write({ op: "deliver", id: "m1", text, gate: "read_only" });
      assert.deepEqual(await client.next(), { ack: "m1", state: "delivered" });
    } finally {
      client.close();
    }
    r.client.outcome = { error: { name: "BadRequest" } };
    assert.equal((await exchange(r.socket, { op: "deliver", id: "m2", text, gate: "full" })).nack, "m2");
    r.client.outcome = { reject: new Error("server gone") };
    assert.equal((await exchange(r.socket, { op: "deliver", id: "m3", text, gate: "full" })).nack, "m3");
    // Logging is synchronous in practice; the pause covers an adapter that appends asynchronously.
    await delay(100);
    const log = r.home.readLog("oc");
    assert.equal(log.includes(SECRET_WORDS), false, "no message body in the log");
    assert.equal(log.includes(FAKE_TOKEN), false, "no token in the log");
  });
});

describe("retries and spawn after review", () => {
  test("a refused register is retried, logged once per error code until a register succeeds", TEST_TIMEOUT, async () => {
    const r = await startRig({ env: { MZ_RELAY_RECONNECT_MS: "20" } });
    const relayd = r.fakeRelayd;
    const registers = (): number => relayd.framesOf("register").length;
    relayd.refuse("register", "unknown_agent");
    await r.chat("s1");
    await waitUntil(() => registers() >= 5, "five refused registers");
    relayd.refuse("register", "identity_ambiguous");
    const switched = registers();
    await waitUntil(() => registers() >= switched + 5, "five more refused registers");
    assert.equal(countLines(r.home.readLog("oc"), "unknown_agent"), 1);
    assert.equal(countLines(r.home.readLog("oc"), "identity_ambiguous"), 1);

    relayd.refuse("register", null);
    const accepted = registers();
    await waitUntil(() => registers() > accepted, "an accepted register");
    const presence = relayd.framesOf("register").at(-1);
    assert.ok(presence);
    await delay(100);
    assert.ok(relayd.isOpen(presence.conn), "the accepted register keeps its connection");
    relayd.refuse("register", "unknown_agent");
    relayd.dropConnection(presence.conn);
    const dropped = registers();
    await waitUntil(() => registers() >= dropped + 3, "three refused registers after the success");
    assert.equal(countLines(r.home.readLog("oc"), "unknown_agent"), 2, "a success resets the once-per-code log");
  });

  test("a refused register does not spend the daemon spawn", TEST_TIMEOUT, async () => {
    const r = await startRig({ recordingBin: "install.json", env: { MZ_RELAY_RECONNECT_MS: "20" } });
    r.fakeRelayd.refuse("register", "unknown_agent");
    await r.chat("s1");
    await waitUntil(() => r.fakeRelayd.framesOf("register").length >= 3, "three refused registers");
    assert.deepEqual(r.spawned(), [], "relayd answered, so there was nothing to start");
    await r.closeRelayd();
    await waitUntil(() => r.spawned().length > 0, "a spawn once relayd is really gone");
    assert.deepEqual(r.spawned(), ["daemon --ensure"]);
  });

  test("each relayd outage spawns the daemon once more", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false, recordingBin: "install.json", env: { MZ_RELAY_RECONNECT_MS: "20" } });
    await r.chat("s1");
    await waitUntil(() => r.spawned().length === 1, "the first outage's spawn");
    const relayd = await r.startRelayd();
    const register = await relayd.waitFor("register");
    await delay(100);
    assert.ok(relayd.isOpen(register.conn), "registered");
    await r.closeRelayd();
    await waitUntil(() => r.spawned().length >= 2, "a spawn for the second outage");
    await delay(300);
    assert.deepEqual(r.spawned(), ["daemon --ensure", "daemon --ensure"]);
  });
});
