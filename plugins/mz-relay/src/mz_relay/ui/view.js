// Pure helpers for the UI: no DOM, no fetch, so node --test covers them.

export const USER = "user";

const TONES = {
  queued: "wait",
  delivering: "wait",
  held: "warn",
  fallback: "warn",
  handed_off: "ok",
  delivered: "ok",
  leased: "ok",
  acked: "done",
  denied: "bad",
  undeliverable: "bad",
  expired: "bad",
};

export function statusTone(status) {
  return TONES[status] ?? "wait";
}

export function emptyModel() {
  return { agents: [], log: [], held: [], head: 0, relay: null };
}

// The /api/state answer as the model: roster cards from peers, online flags from status.
export function modelFromState(state) {
  const online = new Map(state.status.agents.map((a) => [a.name, a.online]));
  return {
    agents: state.peers.agents.map((card) => ({ ...card, online: online.get(card.name) ?? card.online })),
    log: state.log.messages,
    held: state.held.messages,
    head: state.head,
    relay: { pid: state.status.pid, version: state.status.version },
  };
}

const sameMessage = (a, b) => a.source === b.source && a.id === b.id;

// Applies one stream event; `refresh` asks for a reload when the event names rows the model lacks.
export function applyEvent(model, event) {
  const head = Math.max(model.head, event.seq ?? 0);
  if (event.kind === "presence") {
    const agents = model.agents.map((a) => (a.name === event.agent ? { ...a, online: event.online } : a));
    return { model: { ...model, agents, head }, refresh: false, inbox: false };
  }
  const known = model.log.some((m) => sameMessage(m, event));
  const log = model.log.map((m) => (sameMessage(m, event) ? { ...m, status: event.status } : m));
  const touchesHeld = event.status === "held" || model.held.some((m) => sameMessage(m, event));
  return {
    model: { ...model, log, head },
    refresh: !known || touchesHeld,
    inbox: event.to === USER && event.status === "queued",
  };
}

export function filterMessages(messages, { agent = "", status = "", text = "" } = {}) {
  const needle = text.trim().toLowerCase();
  return messages.filter(
    (m) =>
      (!agent || m.source === agent || m.to === agent) &&
      (!status || m.status === status) &&
      (!needle || `${m.preview} ${m.id}`.toLowerCase().includes(needle)),
  );
}

// The user's inbox: what is awaiting an ack, newest last, each message once.
export function mergeInbox(current, result) {
  const byId = new Map(current.map((m) => [m.id, m]));
  for (const m of [...result.awaiting_ack, ...result.messages]) byId.set(m.id, m);
  const waiting = new Set(result.awaiting_ack.map((m) => m.id));
  for (const m of result.messages) waiting.add(m.id);
  return [...byId.values()].filter((m) => waiting.has(m.id)).sort((a, b) => a.time.localeCompare(b.time));
}

export function clockTime(iso) {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleTimeString([], { hour12: false });
}
