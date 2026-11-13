// Pi brief via before_agent_start, and the read-only tool_call gate driven by context events.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { fakeBrief } from "../testing/fake_relayd.ts";
import { type FakePi, piMessage, PiRig, type PiRigOptions } from "../testing/fake_pi.ts";
import { TEST_TIMEOUT, useRig } from "../testing/rig.ts";
import { exchange } from "../testing/support.ts";
import mzRelay from "./mz_relay.ts";

const startRig = useRig((opts?: PiRigOptions) => PiRig.start(mzRelay, opts));
const READ_ONLY_ALLOWED = [
  "read",
  "grep",
  "find",
  "ls",
  "mz_relay_send",
  "mz_relay_inbox",
  "mz_relay_ack",
  "mz_relay_peers",
];
const WRITE_TOOLS = ["bash", "write", "edit", "codemode"];

function peerOnlyReason(tool: string): string {
  return (
    `mz-relay: ${tool} is blocked because a peer message is part of this run and your roster role is read_only. ` +
    "The limit lifts when the run ends or your user types."
  );
}

async function assertBlocked(pi: FakePi, tool: string): Promise<void> {
  const result = await pi.toolCall(tool);
  assert.deepEqual(result, { block: true, reason: peerOnlyReason(tool) }, `${tool} should be blocked`);
}

async function assertAllowed(pi: FakePi, tool: string): Promise<void> {
  assert.equal(await pi.toolCall(tool), undefined, `${tool} should be allowed`);
}

// A read_only peer run in progress: the gate is closed when the test starts.
async function peerRun(pi: FakePi): Promise<void> {
  await pi.context([piMessage.user(), piMessage.assistant(), piMessage.peer("m1", "read_only")]);
  await assertBlocked(pi, "bash");
}

describe("brief", () => {
  test("before registration before_agent_start changes nothing", TEST_TIMEOUT, async () => {
    const r = await startRig({ relayd: false });
    assert.equal(await r.pi.beforeAgentStart({ systemPrompt: "BASE", messageType: "user" }), undefined);
  });

  test("after registration the brief is appended to the system prompt", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.waitRegistered();
    const result = await r.pi.beforeAgentStart({ systemPrompt: "BASE", messageType: "user" });
    assert.deepEqual(result, { systemPrompt: `BASE\n\n${fakeBrief("scout")}` });
    assert.ok(result?.systemPrompt?.includes('You are "scout" on mz-relay'));
  });
});

describe("read-only gate", () => {
  test("a peer message still queued behind the user's run leaves the user's tools open", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.listening();
    await exchange(r.socket, { op: "deliver", id: "m1", text: "t", gate: "read_only" });
    await r.pi.context([piMessage.user()]);
    await assertAllowed(r.pi, "bash");
  });

  test("a read_only peer message as the latest prompt blocks write tools with the peer-only reason", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.context([piMessage.user(), piMessage.assistant(), piMessage.peer("m1", "read_only")]);
    for (const tool of WRITE_TOOLS) await assertBlocked(r.pi, tool);
  });

  test("the read-only allowlist stays usable during a peer run", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await peerRun(r.pi);
    for (const tool of READ_ONLY_ALLOWED) await assertAllowed(r.pi, tool);
  });

  test("assistant and toolResult after the peer message keep the gate closed", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.context([piMessage.peer("m1", "read_only"), piMessage.assistant(), piMessage.toolResult()]);
    await assertBlocked(r.pi, "bash");
  });

  test("compaction and branch summaries after the peer message keep the gate closed", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.context([piMessage.peer("m1", "read_only"), piMessage.compactionSummary(), piMessage.branchSummary()]);
    await assertBlocked(r.pi, "write");
  });

  test("another extension's custom message after the peer message keeps the gate closed", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.context([piMessage.peer("m1", "read_only"), piMessage.assistant(), piMessage.otherCustom()]);
    await assertBlocked(r.pi, "bash");
  });

  test("a user message after the peer message opens the gate", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await peerRun(r.pi);
    await r.pi.context([piMessage.peer("m1", "read_only"), piMessage.assistant(), piMessage.user()]);
    await assertAllowed(r.pi, "bash");
  });

  test("a full-gate peer message never blocks", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.context([piMessage.user(), piMessage.assistant(), piMessage.peer("m2", "full")]);
    for (const tool of WRITE_TOOLS) await assertAllowed(r.pi, tool);
  });

  test("a full peer message after a read_only one opens the gate", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await peerRun(r.pi);
    await r.pi.context([piMessage.peer("m1", "read_only"), piMessage.assistant(), piMessage.peer("m2", "full")]);
    await assertAllowed(r.pi, "bash");
  });

  test("before_agent_start with the read_only custom message closes the gate before any context", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.beforeAgentStart({ systemPrompt: "BASE", messageType: "custom", message: piMessage.peer("m1", "read_only") });
    await assertBlocked(r.pi, "bash");
  });

  test("before_agent_start with a full custom message leaves the gate open", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.beforeAgentStart({ systemPrompt: "BASE", messageType: "custom", message: piMessage.peer("m2", "full") });
    await assertAllowed(r.pi, "bash");
  });

  test("agent_settled opens the gate", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await peerRun(r.pi);
    await r.pi.fire("agent_settled", { type: "agent_settled" });
    await assertAllowed(r.pi, "bash");
  });
});

// Context payloads that hold no prompt: Pi compacted the peer message away, or the payload is malformed.
const NO_PROMPT_CONTEXTS: [string, Record<string, unknown>][] = [
  ["an empty message list", { messages: [] }],
  ["only assistant and toolResult", { messages: [piMessage.assistant(), piMessage.toolResult()] }],
  [
    "a summary that replaced the peer message",
    { messages: [piMessage.compactionSummary(), piMessage.assistant(), piMessage.toolResult()] },
  ],
  ["messages that are not an array", { messages: null }],
  ["no messages field", {}],
];

describe("read-only gate without a prompt in context", () => {
  for (const [what, payload] of NO_PROMPT_CONTEXTS) {
    test(`a context with ${what} keeps a closed gate closed`, TEST_TIMEOUT, async () => {
      const r = await startRig();
      await peerRun(r.pi);
      await r.pi.fire("context", { type: "context", ...payload });
      await assertBlocked(r.pi, "bash");
    });
  }

  test("a context without a prompt keeps an open gate open", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await r.pi.context([piMessage.user()]);
    for (const [what, payload] of NO_PROMPT_CONTEXTS) {
      await r.pi.fire("context", { type: "context", ...payload });
      assert.equal(await r.pi.toolCall("bash"), undefined, `bash stays allowed after ${what}`);
    }
  });

  test("agent_settled still opens a gate a prompt-less context left closed", TEST_TIMEOUT, async () => {
    const r = await startRig();
    await peerRun(r.pi);
    await r.pi.context([piMessage.compactionSummary(), piMessage.assistant()]);
    await assertBlocked(r.pi, "bash");
    await r.pi.fire("agent_settled", { type: "agent_settled" });
    await assertAllowed(r.pi, "bash");
  });
});
