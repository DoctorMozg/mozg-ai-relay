// The mz-relay UI: agents, live traffic, the held queue, a composer and the user's inbox.
import { Board } from "./board.js";
import { Notes } from "./notes.js";
import { html, render, useCallback, useEffect, useRef, useState } from "./preact-htm.module.js";
import {
  USER,
  applyEvent,
  clockTime,
  emptyModel,
  filterMessages,
  mergeInbox,
  modelFromState,
  statusTone,
  viewTabs,
} from "./view.js";

const STATUSES = ["queued", "held", "delivering", "handed_off", "delivered", "fallback", "leased", "acked", "denied", "undeliverable", "expired"];
const KINDS = ["request", "inform", "reply", "done"];
const STREAM_EVENTS = ["message", "presence", "ext"];
const REFRESH_MS = 250;

async function api(path, body) {
  const init = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, { ...init, credentials: "same-origin" });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error?.message ?? `${resp.status} ${resp.statusText}`);
  return data;
}

const messageUrl = (m) => `/api/message?source=${encodeURIComponent(m.source)}&id=${encodeURIComponent(m.id)}`;

function Status({ status }) {
  return html`<span class="status tone-${statusTone(status)}">${status.replace("_", " ")}</span>`;
}

function Route({ m }) {
  const name = (who) => (who === USER ? html`<b class="user">you</b>` : who);
  return html`<span class="route">${name(m.source)} → ${name(m.to)}</span>`;
}

function Body({ m }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api(messageUrl(m)).then(setDetail, (e) => setError(e.message));
  }, [m.source, m.id]);
  if (error) return html`<p class="error">${error}</p>`;
  if (!detail) return html`<p class="muted">loading…</p>`;
  const env = detail.envelope;
  return html`
    <div class="detail">
      <dl>
        <dt>kind</dt><dd>${env.kind}</dd>
        <dt>context</dt><dd><code>${env.context_id}</code></dd>
        <dt>hop</dt><dd>${env.hop}</dd>
        ${detail.reason && html`<dt>reason</dt><dd>${detail.reason}</dd>`}
      </dl>
      <pre class="body">${env.body}</pre>
    </div>`;
}

function Agents({ agents }) {
  return html`
    <section class="panel">
      <h2>Agents <span class="count">${agents.filter((a) => a.online).length}/${agents.length} online</span></h2>
      <ul class="agents">
        ${agents.map((a) => html`
          <li key=${a.name}>
            <span class="dot ${a.online ? "on" : "off"}" title=${a.online ? "online" : "offline"}></span>
            <span class="name">${a.name}</span>
            <span class="tag label-${a.label}">${a.label}</span>
            <span class="muted">${a.harness} · ${a.role}</span>
          </li>`)}
      </ul>
    </section>`;
}

function HeldItem({ m, onDone }) {
  const [open, setOpen] = useState(false);
  const [denying, setDenying] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState(null);
  const decide = async (decision) => {
    setError(null);
    const args = { source: m.source, id: m.id, decision };
    if (decision === "deny" && reason.trim()) args.reason = reason.trim();
    try {
      await api("/api/decide", args);
      onDone();
    } catch (e) {
      setError(e.message);
    }
  };
  return html`
    <li class="held">
      <div class="row">
        <span>${m.source} <span class="tag label-${m.source_label}">${m.source_label}</span>
          → ${m.to} <span class="tag label-${m.to_label}">${m.to_label ?? "not in roster"}</span></span>
        <span class="muted">${clockTime(m.time)}</span>
      </div>
      ${!open && html`<p class="preview">${m.preview}</p>`}
      ${open && html`<${Body} m=${m} />`}
      <div class="actions">
        ${!open && html`<button onClick=${() => setOpen(true)}>Review</button>`}
        ${open && !denying && html`
          <button class="approve" onClick=${() => decide("approve")}>Approve</button>
          <button class="deny" onClick=${() => setDenying(true)}>Deny…</button>`}
        ${denying && html`
          <input aria-label="Reason shown to the sender" placeholder="reason (optional)"
            value=${reason} onInput=${(e) => setReason(e.target.value)} />
          <button class="deny" onClick=${() => decide("deny")}>Deny</button>
          <button onClick=${() => setDenying(false)}>Cancel</button>`}
      </div>
      ${error && html`<p class="error">${error}</p>`}
    </li>`;
}

