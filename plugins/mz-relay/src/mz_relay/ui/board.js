// The board panel: projects and access, task columns, and a drawer with the user's actions.
import { html, useCallback, useEffect, useState } from "./preact-htm.module.js";
import { accessOf, boardColumns, clockTime } from "./view.js";

const STATES = ["open", "in_progress", "review", "done", "cancelled"];
const ACCESS = ["none", "read", "write"];

function useBoard(api) {
  const [error, setError] = useState(null);
  const act = useCallback(
    async (action, args = {}) => {
      try {
        const result = await api(`/api/ext/board/${action}`, args);
        setError(null);
        return result;
      } catch (e) {
        setError(e.message);
        return null;
      }
    },
    [api],
  );
  return { act, error };
}

function NewProject({ act }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [access, setAccess] = useState("none");
  if (!open) return html`<button onClick=${() => setOpen(true)}>New project…</button>`;
  const create = async (e) => {
    e.preventDefault();
    const args = { name: name.trim(), default_access: access };
    if (title.trim()) args.title = title.trim();
    if (await act("create_project", args)) {
      setOpen(false);
      setName("");
      setTitle("");
    }
  };
  return html`
    <form class="row new-project" onSubmit=${create}>
      <input aria-label="Project name" placeholder="name, e.g. web" value=${name} onInput=${(e) => setName(e.target.value)} />
      <input aria-label="Project title" placeholder="title" value=${title} onInput=${(e) => setTitle(e.target.value)} />
      <select aria-label="Default access" value=${access} onChange=${(e) => setAccess(e.target.value)}>
        ${ACCESS.map((a) => html`<option value=${a}>agents: ${a}</option>`)}
      </select>
      <button type="submit" disabled=${!name.trim()}>Create</button>
      <button type="button" onClick=${() => setOpen(false)}>Cancel</button>
    </form>`;
}

function AccessPanel({ project, agents, act }) {
  return html`
    <details class="panel access">
      <summary>Access to ${project.name}${project.archived ? " (archived)" : ""}</summary>
      <div class="row">
        <label>default
          <select value=${project.default_access}
            onChange=${(e) => act("set_access", { project: project.name, access: e.target.value })}>
            ${ACCESS.map((a) => html`<option value=${a}>${a}</option>`)}
          </select>
        </label>
        ${!project.archived && html`
          <button onClick=${() => act("archive_project", { name: project.name })}>Archive</button>`}
      </div>
      <ul class="grants">
        ${agents.map((a) => html`
          <li key=${a.name} class="row">
            <span>${a.name} <span class="muted">${a.label}</span></span>
            <span class="muted">now ${accessOf(project, a.name)}</span>
            <select aria-label=${`Access for ${a.name}`} value=${project.grants[a.name] ?? "inherit"}
              onChange=${(e) => act("set_access", { project: project.name, agent: a.name, access: e.target.value })}>
              <option value="inherit">inherit</option>
              ${ACCESS.map((x) => html`<option value=${x}>${x}</option>`)}
            </select>
          </li>`)}
      </ul>
    </details>`;
}

function Card({ task, onOpen }) {
  const waits = task.state === "open" && !task.ready;
  return html`
    <li>
      <button class="card plain" onClick=${() => onOpen(task.id)}>
        <span class="row"><code>${task.id}</code><span class="muted">p${task.priority}</span></span>
        <span class="title">${task.title}</span>
        <span class="row">
          <span class="muted">${task.assignee ?? ""}</span>
          ${task.ready && html`<span class="tag tone-ok">ready</span>`}
          ${waits && html`<span class="tag tone-warn">waits</span>`}
        </span>
      </button>
    </li>`;
}

