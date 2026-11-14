// The UI's pure helpers: event folding, filters and the user's inbox merge.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  applyEvent,
  emptyModel,
  filterMessages,
  mergeInbox,
  modelFromState,
  statusTone,
} from "../../src/mz_relay/ui/view.js";

const row = (id, extra = {}) => ({
  id,
  source: "alice",
  to: "bob",
  status: "queued",
  preview: `about ${id}`,
  time: `2026-11-14T10:00:0${id.at(-1)}.000Z`,
  ...extra,
});

const model = (extra = {}) => ({ ...emptyModel(), ...extra });

describe("modelFromState", () => {
  test("takes roster cards from peers and online flags from status", () => {
    const state = {
      status: { pid: 7, version: "0.1.1", agents: [{ name: "bob", online: true }] },
      peers: { agents: [{ name: "bob", label: "personal", role: "builder", online: false }] },
      log: { messages: [row("m1")] },
      held: { messages: [] },
      head: 12,
    };

    const built = modelFromState(state);

    assert.deepEqual(built.agents, [{ name: "bob", label: "personal", role: "builder", online: true }]);
    assert.equal(built.head, 12);
    assert.deepEqual(built.relay, { pid: 7, version: "0.1.1" });
  });
});

describe("applyEvent", () => {
  test("a presence event flips one agent and moves the head", () => {
    const before = model({ agents: [{ name: "bob", online: false }, { name: "carol", online: false }], head: 3 });

    const { model: after, refresh } = applyEvent(before, { seq: 4, kind: "presence", agent: "bob", online: true });

    assert.deepEqual(after.agents.map((a) => a.online), [true, false]);
    assert.equal(after.head, 4);
    assert.equal(refresh, false);
  });

  test("a status change on a known row updates it without a reload", () => {
    const before = model({ log: [row("m1")] });

    const { model: after, refresh } = applyEvent(before, {
      seq: 5,
      kind: "message",
      source: "alice",
      id: "m1",
      to: "bob",
      status: "delivered",
    });

    assert.equal(after.log[0].status, "delivered");
    assert.equal(refresh, false);
  });

  test("an unknown message or anything touching the held queue asks for a reload", () => {
    const before = model({ log: [row("m1", { status: "held" })], held: [row("m1", { status: "held" })] });

    const unknown = applyEvent(before, { seq: 6, kind: "message", source: "dave", id: "m9", to: "bob", status: "queued" });
    const released = applyEvent(before, { seq: 7, kind: "message", source: "alice", id: "m1", to: "bob", status: "queued" });

    assert.equal(unknown.refresh, true);
    assert.equal(released.refresh, true);
  });

  test("a new message to the user asks for the inbox", () => {
    const { inbox } = applyEvent(model(), { seq: 1, kind: "message", source: "bob", id: "m1", to: "user", status: "queued" });

    assert.equal(inbox, true);
  });
});

describe("filterMessages", () => {
  const messages = [row("m1"), row("m2", { source: "carol", to: "alice", status: "held" }), row("m3", { preview: "Release notes" })];

  test("an agent filter matches either end of the route", () => {
    assert.deepEqual(filterMessages(messages, { agent: "alice" }).map((m) => m.id), ["m1", "m2", "m3"]);
    assert.deepEqual(filterMessages(messages, { agent: "carol" }).map((m) => m.id), ["m2"]);
  });

  test("status and text filters combine, text ignoring case", () => {
    assert.deepEqual(filterMessages(messages, { status: "held" }).map((m) => m.id), ["m2"]);
    assert.deepEqual(filterMessages(messages, { text: "release" }).map((m) => m.id), ["m3"]);
    assert.deepEqual(filterMessages(messages, { status: "held", text: "release" }), []);
  });
});

describe("mergeInbox", () => {
  test("keeps each message once, drops acked ones and sorts by time", () => {
    const shown = [row("m1"), row("m2")];

    const merged = mergeInbox(shown, { messages: [row("m3")], awaiting_ack: [row("m2"), row("m3")] });

    assert.deepEqual(merged.map((m) => m.id), ["m2", "m3"]);
  });
});

describe("statusTone", () => {
  test("names a tone for every terminal state and falls back to wait", () => {
    assert.equal(statusTone("denied"), "bad");
    assert.equal(statusTone("acked"), "done");
    assert.equal(statusTone("something-new"), "wait");
  });
});