function Held({ held, onChange }) {
  return html`
    <section class="panel">
      <h2>Held for you <span class="count" aria-live="polite">${held.length}</span></h2>
      ${held.length === 0 && html`<p class="muted">Nothing is waiting for approval.</p>`}
      <ul>${held.map((m) => html`<${HeldItem} key=${`${m.source}/${m.id}`} m=${m} onDone=${onChange} />`)}</ul>
    </section>`;
}

function Timeline({ log, agents }) {
  const [filter, setFilter] = useState({ agent: "", status: "", text: "" });
  const [openKey, setOpenKey] = useState(null);
  const set = (key) => (e) => setFilter({ ...filter, [key]: e.target.value });
  const shown = filterMessages(log, filter).slice().reverse();
  return html`
    <section class="panel timeline">
      <h2>Messages</h2>
      <div class="filters">
        <select aria-label="Agent" value=${filter.agent} onChange=${set("agent")}>
          <option value="">all agents</option>
          <option value=${USER}>you</option>
          ${agents.map((a) => html`<option value=${a.name}>${a.name}</option>`)}
        </select>
        <select aria-label="Status" value=${filter.status} onChange=${set("status")}>
          <option value="">any status</option>
          ${STATUSES.map((s) => html`<option value=${s}>${s}</option>`)}
        </select>
        <input type="search" aria-label="Search" placeholder="search" value=${filter.text} onInput=${set("text")} />
      </div>
      ${shown.length === 0 && html`<p class="muted">No messages.</p>`}
      <ul class="messages">
        ${shown.map((m) => {
          const key = `${m.source}/${m.id}`;
          const open = key === openKey;
          return html`
            <li key=${key} class=${open ? "open" : ""}>
              <button class="row plain" aria-expanded=${open} onClick=${() => setOpenKey(open ? null : key)}>
                <span class="muted time">${clockTime(m.time)}</span>
                <${Route} m=${m} />
                <span class="kind">${m.kind}</span>
                <${Status} status=${m.status} />
              </button>
              ${open ? html`<${Body} m=${m} />` : html`<p class="preview">${m.preview}</p>`}
            </li>`;
        })}
      </ul>
    </section>`;
}

function Composer({ agents, replyTo, onClearReply }) {
  const [to, setTo] = useState("");
  const [kind, setKind] = useState("request");
  const [body, setBody] = useState("");
  const [note, setNote] = useState(null);
  useEffect(() => {
    if (replyTo) {
      setTo(replyTo.source);
      setKind("reply");
    }
  }, [replyTo]);
  const target = to || agents[0]?.name || "";
  const send = async (e) => {
    e.preventDefault();
    const args = { to: target, kind, body };
    if (replyTo) args.in_reply_to = replyTo.id;
    try {
      const sent = await api("/api/send", args);
      setBody("");
      setNote({ ok: true, text: `sent ${sent.id} to ${target} (${sent.status})` });
      onClearReply();
    } catch (err) {
      setNote({ ok: false, text: err.message });
    }
  };
  return html`
    <section class="panel">
      <h2>Message an agent</h2>
      <form class="composer" onSubmit=${send}>
        <div class="row">
          <select aria-label="To" value=${target} onChange=${(e) => setTo(e.target.value)}>
            ${agents.map((a) => html`<option value=${a.name}>${a.name}</option>`)}
          </select>
          <select aria-label="Kind" value=${kind} onChange=${(e) => setKind(e.target.value)}>
            ${KINDS.map((k) => html`<option value=${k}>${k}</option>`)}
          </select>
        </div>
        ${replyTo && html`
          <p class="muted">answering <code>${replyTo.id}</code>
            <button type="button" class="link" onClick=${onClearReply}>clear</button></p>`}
        <textarea aria-label="Message" rows="5" placeholder="Write as yourself; the agent sees it comes from you."
          value=${body} onInput=${(e) => setBody(e.target.value)}></textarea>
        <button type="submit" disabled=${!target || (!body.trim() && kind !== "done")}>Send</button>
        ${note && html`<p class=${note.ok ? "muted" : "error"} role="status">${note.text}</p>`}
      </form>
    </section>`;
}

