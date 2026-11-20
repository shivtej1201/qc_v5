// QC V5 Dashboard — SEO Toolkit: the free integrations (AI crawler access, security headers, structured data,
// PageSpeed / CrUX, Wayback, IndexNow, Bing Webmaster, Search Console + GA4) and the Google update timeline.
// Motion: GSAP through motion.js (view-scoped, reverted on navigation, off under reduced motion).
import { api } from "./api.js";
import { h, mount, fmt, toast, icon } from "./ui.js";
import { CardReveal, StaggerReveal, CounterAnimation, ProgressAnimation, StatusTransition } from "./motion.js";

const SEV = ["HIGH", "MEDIUM", "LOW", "INFO"];
const STATUS_LABEL = { ok: "Done", error: "Error", needs_key: "Needs key", unreachable: "Unreachable", running: "Running…" };
const sevPill = (s) => h("span", { class: `sev sev-${s}` }, s);
const state = { url: "", results: new Map(), catalogue: null };

function statusChip(status) {
  const cls = status === "ok" ? "st-PASS" : status === "running" ? "st-RUNNING live" : status === "needs_key" ? "st-QUEUED" : "st-FAILED";
  return h("span", { class: `pill ${cls}` }, STATUS_LABEL[status] || status);
}

function sevBar(counts) {
  const total = SEV.reduce((a, s) => a + (counts?.[s] || 0), 0);
  const bar = h("div", { class: "sev-bar", role: "img", "aria-label": SEV.map((s) => `${counts?.[s] || 0} ${s}`).join(", ") },
    SEV.map((s) => h("i", { class: `sev-dot-${s}`, style: { flexGrow: String(counts?.[s] || 0) } })));
  if (!total) bar.classList.add("empty-bar");
  return bar;
}

function summaryRows(r) {
  const out = [];
  for (const [k, v] of Object.entries(r.summary || {})) {
    if (k === "findings_by_severity" || v === null || v === undefined) continue;
    let val = v;
    if (Array.isArray(v)) val = v.length ? v.join(", ") : "none";
    else if (typeof v === "object") val = Object.entries(v).map(([a, b]) => `${a} ${b}`).join(" · ") || "—";
    else if (typeof v === "boolean") val = v ? "yes" : "no";
    else if (typeof v === "number") val = fmt.n(v);
    out.push(h("dt", {}, k.replaceAll("_", " ")), h("dd", {}, String(val)));
  }
  return out.length ? h("dl", { class: "kv" }, out) : null;
}

function findingList(r) {
  if (!r.findings?.length) return h("p", { class: "muted" }, r.status === "ok" ? "No problems found." : "");
  const items = r.findings.map((f) => h("li", { class: "finding" },
    h("div", { class: "row" }, sevPill(f.severity), h("b", {}, f.title)),
    f.detail ? h("div", { class: "dim mono-wrap" }, f.detail) : null,
    f.fix ? h("div", { class: "fix" }, icon("check"), " ", f.fix) : null));
  const ul = h("ul", { class: "findings" }, items);
  StaggerReveal(items, { stagger: 0.03 });
  return ul;
}

function resultBody(spec, r) {
  if (!r) return h("p", { class: "muted" }, "Not run yet.");
  if (r.status === "running") return h("div", { class: "progress indeterminate" }, h("i", {}));
  const parts = [];
  if (r.error) parts.push(h("p", { class: r.status === "needs_key" ? "muted" : "bad" }, r.error));
  if (r.status === "needs_key") {
    parts.push(h("p", { class: "dim" }, "Set ", ...spec.env.map((e, i) => [i ? ", " : "", h("code", {}, e)]),
      " in the server environment, then run again."));
    if (r.data?.suggested_key) parts.push(h("dl", { class: "kv" },
      h("dt", {}, "suggested key"), h("dd", { class: "mono" }, r.data.suggested_key),
      h("dt", {}, "publish at"), h("dd", { class: "mono" }, r.data.publish_at)));
  }
  parts.push(summaryRows(r), findingList(r));
  if (r.checked_at) parts.push(h("div", { class: "dim small" }, `${fmt.ago(r.checked_at)} · ${fmt.n(r.duration_ms)} ms · ${r.source || ""}`));
  return h("div", { class: "stack-sm" }, parts);
}

