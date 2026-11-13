// Pi native tools mz_relay_send|inbox|ack|peers: schemas, per-call relayd connections, verbatim texts.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { FAKE_TOKEN, type Frame } from "../testing/fake_relayd.ts";
import { PiRig, type PiRigOptions } from "../testing/fake_pi.ts";
import { TEST_TIMEOUT, useRig } from "../testing/rig.ts";
import mzRelay from "./mz_relay.ts";

const startRig = useRig((opts?: PiRigOptions) => PiRig.start(mzRelay, opts));
const TOOL_NAMES = ["mz_relay_ack", "mz_relay_inbox", "mz_relay_peers", "mz_relay_send"];
const NOT_CONNECTED = "mz-relay error daemon_unavailable: not connected to relayd yet";

// A registered extension whose relayd answers `op` with `text`.
async function registeredRig(op: string, text: string): Promise<PiRig> {
  const r = await startRig();
  r.fakeRelayd.onOp(op, (args) => ({
    result: { id: args.id ?? "m1", status: "queued", recipient_online: true, notices_pending: 0, text },
  }));
  await r.waitRegistered();
  return r;
}

function textResult(text: string): { content: { type: string; text: string }[]; details: Record<string, unknown> } {
  return { content: [{ type: "text", text }], details: {} };
}

function helloOn(r: PiRig, frame: Frame): Frame | undefined {
  return r.fakeRelayd.framesOf("hello").find((f) => f.conn === frame.conn);
}

describe("tool registration", () => {
  test("the four tools are registered at factory time, before session_start", TEST_TIMEOUT, async () => {
    const r = await startRig({ sessionStart: false, relayd: false });
    assert.deepEqual(r.pi.tools.map((t) => t.name).sort(), TOOL_NAMES);
    for (const tool of r.pi.tools) {
      assert.ok(tool.label.length > 0, `${tool.name} has a label`);
      assert.ok(tool.description.length > 0, `${tool.name} has a description`);
      assert.equal(tool.parameters.type, "object");
      assert.equal(tool.parameters.additionalProperties, false, `${tool.name} rejects extra properties`);
    }
  });

  test("schemas match the MCP tools: required fields, body limit, kind enum, inbox limit range", TEST_TIMEOUT, async () => {
    const r = await startRig({ sessionStart: false, relayd: false });
    const props = (name: string): Record<string, Record<string, unknown>> =>
      r.pi.tool(name).parameters.properties as Record<string, Record<string, unknown>>;
    const send = r.pi.tool("mz_relay_send").parameters;
    assert.deepEqual([...(send.required as string[])].sort(), ["body", "to"]);
    assert.deepEqual(Object.keys(props("mz_relay_send")).sort(), ["body", "context_id", "in_reply_to", "kind", "to"]);
    assert.equal(props("mz_relay_send").body?.maxLength, 32000);
    assert.deepEqual(props("mz_relay_send").kind?.enum, ["request", "inform", "reply", "done"]);
    assert.equal(props("mz_relay_inbox").limit?.type, "integer");
    assert.equal(props("mz_relay_inbox").limit?.minimum, 1);
    assert.equal(props("mz_relay_inbox").limit?.maximum, 50);
    assert.deepEqual(r.pi.tool("mz_relay_ack").parameters.required, ["id"]);
    assert.deepEqual(props("mz_relay_peers"), {});
  });
});