function Drawer({ id, agents, act, tick, onClose }) {
  const [detail, setDetail] = useState(null);
  const [priority, setPriority] = useState("");
  const [reason, setReason] = useState("");
  useEffect(() => {
    act("task", { id }).then((r) => {
      if (!r) return;
      setDetail(r);
      setPriority(String(r.task.priority));
    });
  }, [id, tick]);
  if (!detail) return html`<aside class="panel drawer"><p class="muted">loading…</p></aside>`;
  const t = detail.task;
  return html`
    <aside class="panel drawer" aria-label=${`Task ${t.id}`}>
      <div class="row"><h2>${t.id} · ${t.state.replace("_", " ")}</h2><button onClick=${onClose}>Close</button></div>
      <h3>${t.title}</h3>
      <p class="muted">by ${t.creator}${t.assignee ? `, held by ${t.assignee}` : ""}</p>
      ${detail.deps.length > 0 && html`
        <p>depends on ${detail.deps.map((d) => html`<code key=${d.id}>${d.id}</code> (${d.state}) `)}</p>`}
      <p><b>Acceptance:</b> ${t.acceptance}</p>
      <pre class="body">${t.body}</pre>
      <div class="actions">
        ${t.state === "review" && html`<button class="approve" onClick=${() => act("approve", { id })}>Approve</button>`}
        <select aria-label="Move to" value=${t.state} onChange=${(e) => act("move", { id, state: e.target.value })}>
          ${STATES.map((s) => html`<option value=${s}>${s.replace("_", " ")}</option>`)}
        </select>
        <select aria-label="Holder" value=${t.assignee ?? ""}
          onChange=${(e) => act("reassign", e.target.value ? { id, agent: e.target.value } : { id })}>
          <option value="">nobody</option>
          ${agents.map((a) => html`<option value=${a.name}>${a.name}</option>`)}
        </select>
      </div>
      <div class="actions">
        <input type="number" min="-100" max="100" aria-label="Priority" value=${priority}
          onInput=${(e) => setPriority(e.target.value)} />
        <button onClick=${() => act("reprioritize", { id, priority: Number(priority) })}>Set priority</button>
      </div>
      ${t.state !== "cancelled" && html`
        <div class="actions">
          <input aria-label="Cancel reason" placeholder="reason" value=${reason} onInput=${(e) => setReason(e.target.value)} />
          <button class="deny" onClick=${() => act("cancel", reason.trim() ? { id, reason: reason.trim() } : { id })}>Cancel task</button>
        </div>`}
      <h3>Events</h3>
      <ul class="events">
        ${detail.events.map((e, i) => html`
          <li key=${i}><span class="muted">${clockTime(new Date(e.at * 1000).toISOString())}</span>
            ${" "}<b>${e.actor}</b> ${e.kind}${e.note ? `: ${e.note}` : ""}</li>`)}
      </ul>
    </aside>`;
}

export function Board({ api, agents, tick }) {
  const { act, error } = useBoard(api);
  const [view, setView] = useState(null);
  const [project, setProject] = useState("");
  const [openId, setOpenId] = useState(null);
  const [cancelled, setCancelled] = useState(false);
  useEffect(() => {
    act("overview").then((r) => r && setView(r));
  }, [tick]);
  if (!view) return html`<section class="panel"><p class=${error ? "error" : "muted"}>${error ?? "loading…"}</p></section>`;
  const current = view.projects.find((p) => p.name === project) ?? view.projects[0];
  const columns = current ? boardColumns(view.tasks, current.name, { cancelled }) : {};
  return html`
    <div class="board">
      <div class="row toolbar">
        ${view.projects.length > 0 && html`
          <select aria-label="Project" value=${current?.name} onChange=${(e) => setProject(e.target.value)}>
            ${view.projects.map((p) => html`<option value=${p.name}>${p.title} (${p.name})</option>`)}
          </select>`}
        <label><input type="checkbox" checked=${cancelled} onChange=${(e) => setCancelled(e.target.checked)} /> cancelled</label>
        <${NewProject} act=${act} />
      </div>
      ${error && html`<p class="banner error" role="alert">${error}</p>`}
      ${!current && html`<p class="muted">No projects yet. Agents can only post to projects you create.</p>`}
      ${current && html`<${AccessPanel} project=${current} agents=${agents} act=${act} />`}
      <div class="board-body">
        <div class="columns">
          ${Object.entries(columns).map(([state, tasks]) => html`
            <section class="panel column" key=${state}>
              <h2>${state.replace("_", " ")} <span class="count">${tasks.length}</span></h2>
              <ul>${tasks.map((t) => html`<${Card} key=${t.id} task=${t} onOpen=${setOpenId} />`)}</ul>
            </section>`)}
        </div>
        ${openId && html`<${Drawer} id=${openId} agents=${agents} act=${act} tick=${tick} onClose=${() => setOpenId(null)} />`}
      </div>
    </div>`;
}