function integrationCard(spec, onRun) {
  const r = state.results.get(spec.id);
  const counts = r?.summary?.findings_by_severity;
  const nums = SEV.slice(0, 3).map((s) => {
    const el = h("b", { class: `sev-num sev-text-${s}` }, "0");
    if (r && r.status !== "running") requestAnimationFrame(() => CounterAnimation(el, counts?.[s] || 0));
    return h("span", { class: "sev-count" }, el, h("small", {}, s));
  });
  const params = spec.params.map((p) => h("label", { class: "field inline" }, h("span", {}, p.name),
    h("select", { "data-param": p.name, "aria-label": p.name },
      p.options.map((o) => h("option", { value: String(o), selected: o === p.default || null }, String(o))))));
  const card = h("section", { class: "card integ", "data-id": spec.id },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, spec.name), h("div", { class: "dim" }, `${spec.category} · ${spec.cost}`)),
      r ? statusChip(r.status) : (spec.configured ? h("span", { class: "tag" }, "ready") : h("span", { class: "tag tag-warn" }, "key needed"))),
    h("p", { class: "muted" }, spec.description),
    h("div", { class: "row integ-bar" }, ...nums, h("span", { class: "spacer" }), ...params,
      h("button", { class: "btn btn-primary", disabled: r?.status === "running" || null, onclick: (e) => onRun(spec, e.currentTarget.closest(".integ")) },
        icon("play"), r ? "Run again" : "Run")),
    counts ? sevBar(counts) : null,
    h("div", { class: "integ-body" }, resultBody(spec, r)));
  return card;
}

function updatesCard() {
  const host = h("div", {}, h("p", { class: "muted" }, "Loading…"));
  const card = h("section", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Google Search updates"),
      h("div", { class: "dim" }, "Confirmed core, spam and policy updates — line them up with traffic changes"))), host);
  api.googleUpdates({ limit: 12 }).then((d) => {
    const items = d.updates.map((u) => h("li", { class: "tl-item" },
      h("span", { class: "tl-dot", "aria-hidden": "true" }),
      h("div", {}, h("div", { class: "row" }, h("b", { class: "mono" }, u.date), h("span", { class: "tag" }, u.kind)),
        h("a", { href: u.source, target: "_blank", rel: "noopener noreferrer" }, u.name),
        u.notes ? h("div", { class: "dim" }, u.notes) : null)));
    mount(host, h("ol", { class: "timeline" }, items),
      h("div", { class: "dim small" }, `${d.total_matching} updates since March 2024 · dataset verified ${d.last_verified} · every entry cites a Google URL`));
    StaggerReveal(items, { stagger: 0.04 });
  }).catch((e) => mount(host, h("p", { class: "bad" }, e.message)));
  CardReveal(card);
  return card;
}

function templatesCard() {
  const pre = h("pre", { class: "code-block", tabindex: 0 }, "");
  const select = h("select", { "aria-label": "Schema template" });
  const card = h("section", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Schema.org templates"),
      h("div", { class: "dim" }, "Ready-to-fill JSON-LD; replace the [placeholders] and validate"))),
    h("div", { class: "row" }, select, h("span", { class: "spacer" }),
      h("button", { class: "btn", onclick: async () => {
        try { await navigator.clipboard.writeText(pre.textContent); toast("Template copied"); }
        catch { toast("Copy failed — select the text instead", "err"); } } }, "Copy")),
    pre);
  api.schemaTemplates().then(({ templates }) => {
    const show = (name) => {
      const t = templates.find((x) => x.name === name);
      pre.textContent = JSON.stringify(t?.template || {}, null, 2);
      CardReveal(pre);
    };
    mount(select, templates.map((t) => h("option", { value: t.name }, t.name)));
    select.onchange = () => show(select.value);
    show(templates[0]?.name);
  }).catch((e) => { pre.textContent = e.message; });
  CardReveal(card);
  return card;
}