describe("tool calls", () => {
  test("before registration every tool returns the not-connected error text without throwing", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false });
    const params: Record<string, Record<string, unknown>> = {
      mz_relay_send: { to: "architect", body: "hi" },
      mz_relay_inbox: {},
      mz_relay_ack: { id: "m1" },
      mz_relay_peers: {},
    };
    for (const name of TOOL_NAMES) {
      assert.deepEqual(await r.pi.tool(name).execute("call-1", params[name] ?? {}), textResult(NOT_CONNECTED));
    }
  });

  test("send goes over its own hello'd connection with the token and a fresh id", TEST_TIMEOUT, async () => {
    const r = await registeredRig("send", "queued m1 for architect");
    const result = await r.pi.tool("mz_relay_send").execute("call-1", { to: "architect", body: "please review" });
    assert.deepEqual(result, textResult("queued m1 for architect"));
    const send = await r.fakeRelayd.waitFor("send");
    const register = await r.fakeRelayd.waitFor("register");
    assert.notEqual(send.conn, register.conn, "a tool call does not reuse the presence connection");
    assert.equal(helloOn(r, send)?.args.client, "pi");
    assert.equal(send.args.token, FAKE_TOKEN);
    assert.equal(send.args.to, "architect");
    assert.equal(send.args.body, "please review");
    assert.equal(send.args.kind, "request");
    assert.match(String(send.args.id), /^[A-Za-z0-9-]{8,64}$/);
  });

  test("send passes kind, in_reply_to and context_id when given", TEST_TIMEOUT, async () => {
    const r = await registeredRig("send", "queued m2 for architect");
    await r.pi.tool("mz_relay_send").execute("call-1", {
      to: "architect",
      body: "done",
      kind: "reply",
      in_reply_to: "parent-0001",
      context_id: "ctx-00001",
    });
    const send = await r.fakeRelayd.waitFor("send");
    assert.equal(send.args.kind, "reply");
    assert.equal(send.args.in_reply_to, "parent-0001");
    assert.equal(send.args.context_id, "ctx-00001");
  });

  test("two sends draw two different ids", TEST_TIMEOUT, async () => {
    const r = await registeredRig("send", "queued");
    await r.pi.tool("mz_relay_send").execute("call-1", { to: "architect", body: "a" });
    await r.pi.tool("mz_relay_send").execute("call-2", { to: "architect", body: "b" });
    const ids = r.fakeRelayd.framesOf("send").map((f) => f.args.id);
    assert.equal(ids.length, 2);
    assert.notEqual(ids[0], ids[1]);
  });

  test("a relayd business error becomes the mz-relay error text", TEST_TIMEOUT, async () => {
    const r = await startRig();
    r.fakeRelayd.onOp("send", () => ({
      error: { code: "not_allowed", message: "scout may not message architect" },
    }));
    await r.waitRegistered();
    const result = await r.pi.tool("mz_relay_send").execute("call-1", { to: "architect", body: "hi" });
    assert.deepEqual(result, textResult("mz-relay error not_allowed: scout may not message architect"));
  });

  test("inbox passes limit with the token and shows relayd's text verbatim", TEST_TIMEOUT, async () => {
    const r = await registeredRig("inbox", "no new messages\n\nsent:\n- m1 → architect: queued");
    const result = await r.pi.tool("mz_relay_inbox").execute("call-1", { limit: 5 });
    assert.deepEqual(result, textResult("no new messages\n\nsent:\n- m1 → architect: queued"));
    const inbox = await r.fakeRelayd.waitFor("inbox");
    assert.equal(inbox.args.limit, 5);
    assert.equal(inbox.args.token, FAKE_TOKEN);
  });

  test("ack passes the id with the token", TEST_TIMEOUT, async () => {
    const r = await registeredRig("ack", "acked m1");
    assert.deepEqual(await r.pi.tool("mz_relay_ack").execute("call-1", { id: "m1" }), textResult("acked m1"));
    const ack = await r.fakeRelayd.waitFor("ack");
    assert.equal(ack.args.id, "m1");
    assert.equal(ack.args.token, FAKE_TOKEN);
  });

  test("peers passes the token and shows relayd's text", TEST_TIMEOUT, async () => {
    const r = await registeredRig("peers", "architect : architect — Designs the system [online]");
    const result = await r.pi.tool("mz_relay_peers").execute("call-1", {});
    assert.deepEqual(result, textResult("architect : architect — Designs the system [online]"));
    assert.equal((await r.fakeRelayd.waitFor("peers")).args.token, FAKE_TOKEN);
  });

  test("a call while relayd is gone returns an error text instead of throwing", TEST_TIMEOUT, async () => {
    const r = await registeredRig("peers", "unused");
    await r.closeRelayd();
    const result = await r.pi.tool("mz_relay_peers").execute("call-1", {});
    assert.equal(result.content.length, 1);
    assert.match(result.content[0]?.text ?? "", /^mz-relay error /);
    assert.deepEqual(result.details, {});
  });
});
