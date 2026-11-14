// The notes panel: every project's decision log, searchable, with the user's delete.
import { html, useEffect, useState } from "./preact-htm.module.js";
import { clockTime } from "./view.js";

export function Notes({ api, tick }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [project, setProject] = useState("");
  const [query, setQuery] = useState("");
  const [again, setAgain] = useState(0);

  const call = async (action, args) => {
    try {
      const result = await api(`/api/ext/notes/${action}`, args);
      setError(null);
      return result;
    } catch (e) {
      setError(e.message);
      return null;
    }
  };
  useEffect(() => {
    const args = {};
    if (project) args.project = project;
    if (query.trim()) args.query = query.trim();
    call("list", args).then((r) => r && setView(r));
  }, [tick, project, query, again]);

  const remove = async (id) => {
    if (await call("delete", { id })) setAgain((n) => n + 1);
  };

  if (!view) return html`<section class="panel"><p class=${error ? "error" : "muted"}>${error ?? "loading…"}</p></section>`;
  return html`
    <section class="panel notes">
      <div class="filters">
        <select aria-label="Project" value=${project} onChange=${(e) => setProject(e.target.value)}>
          <option value="">all projects</option>
          ${view.projects.map((p) => html`<option value=${p}>${p}</option>`)}
        </select>
        <input type="search" aria-label="Search notes" placeholder="search titles and bodies" value=${query}
          onInput=${(e) => setQuery(e.target.value)} />
      </div>
      ${error && html`<p class="banner error" role="alert">${error}</p>`}
      ${view.notes.length === 0 && html`<p class="muted">${view.projects.length ? "No notes yet." : view.text}</p>`}
      <ul>
        ${view.notes.map((n) => html`
          <li key=${n.id}>
            <div class="row">
              <span><code>#${n.id}</code> <span class="tag">${n.kind}</span> <b>${n.title}</b></span>
              <span class="muted">${n.project} · ${n.author} · ${clockTime(new Date(n.created_at * 1000).toISOString())}</span>
            </div>
            <pre class="body">${n.body}</pre>
            <div class="actions"><button class="deny" onClick=${() => remove(n.id)}>Delete</button></div>
          </li>`)}
      </ul>
    </section>`;
}