/** Full view. `rerender` is called after a run finishes so the shell + cards refresh. */
export function integrationsView(query) {
  if (query?.get("url")) state.url = query.get("url");
  const grid = h("div", { class: "grid g-2 integ-grid" });
  const input = h("input", { type: "url", placeholder: "https://www.example.com/page", value: state.url, "aria-label": "URL to check",
    oninput: (e) => { state.url = e.target.value.trim(); } });
  const totals = h("div", { class: "row integ-totals" });

  const paint = () => {
    if (!state.catalogue) return;
    const cards = state.catalogue.integrations.map((spec) => integrationCard(spec, runOne));
    mount(grid, cards);
    const agg = { HIGH: 0, MEDIUM: 0, LOW: 0, INFO: 0 };
    let done = 0;
    for (const r of state.results.values()) {
      if (r.status === "running") continue;
      done += 1;
      for (const s of SEV) agg[s] += r.summary?.findings_by_severity?.[s] || 0;
    }
    const bar = h("div", { class: "progress" }, h("i", {}));
    mount(totals, h("span", { class: "dim" }, `${done} of ${state.catalogue.integrations.length} checks run`),
      h("div", { style: { flex: "1", minWidth: "120px" } }, bar),
      ...SEV.map((s) => h("span", { class: "row tight" }, sevPill(s), h("b", {}, fmt.n(agg[s])))));
    ProgressAnimation(bar.firstChild, done / state.catalogue.integrations.length);
  };

  async function runOne(spec, cardEl) {
    if (!state.url) { toast("Enter a URL first", "err"); input.focus(); return; }
    const params = {};
    cardEl?.querySelectorAll("[data-param]").forEach((s) => {
      const v = s.value;
      params[s.dataset.param] = v === "true" ? true : v === "false" ? false : /^\d+$/.test(v) ? Number(v) : v;
    });
    state.results.set(spec.id, { status: "running" });
    paint();
    try {
      const r = await api.runIntegration(spec.id, state.url, params);
      state.results.set(spec.id, r);
    } catch (e) {
      state.results.set(spec.id, { status: "error", error: e.message, summary: {}, findings: [] });
    }
    paint();
    const el = grid.querySelector(`[data-id="${spec.id}"] .pill`);
    if (el) StatusTransition(el, state.results.get(spec.id).status);
  }

  async function runAllFree() {
    if (!state.url) { toast("Enter a URL first", "err"); input.focus(); return; }
    const free = state.catalogue.integrations.filter((i) => i.configured);
    await Promise.all(free.map((spec) => runOne(spec, grid.querySelector(`[data-id="${spec.id}"]`))));
    toast(`${free.length} checks finished`);
  }

  const head = h("section", { class: "card hero-integ" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Check a URL with every free integration"),
      h("div", { class: "dim" }, "Runs from the server; nothing is stored. Private and internal addresses are blocked."))),
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); runAllFree(); } },
      h("div", { style: { flex: "1", minWidth: "240px" } }, input),
      h("button", { class: "btn btn-primary", type: "submit" }, icon("play"), "Run all ready checks")),
    totals);
  CardReveal(head);

  const notIncluded = h("details", { class: "card" }, h("summary", {}, "Not included (paid or out of scope)"), h("ul", {}));
  api.integrations().then((c) => {
    state.catalogue = c;
    paint();
    StaggerReveal(Array.from(grid.children), { stagger: 0.06 });
    mount(notIncluded.querySelector("ul"), c.not_included.map((n) => h("li", {}, h("b", {}, n.name), " — ", h("span", { class: "dim" }, n.reason))));
    notIncluded.append(h("p", { class: "dim small" }, c.attribution));
  }).catch((e) => mount(grid, h("section", { class: "card" }, h("p", { class: "bad" }, e.message))));

  return h("div", { class: "stack" }, head, grid, h("div", { class: "grid g-2" }, updatesCard(), templatesCard()), notIncluded);
}