function Inbox({ inbox, onAck, onReply }) {
  return html`
    <section class="panel">
      <h2>Your inbox <span class="count" aria-live="polite">${inbox.length}</span></h2>
      ${inbox.length === 0 && html`<p class="muted">No messages from agents.</p>`}
      <ul>
        ${inbox.map((m) => html`
          <li key=${m.id} class="inbox">
            <div class="row"><span>from <b>${m.source}</b> · ${m.kind}</span><span class="muted">${clockTime(m.time)}</span></div>
            <pre class="body">${m.body}</pre>
            <div class="actions">
              <button onClick=${() => onReply(m)}>Reply</button>
              <button onClick=${() => onAck(m)}>Mark handled</button>
            </div>
          </li>`)}
      </ul>
    </section>`;
}

function App() {
  const [model, setModel] = useState(emptyModel);
  const [inbox, setInbox] = useState([]);
  const [stream, setStream] = useState("connecting");
  const [error, setError] = useState(null);
  const [replyTo, setReplyTo] = useState(null);
  const [tab, setTab] = useState("relay");
  // Bumped on every extension change, reset and reconnect; extension panels reload on it.
  const [extTick, setExtTick] = useState(0);
  const bumpExt = () => setExtTick((n) => n + 1);
  const timer = useRef(null);
  // The stream handler outlives renders, so it reads the newest model through a ref.
  const latest = useRef(model);
  latest.current = model;

  const reload = useCallback(async () => {
    try {
      setModel(modelFromState(await api("/api/state")));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);
  const reloadSoon = useCallback(() => {
    clearTimeout(timer.current);
    timer.current = setTimeout(reload, REFRESH_MS);
  }, [reload]);
  const pullInbox = useCallback(async () => {
    try {
      const result = await api("/api/inbox", {});
      setInbox((current) => mergeInbox(current, result));
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    reload();
    pullInbox();
    const source = new EventSource("/api/events");
    const onEvent = (e) => {
      const result = applyEvent(latest.current, JSON.parse(e.data));
      latest.current = result.model;
      setModel(result.model);
      if (result.refresh) reloadSoon();
      if (result.ext) bumpExt();
      if (result.inbox) pullInbox();
    };
    STREAM_EVENTS.forEach((name) => source.addEventListener(name, onEvent));
    source.addEventListener("reset", () => {
      reload();
      bumpExt();
    });
    source.addEventListener("relay-error", (e) => setError(JSON.parse(e.data).error.message));
    source.onopen = () => {
      setStream("live");
      reload();
      bumpExt();
    };
    source.onerror = () => setStream("reconnecting");
    return () => source.close();
  }, []);

  const ack = async (m) => {
    try {
      await api("/api/ack", { id: m.id });
      setInbox((current) => current.filter((x) => x.id !== m.id));
    } catch (e) {
      setError(e.message);
    }
  };

  const tabs = viewTabs(model.extensions);
  return html`
    <header>
      <h1>mz-relay</h1>
      <span class="stream ${stream}" role="status">${stream}</span>
      ${model.relay && html`<span class="muted">relayd ${model.relay.version} · pid ${model.relay.pid}</span>`}
      ${tabs.length > 1 && html`
        <nav class="tabs" aria-label="Views">
          ${tabs.map((name) => html`
            <button class=${tab === name ? "active" : ""} aria-pressed=${tab === name} onClick=${() => setTab(name)}>${name}</button>`)}
        </nav>`}
    </header>
    ${error && html`<p class="banner error" role="alert">${error}</p>`}
    ${tab === "board" && html`<main class="wide"><${Board} api=${api} agents=${model.agents} tick=${extTick} /></main>`}
    ${tab === "notes" && html`<main class="wide"><${Notes} api=${api} tick=${extTick} /></main>`}
    <main hidden=${tab !== "relay"}>
      <div class="col">
        <${Agents} agents=${model.agents} />
        <${Held} held=${model.held} onChange=${reload} />
      </div>
      <${Timeline} log=${model.log} agents=${model.agents} />
      <div class="col">
        <${Composer} agents=${model.agents} replyTo=${replyTo} onClearReply=${() => setReplyTo(null)} />
        <${Inbox} inbox=${inbox} onAck=${ack} onReply=${setReplyTo} />
      </div>
    </main>`;
}

const root = document.getElementById("app");
// Preact appends to what is there, so the loading note goes first.
root.replaceChildren();
render(html`<${App} />`, root);
