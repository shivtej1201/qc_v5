// QC V5 Dashboard — app shell, router, polling, views.
import { api } from "./api.js";
import { h, mount, fmt, countUp, toast, stateClass, icon } from "./ui.js";
import { gauge, severityDonut, barList, areaChart } from "./charts.js";
import { createCrawlCore } from "./scene3d.js";

const ACTIVE = new Set(["RUNNING", "STOPPING"]);
const CSV_FILES = ["01_summary.csv", "12_issues.csv", "13_issue_summary.csv", "14_page_issue_summary.csv",
  "02_source_page_audit.csv", "03_url_audit.csv", "04_unique_url_validation.csv", "05_image_audit.csv",
  "06_unique_image_validation.csv", "07_broken_urls.csv", "08_redirect_urls.csv", "09_broken_images.csv",
  "10_external_urls.csv", "11_reconciliation.csv"];
const SEV_COLOR = { HIGH: "var(--sev-high)", MEDIUM: "var(--sev-medium)", LOW: "var(--sev-low)", INFO: "var(--sev-info)" };

// ------------------------------------------------------------------ live history (instantaneous rates)
const history = new Map();   // jobId -> [{t, done, pps, rps}]
function track(jobId, stats) {
  if (!stats) return [];
  const live = stats.live_metrics || {};
  const arr = history.get(jobId) || [];
  const t = performance.now() / 1000, done = stats.source_pages_processed || 0;
  const prev = arr.at(-1);
  const pps = prev && t > prev.t ? Math.max(0, (done - prev.done) / (t - prev.t)) : live.source_pages_per_sec || 0;
  if (!prev || done !== prev.done || t - prev.t > 4) arr.push({ t, done, pps, rps: live.primary_requests_per_sec || 0 });
  while (arr.length > 90) arr.shift();
  history.set(jobId, arr);
  return arr;
}

// ------------------------------------------------------------------ shell
const app = document.getElementById("app");
let scene = null;
let sceneJob = null;       // job the live scene belongs to (kept alive across polling refreshes)
let sceneHostEl = null;
let poll = null;
let viewToken = 0;
let lastHash = null;
const lastKpi = new Map();  // count-ups continue from the previous value instead of restarting at 0

function nav(active) {
  const link = (href, ic, label, key) => h("a", { href, "aria-current": active === key ? "page" : null }, icon(ic), h("span", {}, label));
  return h("nav", { class: "nav", "aria-label": "Primary" },
    link("#/", "overview", "Command Center", "overview"),
    link("#/audits", "audits", "Audits", "audits"),
    link("/", "classic", "Classic console", "classic"));
}

function shell(active, title, subtitle, actions, content) {
  const theme = h("button", { class: "btn icon-btn btn-ghost", title: "Toggle theme", "aria-label": "Toggle light/dark theme", onclick: toggleTheme }, icon("sun"));
  return h("div", { class: "shell" },
    h("aside", { class: "sidebar" },
      h("div", { class: "brand" }, h("div", { class: "brand-mark", "aria-hidden": "true" }, "QC"),
        h("div", {}, "QC V5", h("small", {}, "Audit Intelligence"))),
      nav(active),
      h("div", { class: "sidebar-foot" }, h("span", {}, "Primary-domain budget is shared by all requests."), h("span", {}, "One active crawl per domain."))),
    h("main", { class: "main", id: "main" },
      h("header", { class: "topbar" },
        h("div", {}, h("h1", {}, title), subtitle ? h("p", {}, subtitle) : null),
        h("div", { class: "toolbar" }, actions, theme)),
      content));
}

function toggleTheme() {
  const cur = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = cur;
  try { localStorage.setItem("qc-theme", cur); } catch { /* storage may be blocked */ }
  scene?.setTheme();
}

function newAuditButton() {
  return h("button", { class: "btn btn-primary", onclick: () => openWizard() }, icon("plus"), "New audit");
}

function schedule(fn, anyActive) {
  clearTimeout(poll);
  poll = setTimeout(fn, anyActive ? 2000 : 8000);
}

function disposeScene() { scene?.dispose(); scene = null; sceneJob = null; sceneHostEl = null; }

// ------------------------------------------------------------------ pieces
function kpi(label, value, sub, frac, extra = "") {
  const v = h("div", { class: "value" }, "0");
  if (lastKpi.has(label)) v.dataset.v = String(lastKpi.get(label));
  if (typeof value === "number") lastKpi.set(label, value);
  const bar = frac === undefined ? null : h("div", { class: "bar" }, h("i", { style: { width: `${Math.round(Math.max(0, Math.min(1, frac)) * 100)}%` } }));
  const card = h("div", { class: `card kpi reveal ${extra}` }, h("div", { class: "label" }, label), v, h("div", { class: "sub" }, sub || ""), bar);
  if (typeof value === "number") countUp(v, value); else v.textContent = value ?? "—";
  return card;
}

function heroCard(job, stats) {
  const live = (stats && stats.live_metrics) || {};
  const reuse = scene && job && sceneJob === job.job_id && sceneHostEl;
  const sceneHost = reuse ? sceneHostEl : h("div", { class: "scene" });
  const running = ACTIVE.has(job?.run_state);
  const remaining = stats ? (stats.source_pages_total || 0) - (stats.source_pages_processed || 0) : 0;
  const pps = running ? (history.get(job.job_id)?.at(-1)?.pps || live.source_pages_per_sec || 0) : 0;
  const chip = (val, label) => h("div", { class: "chip" }, h("b", {}, val), h("span", {}, label));
  const card = h("section", { class: "card hero-3d reveal", "aria-label": "Live crawl visualization" },
    sceneHost,
    h("div", { class: "hero-overlay" },
      h("div", { class: "row" },
        h("div", {}, h("div", { class: "title" }, job ? job.name : "No audit yet"),
          h("div", { class: "dim", style: { fontSize: "var(--fs-xs)" } }, job ? `${job.primary_domain} · ${fmt.n(job.url_count)} source pages` : "Start an audit to see the crawl core come alive")),
        h("span", { class: "spacer" }),
        job ? h("span", { class: stateClass(job.run_state) }, job.run_state) : null),
      h("div", { class: "legend", style: { marginTop: "10px" } },
        h("span", {}, h("i", { style: { background: "var(--text-3)" } }), "pending"),
        h("span", {}, h("i", { style: { background: "var(--active)" } }), "in flight"),
        h("span", {}, h("i", { style: { background: "var(--brand-500)" } }), "done"),
        h("span", {}, h("i", { style: { background: "var(--sev-high)" } }), running ? "page error" : "page with HIGH issue")),
      h("div", { class: "chips" },
        chip(running ? fmt.dec(pps) : "—", "pages / s"),
        chip(running ? fmt.dec(live.primary_requests_per_sec) : "—", "req / s (primary)"),
        chip(running && pps > 0 ? fmt.dur(remaining / pps) : "—", "ETA"),
        chip(live.current_target_rps ? `${fmt.dec(live.current_target_rps)}` : "—", "throttle req/s"),
        chip(fmt.n(live.waf_events || 0), "WAF events"))));
  if (reuse) {
    feedScene(job, stats);
  } else {
    disposeScene();
    sceneHostEl = sceneHost;
    sceneJob = job ? job.job_id : null;
    requestAnimationFrame(() => {
      if (sceneHostEl !== sceneHost) return;           // view changed before the frame
      scene = createCrawlCore(sceneHost);
      feedScene(job, stats);
    });
  }
  return card;
}

function feedScene(job, stats) {
  if (!scene || !job) return;
  const total = stats?.source_pages_total || job.url_count || 0;
  const done = stats?.source_pages_processed || 0;
  const errors = (stats?.source_pages_failed || 0) + (stats?.source_pages_blocked || 0) + (stats?.source_pages_timed_out || 0);
  const high = job.health ? job.health.pages_with_high_issues : errors;
  const live = stats?.live_metrics || {};
  scene.update({ total, done, high, rps: ACTIVE.has(job.run_state) ? live.primary_requests_per_sec || 0 : 0,
    waf: live.waf_events || 0, running: ACTIVE.has(job.run_state) });
}

function durationGauge(job, stats) {
  const est = job?.estimate;
  let minutes = null, label = "projected minutes", caption = "";
  if (job && !ACTIVE.has(job.run_state) && job.crawl_seconds) {
    minutes = job.crawl_seconds / 60; label = "actual minutes";
  } else if (job && ACTIVE.has(job.run_state) && stats?.live_metrics) {
    const live = stats.live_metrics, arr = history.get(job.job_id) || [];
    const pps = arr.at(-1)?.pps || live.source_pages_per_sec || 0;
    const left = (stats.source_pages_total || 0) - (stats.source_pages_processed || 0);
    minutes = pps > 0 ? (live.elapsed_sec + left / pps) / 60 : est?.estimated_minutes;
  } else if (est) minutes = est.estimated_minutes;
  if (est) caption = est.meets_20_min_target
    ? `On target: ${est.estimated_minutes} min predicted at ${est.rps} req/s.`
    : `Needs ~${est.rps_needed_for_20_min} req/s sustained for ≤20 min (configured ${est.rps}).`;
  const ok = minutes !== null && minutes <= 20;
  return h("section", { class: "card reveal", "aria-label": "Duration versus the 15–20 minute target" },
    h("div", { class: "card-head" }, h("h2", {}, "Time vs 15–20 min target"), h("span", { class: "dim", style: { fontSize: "var(--fs-xs)" } }, "green arc = target window")),
    gauge({ value: minutes || 0, max: 40, band: [15, 20], label, display: minutes === null ? "—" : fmt.dec(minutes),
      color: minutes === null ? "var(--text-3)" : ok ? "var(--ok)" : "var(--sev-medium)" }),
    h("p", { class: "muted", style: { fontSize: "var(--fs-sm)", textAlign: "center", margin: "6px 0 0" } }, caption));
}

function insightsCard(list) {
  const icons = { danger: "!", warning: "▲", info: "i", success: "✓" };
  return h("section", { class: "card reveal" }, h("h2", {}, "Smart insights"),
    list.length ? h("div", { class: "insights" }, list.map((i) =>
      h("div", { class: `insight ins-${i.kind}` }, h("div", { class: "ico", "aria-hidden": "true" }, icons[i.kind] || "i"),
        h("div", {}, h("b", {}, i.title), h("p", {}, i.message)))))
      : h("p", { class: "muted" }, "Insights appear once an audit is running or finished."));
}

function issueTypesCard(ov) {
  const rows = (ov?.types || []).filter((t) => t.severity !== "INFO").slice(0, 8)
    .map((t) => ({ label: t.issue_type.replaceAll("_", " ").toLowerCase(), value: t.relationships, color: SEV_COLOR[t.severity],
      title: `${t.severity} · ${t.unique_urls} unique URL(s) on ${t.source_pages_affected} page(s) — ${t.recommended_action}` }));
  return h("section", { class: "card reveal" }, h("h2", {}, "Top issue types"),
    rows.length ? barList(rows) : h("p", { class: "muted" }, "No issues yet."));
}

function auditsTable(audits, { compact = false } = {}) {
  if (!audits.length) {
    return h("div", { class: "empty" }, h("h3", {}, "No audits yet"),
      h("p", {}, "Upload a sitemap URL list to audit every page, link and image."), newAuditButton());
  }
  const rows = audits.map((a) => {
    const s = a.stats || {};
    const frac = s.source_pages_total ? s.source_pages_processed / s.source_pages_total : 0;
    return h("tr", { class: "clickable", tabindex: 0, onclick: () => go(`#/audit/${a.job_id}`),
      onkeydown: (e) => { if (e.key === "Enter") go(`#/audit/${a.job_id}`); } },
      h("td", {}, h("b", {}, a.name), h("div", { class: "dim mono" }, a.job_id)),
      h("td", {}, h("span", { class: stateClass(a.run_state) }, a.run_state)),
      h("td", { style: { minWidth: "160px" } }, h("div", { class: "progress" }, h("i", { style: { width: `${(frac * 100).toFixed(1)}%` } })),
        h("div", { class: "dim", style: { fontSize: "var(--fs-xs)", marginTop: "4px" } }, `${fmt.n(s.source_pages_processed || 0)} / ${fmt.n(s.source_pages_total || a.url_count)}`)),
      compact ? null : h("td", { class: "nowrap" }, a.primary_domain),
      h("td", { class: "nowrap" }, a.crawl_seconds ? fmt.dur(a.crawl_seconds) : `~${a.estimate?.estimated_minutes ?? "—"} min`),
      h("td", {}, a.final_qc_status ? h("span", { class: stateClass(a.final_qc_status) }, a.final_qc_status) : h("span", { class: "dim" }, "—")),
      compact ? null : h("td", { class: "nowrap dim" }, fmt.ago(a.created_at)));
  });
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Audit"), h("th", {}, "State"), h("th", {}, "Progress"),
      compact ? null : h("th", {}, "Domain"), h("th", {}, "Duration"), h("th", {}, "QC"), compact ? null : h("th", {}, "Created"))),
    h("tbody", {}, rows)));
}

// ------------------------------------------------------------------ views
async function viewOverview(token) {
  const { audits } = await api.audits();
  if (token !== viewToken) return;
  const focus = audits.find((a) => ACTIVE.has(a.run_state)) || audits[0];
  let detail = null, insights = [], ov = null;
  if (focus) {
    [detail, { insights }] = await Promise.all([api.audit(focus.job_id), api.insights(focus.job_id)]);
    if (detail.package_available) ov = await api.overview(focus.job_id).catch(() => null);
  }
  if (token !== viewToken) return;
  const stats = detail?.stats || {};
  const hist = focus ? track(focus.job_id, stats) : [];
  const live = stats.live_metrics || {};
  const sev = ov?.by_severity || { HIGH: 0, MEDIUM: 0, LOW: 0, INFO: 0 };
  const content = h("div", { class: "stack" },
    h("div", { class: "grid g-hero" }, heroCard(detail, stats), durationGauge(detail, stats)),
    h("div", { class: "grid g-kpi" },
      kpi("Source pages", stats.source_pages_processed || 0, `of ${fmt.n(stats.source_pages_total || focus?.url_count || 0)}`,
        stats.source_pages_total ? stats.source_pages_processed / stats.source_pages_total : 0),
      kpi("URL relationships", stats.url_relationships_persisted || 0, `${fmt.n(stats.new_http_validations || 0)} unique validated once`),
      kpi("Unique URLs", (stats.unique_http_destinations || 0) - (stats.url_validations_pending || 0), `of ${fmt.n(stats.unique_http_destinations || 0)} · reuse ${fmt.dec(stats.reuse_rate_pct || 0)}%`,
        stats.unique_http_destinations ? 1 - (stats.url_validations_pending || 0) / stats.unique_http_destinations : 0),
      kpi("Images", stats.unique_images_processed || 0, `of ${fmt.n(stats.unique_image_urls || 0)} unique · ${fmt.n(stats.image_relationships_persisted || 0)} placements`,
        stats.unique_image_urls ? (stats.unique_images_processed || 0) / stats.unique_image_urls : 0),
      kpi("High issues", ov ? sev.HIGH : "—", ov ? `${fmt.n(ov.total)} issues in total` : "after the report is built", undefined, ov && sev.HIGH ? "danger" : ""),
      kpi("Health score", detail?.health?.score !== undefined && detail?.health?.score !== null ? `${fmt.dec(detail.health.score)}%` : "—",
        detail?.health ? `${fmt.n(detail.health.pages_with_high_issues)} pages with HIGH issues` : "share of pages without HIGH issues")),
    h("div", { class: "grid g-2" },
      h("section", { class: "card reveal" }, h("div", { class: "card-head" }, h("h2", {}, "Live throughput"),
        h("div", { class: "legend" }, h("span", {}, h("i", { style: { background: "var(--brand-500)" } }), "pages/s"),
          h("span", {}, h("i", { style: { background: "var(--accent-500)" } }), "req/s primary"))),
        hist.length > 1 ? areaChart([{ label: "pages/s", values: hist.map((p) => p.pps), color: "var(--brand-500)" },
          { label: "req/s", values: hist.map((p) => p.rps), color: "var(--accent-500)" }], { yLabel: "/s" })
          : h("p", { class: "muted" }, focus && ACTIVE.has(focus.run_state) ? "Collecting samples…" : "Shown while an audit is running."),
        h("h3", { style: { marginTop: "var(--s-4)" } }, focus && ACTIVE.has(focus.run_state) ? "Request mix (req/s)" : "Request mix (run average, req/s)"),
        barList([{ label: "source pages", value: live.source_request_rate || 0, color: "var(--brand-500)" },
          { label: "link checks", value: live.url_request_rate || 0, color: "var(--accent-500)" },
          { label: "image checks", value: live.image_request_rate || 0, color: "var(--sev-low)" }])),
      h("section", { class: "card reveal" }, h("h2", {}, "Issues by severity"),
        ov ? severityDonut(sev) : h("p", { class: "muted" }, "Available when the audit report has been built."))),
    h("div", { class: "grid g-2" }, issueTypesCard(ov), insightsCard(insights)),
    h("section", { class: "card reveal" }, h("div", { class: "card-head" }, h("h2", {}, "Recent audits"), h("a", { href: "#/audits" }, "View all")),
      auditsTable(audits.slice(0, 6), { compact: true })));
  mount(app, shell("overview", "Command Center", "Live crawl, validation and issue intelligence", newAuditButton(), content));
  schedule(() => render(), audits.some((a) => ACTIVE.has(a.run_state)));
}

async function viewAudits(token) {
  const { audits } = await api.audits();
  if (token !== viewToken) return;
  disposeScene();
  mount(app, shell("audits", "Audits", `${audits.length} audit(s) · one active crawl per domain`, newAuditButton(),
    h("section", { class: "card reveal" }, auditsTable(audits))));
  schedule(() => render(), audits.some((a) => ACTIVE.has(a.run_state)));
}

const issueState = { severity: "HIGH", issue_type: "", q: "", offset: 0, limit: 25 };   // fix-first view by default

async function viewAudit(token, id) {
  const [d, { insights }] = await Promise.all([api.audit(id), api.insights(id)]);
  if (token !== viewToken) return;
  const stats = d.stats || {};
  track(id, stats);
  const running = ACTIVE.has(d.run_state);
  const actions = [
    h("a", { class: "btn btn-ghost", href: "#/audits" }, icon("back"), "Audits"),
    running ? h("button", { class: "btn btn-danger", onclick: async () => { await act(() => api.stop(id), "Stopping gracefully — in-flight work is saved."); } }, icon("stop"), "Stop")
      : ["STOPPED", "INTERRUPTED", "PARTIAL", "FAILED"].includes(d.run_state)
        ? h("button", { class: "btn", onclick: async () => { await act(() => api.resume(id), "Resumed from checkpoint."); } }, icon("play"), "Resume") : null,
    d.package_available ? h("a", { class: "btn btn-primary", href: api.packageUrl(id) }, icon("download"), "Download ZIP") : null,
  ];
  // Each stage is progress against ITS OWN population (one shared scale would make 100% of
  // 1,500 pages look empty next to 55,000 relationships).
  const stage = (label, val, total, color) => {
    const frac = total ? Math.min(1, val / total) : 0;
    return h("div", { class: "bar-row" }, h("span", { class: "truncate" }, label),
      h("div", { class: "track" }, h("i", { style: { width: `${(frac * 100).toFixed(1)}%`, background: color } })),
      h("span", { class: "num" }, total ? `${fmt.dec(frac * 100, 0)}%` : "—"));
  };
  const urlsDone = (stats.unique_http_destinations || 0) - (stats.url_validations_pending || 0);
  const funnel = h("section", { class: "card reveal" }, h("h2", {}, "Pipeline"),
    h("div", { class: "bars" },
      stage(`source pages ${fmt.n(stats.source_pages_processed || 0)} / ${fmt.n(stats.source_pages_total || d.url_count)}`,
        stats.source_pages_processed || 0, stats.source_pages_total || d.url_count, "var(--brand-500)"),
      stage(`unique URLs ${fmt.n(urlsDone)} / ${fmt.n(stats.unique_http_destinations || 0)}`, urlsDone, stats.unique_http_destinations || 0, "var(--ok)"),
      stage(`unique images ${fmt.n(stats.unique_images_processed || 0)} / ${fmt.n(stats.unique_image_urls || 0)}`,
        stats.unique_images_processed || 0, stats.unique_image_urls || 0, "var(--sev-low)")),
    h("div", { class: "grid g-2", style: { marginTop: "var(--s-4)" } },
      h("div", { class: "kpi" }, h("div", { class: "label" }, "URL relationships"), h("div", { class: "value", style: { fontSize: "var(--fs-xl)" } }, fmt.n(stats.url_relationships_persisted || 0))),
      h("div", { class: "kpi" }, h("div", { class: "label" }, "Image placements"), h("div", { class: "value", style: { fontSize: "var(--fs-xl)" } }, fmt.n(stats.image_relationships_persisted || 0)))),
    h("p", { class: "dim", style: { fontSize: "var(--fs-xs)", marginTop: "var(--s-3)" } },
      `Each unique URL/image is requested once; ${fmt.n(stats.reused_http_validations || 0)} link and ${fmt.n(stats.reused_image_validations || 0)} image relationships reused a result.`));

  let reportSection;
  if (d.package_available) {
    const [ov, pagesRes] = await Promise.all([api.overview(id), api.pages(id, 12)]);
    if (token !== viewToken) return;
    reportSection = [
      h("div", { class: "grid g-3" },
        h("section", { class: "card reveal" }, h("h2", {}, "Issues by severity"), severityDonut(ov.by_severity)),
        issueTypesCard(ov),
        h("section", { class: "card reveal" }, h("h2", {}, "Biggest wins"),
          ov.top_high_urls.length ? h("div", { class: "bars" }, ov.top_high_urls.slice(0, 6).map((u) =>
            h("div", {}, h("div", { class: "row" }, h("span", { class: "sev sev-HIGH" }, u.issue_type), h("span", { class: "spacer" }), h("b", {}, fmt.n(u.relationships))),
              h("span", { class: "url mono dim", title: u.url }, u.url))))
            : h("p", { class: "muted" }, "No HIGH issues."))),
      issueExplorer(id, ov),
      h("div", { class: "grid g-2" },
        h("section", { class: "card reveal" }, h("h2", {}, "Worst pages"), worstPages(pagesRes.pages)),
        h("section", { class: "card reveal" }, h("h2", {}, "Report files"),
          h("div", { class: "file-list" }, CSV_FILES.map((f) => h("a", { href: api.fileUrl(id, f), class: "file-link" }, icon("download"), h("span", { class: "mono" }, f)))))),
    ];
  } else {
    reportSection = [h("section", { class: "card reveal" }, h("h2", {}, "Report"),
      h("p", { class: "muted" }, running ? "The issue report (14 CSVs + ZIP) is built automatically when the crawl finishes."
        : "No report: the audit has not finished. Resume it to complete the crawl and build the report."))];
  }
  const content = h("div", { class: "stack" },
    h("div", { class: "grid g-hero" }, heroCard(d, stats), durationGauge(d, stats)),
    h("div", { class: "grid g-kpi" },
      kpi("Source pages", stats.source_pages_processed || 0, `of ${fmt.n(stats.source_pages_total || d.url_count)}`,
        stats.source_pages_total ? stats.source_pages_processed / stats.source_pages_total : 0),
      kpi("Failed / timed out", (stats.source_pages_failed || 0) + (stats.source_pages_timed_out || 0), "source pages"),
      kpi("Blocked", stats.source_pages_blocked || 0, "robots / WAF / SSRF"),
      kpi("Unique URLs", stats.unique_http_destinations || 0, `${fmt.n(stats.url_validations_pending || 0)} pending`),
      kpi("Unique images", stats.unique_image_urls || 0, `${fmt.n(stats.unique_images_pending || 0)} pending`),
      kpi("Health score", d.health?.score !== undefined && d.health?.score !== null ? `${fmt.dec(d.health.score)}%` : "—", "pages without HIGH issues")),
    h("div", { class: "grid g-2" }, funnel, insightsCard(insights)),
    reportSection,
    h("details", { class: "card" }, h("summary", {}, "Worker log (last lines)"),
      h("pre", { class: "mono dim", style: { whiteSpace: "pre-wrap", fontSize: "var(--fs-xs)" } }, (d.log_tail || []).join("\n"))));
  mount(app, shell("audits", d.name, `${d.job_id} · ${d.primary_domain} · preset ${d.preset}`, actions, content));
  schedule(() => render(), running);
}

function issueExplorer(id, ov) {
  const tableHost = h("div", {});
  const pager = h("div", { class: "row", style: { marginTop: "var(--s-3)" } });
  const types = (ov.types || []).map((t) => t.issue_type);
  const segBtn = (val, label) => h("button", { "aria-pressed": String(issueState.severity === val), onclick: (e) => {
    issueState.severity = val; issueState.offset = 0;
    e.currentTarget.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === e.currentTarget)));
    load();
  } }, label);
  const typeSel = h("select", { "aria-label": "Issue type", onchange: (e) => { issueState.issue_type = e.target.value; issueState.offset = 0; load(); } },
    h("option", { value: "" }, "All issue types"), [...new Set(types)].map((t) => h("option", { value: t, selected: issueState.issue_type === t || null }, t)));
  let tmr;
  const search = h("input", { type: "search", placeholder: "Search URL or page…", value: issueState.q, "aria-label": "Search issues",
    oninput: (e) => { clearTimeout(tmr); tmr = setTimeout(() => { issueState.q = e.target.value; issueState.offset = 0; load(); }, 300); } });

  async function load() {
    clearTimeout(poll);   // the explorer is interactive; do not re-render under the user
    mount(tableHost, h("div", { class: "skeleton", style: { height: "220px" } }));
    const res = await api.issues(id, { severity: issueState.severity, issue_type: issueState.issue_type, q: issueState.q,
      offset: issueState.offset, limit: issueState.limit });
    const rows = res.items.map((r) => h("tr", {},
      h("td", {}, h("span", { class: `sev sev-${r.severity}` }, r.severity)),
      h("td", {}, h("b", {}, r.issue_type), h("div", { class: "dim" }, r.category, r.element_type ? ` · ${r.element_type}` : "")),
      h("td", {}, h("a", { class: "url mono", href: r.normalized_url || r.found_url, target: "_blank", rel: "noopener noreferrer nofollow", title: r.normalized_url }, r.normalized_url || r.found_url),
        h("div", { class: "dim" }, "on ", h("a", { class: "url mono", href: r.source_page_url, target: "_blank", rel: "noopener noreferrer nofollow", title: r.source_page_url }, r.source_page_url)),
        r.anchor_or_alt_text ? h("div", { class: "dim truncate", title: r.anchor_or_alt_text }, `“${r.anchor_or_alt_text}”`) : null),
      h("td", { class: "nowrap" }, r.http_status || "—", r.hop_count && r.hop_count !== "0" ? h("div", { class: "dim" }, `${r.hop_count} hop(s)`) : null),
      h("td", {}, h("div", {}, r.detail), h("div", { class: "dim", style: { marginTop: "4px" } }, r.recommended_action))));
    mount(tableHost, res.items.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Severity"), h("th", {}, "Issue"), h("th", {}, "URL / page"), h("th", {}, "Status"), h("th", {}, "Detail & fix"))),
      h("tbody", {}, rows))) : h("div", { class: "empty" }, "No issues match these filters."));
    const from = res.matched ? res.offset + 1 : 0, to = Math.min(res.matched, res.offset + res.limit);
    mount(pager, h("span", { class: "dim" }, `${fmt.n(from)}–${fmt.n(to)} of ${fmt.n(res.matched)}`), h("span", { class: "spacer" }),
      h("button", { class: "btn", disabled: res.offset === 0 || null, onclick: () => { issueState.offset = Math.max(0, res.offset - res.limit); load(); } }, "Previous"),
      h("button", { class: "btn", disabled: to >= res.matched || null, onclick: () => { issueState.offset = res.offset + res.limit; load(); } }, "Next"));
  }
  load();
  return h("section", { class: "card reveal", "aria-label": "Issue explorer" },
    h("div", { class: "card-head" }, h("h2", {}, "Issue explorer"), h("a", { href: api.fileUrl(id, "12_issues.csv") }, "Download all (CSV)")),
    h("div", { class: "filters" }, h("div", { class: "seg", role: "group", "aria-label": "Severity" },
      segBtn("", "All"), segBtn("HIGH", "High"), segBtn("MEDIUM", "Medium"), segBtn("LOW", "Low"), segBtn("INFO", "Info")),
      typeSel, search),
    tableHost, pager);
}

function worstPages(pages) {
  if (!pages.length) return h("p", { class: "muted" }, "No page-level issues.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Page"), h("th", {}, "High"), h("th", {}, "Med"), h("th", {}, "Top issues"))),
    h("tbody", {}, pages.map((p) => h("tr", {},
      h("td", {}, h("a", { class: "url mono", href: p.source_page_url, target: "_blank", rel: "noopener noreferrer nofollow", title: p.source_page_url }, p.source_page_url)),
      h("td", {}, h("b", { style: { color: Number(p.high) ? "var(--sev-high)" : "inherit" } }, p.high)),
      h("td", {}, p.medium),
      h("td", { class: "dim" }, p.top_issue_types))))));
}

async function act(fn, ok) {
  try { await fn(); toast(ok); render(); } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------------ new-audit wizard
async function openWizard() {
  const { presets } = await api.presets();
  const st = { step: 1, mode: "paste", urls: [], file: null, preset: "target", rps: presets.target.target_rps, imagesOnPrimary: false, name: "", domain: "" };
  const back = h("div", { class: "modal-backdrop", role: "dialog", "aria-modal": "true", "aria-labelledby": "wiz-title" });
  const close = () => { back.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  const modal = h("div", { class: "modal" });
  back.append(modal);
  document.body.append(back);

  const count = () => (st.mode === "paste" ? st.urls.length : st.fileCount || 0);
  let nextBtn = null;
  const canNext = () => st.step !== 1 || (st.mode === "paste" ? st.urls.length > 0 : !!st.file);
  // the Continue button must follow the input live (paste / file choice), not only the initial draw
  const refreshNext = () => { if (nextBtn) nextBtn.disabled = !canNext(); };
  function draw() {
    const steps = h("div", { class: "steps" }, ["1 · Pages", "2 · Speed", "3 · Launch"].map((s, i) => h("span", { "aria-current": st.step === i + 1 ? "step" : null }, s)));
    let body;
    if (st.step === 1) {
      const ta = h("textarea", { placeholder: "https://www.example.com/page-1\nhttps://www.example.com/page-2\n…", "aria-label": "Source page URLs, one per line",
        oninput: (e) => { st.urls = e.target.value.split(/\s+/).map((u) => u.trim()).filter((u) => /^https?:\/\//i.test(u)); info.textContent = `${fmt.n(st.urls.length)} valid URL(s)`; refreshNext(); } },
        st.urls.join("\n"));
      const info = h("div", { class: "dim" }, `${fmt.n(st.urls.length)} valid URL(s)`);
      const fileInput = h("input", { type: "file", accept: ".txt,.csv,.xlsx", class: "sr-only", id: "wiz-file", onchange: (e) => pick(e.target.files[0]) });
      const dz = h("label", { class: "dropzone", for: "wiz-file" }, st.file ? `${st.file.name} (${fmt.n(st.file.size / 1024)} KB)` : "Drop a .txt / .csv / .xlsx file here, or click to choose");
      const pick = (f) => { if (!f) return; st.file = f; st.fileCount = 0; dz.textContent = `${f.name} (${fmt.n(f.size / 1024)} KB)`; refreshNext();
        if (f.name.endsWith(".txt")) f.text().then((t) => { st.fileCount = t.split(/\r?\n/).filter((l) => l.trim()).length; dz.textContent += ` · ${fmt.n(st.fileCount)} lines`; }); };
      dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
      dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
      dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("drag"); pick(e.dataTransfer.files[0]); });
      body = [h("div", { class: "seg", role: "group" },
        h("button", { "aria-pressed": String(st.mode === "paste"), onclick: () => { st.mode = "paste"; draw(); } }, "Paste URLs"),
        h("button", { "aria-pressed": String(st.mode === "file"), onclick: () => { st.mode = "file"; draw(); } }, "Upload file")),
        st.mode === "paste" ? h("div", { class: "field" }, h("label", {}, "Sitemap source pages"), ta, info) : h("div", {}, fileInput, dz)];
    } else if (st.step === 2) {
      const estBox = h("div", { class: "estimate" }, h("div", { class: "skeleton", style: { width: "220px", height: "40px" } }));
      const refresh = async () => {
        const n = count() || 10000;
        const e = await api.estimate(n, st.rps, st.imagesOnPrimary);
        mount(estBox,
          h("div", {}, h("div", { class: "dim" }, `${fmt.n(n)} pages${count() ? "" : " (example)"} @ ${st.rps} req/s`), h("b", {}, `${e.estimated_minutes} min`)),
          h("div", {}, h("span", { class: e.meets_20_min_target ? "pill st-PASS" : "pill st-PARTIAL" }, e.meets_20_min_target ? "meets 15–20 min" : "over 20 min")),
          h("div", { class: "dim", style: { fontSize: "var(--fs-xs)", maxWidth: "320px" } },
            `≤20 min needs ~${e.rps_needed_for_20_min} req/s sustained on the primary domain. Only raise the rate if the edge has been measured to tolerate it.`));
      };
      const presetCards = Object.entries(presets).map(([k, p]) => h("button", { class: "preset", role: "radio", "aria-checked": String(st.preset === k),
        onclick: () => { st.preset = k; st.rps = p.target_rps; draw(); } }, h("b", {}, p.label), h("small", {}, p.description)));
      const slider = h("input", { type: "range", min: 1, max: 30, step: 0.5, value: st.rps, "aria-label": "Requests per second",
        oninput: (e) => { st.rps = Number(e.target.value); rv.textContent = `${st.rps} req/s`; refresh(); } });
      const rv = h("b", { class: "nowrap", style: { minWidth: "84px", textAlign: "right" } }, `${st.rps} req/s`);
      body = [h("div", { class: "preset-grid", role: "radiogroup", "aria-label": "Speed preset" }, presetCards),
        h("div", { class: "field" }, h("label", {}, "Primary-domain request rate"), h("div", { class: "row" }, slider, rv)),
        h("label", { class: "row dim", style: { fontSize: "var(--fs-sm)" } },
          h("input", { type: "checkbox", checked: st.imagesOnPrimary || null, onchange: (e) => { st.imagesOnPrimary = e.target.checked; refresh(); } }),
          "Images are served from the same rate-limited domain"),
        estBox];
      refresh();
    } else {
      body = [h("div", { class: "field" }, h("label", { for: "wiz-name" }, "Audit name"),
        h("input", { type: "text", id: "wiz-name", value: st.name, placeholder: "e.g. Bajaj Mall PDP sitemap — Nov", oninput: (e) => { st.name = e.target.value; } })),
        h("div", { class: "field" }, h("label", { for: "wiz-domain" }, "Primary domain (shared rate budget)"),
          h("input", { type: "text", id: "wiz-domain", value: st.domain, placeholder: "auto-detected from the URLs, e.g. bajajfinserv.in", oninput: (e) => { st.domain = e.target.value; } })),
        h("div", { class: "estimate" }, h("div", {}, h("div", { class: "dim" }, "Pages"), h("b", {}, count() ? fmt.n(count()) : "from file")),
          h("div", {}, h("div", { class: "dim" }, "Preset"), h("b", {}, presets[st.preset].label)),
          h("div", {}, h("div", { class: "dim" }, "Rate"), h("b", {}, `${st.rps} req/s`)))];
    }
    nextBtn = st.step < 3 ? h("button", { class: "btn btn-primary", disabled: !canNext() || null, onclick: () => { st.step++; draw(); } }, "Continue") : null;
    mount(modal,
      h("header", {}, h("h2", { id: "wiz-title", style: { margin: 0, fontSize: "var(--fs-lg)" } }, "New on-page audit"), h("span", { class: "spacer" }), steps),
      h("div", { class: "body" }, body),
      h("footer", {},
        h("button", { class: "btn btn-ghost", onclick: close }, "Cancel"),
        st.step > 1 ? h("button", { class: "btn", onclick: () => { st.step--; draw(); } }, "Back") : null,
        nextBtn || h("button", { class: "btn btn-primary", onclick: launch }, icon("play"), "Launch audit")));
  }
  async function launch() {
    try {
      const overrides = { target_rps: st.rps, max_rps: st.rps };
      let job;
      if (st.mode === "paste") {
        job = await api.start({ name: st.name || "On-page audit", urls: st.urls, preset: st.preset, overrides, primary_domain: st.domain || null });
      } else {
        const f = new FormData();
        f.append("file", st.file); f.append("name", st.name || st.file.name); f.append("preset", st.preset); f.append("target_rps", String(st.rps));
        if (st.domain) f.append("primary_domain", st.domain);
        job = await api.upload(f);
      }
      close();
      toast(`Audit started: ${fmt.n(job.url_count)} pages`);
      go(`#/audit/${job.job_id}`);
    } catch (e) { toast(e.message, "err"); }
  }
  draw();
}

// ------------------------------------------------------------------ router
function go(hash) { if (location.hash === hash) render(); else location.hash = hash; }

async function render() {
  clearTimeout(poll);
  const token = ++viewToken;
  const hash = location.hash || "#/";
  // same view re-rendered by polling -> quiet refresh (no entrance/chart animation replays)
  document.documentElement.classList.toggle("refreshing", hash === lastHash);
  if (hash !== lastHash) lastKpi.clear();
  lastHash = hash;
  try {
    const m = hash.match(/^#\/audit\/(onpage_[a-f0-9]{12})$/);
    if (m) await viewAudit(token, m[1]);
    else if (hash.startsWith("#/audits")) await viewAudits(token);
    else await viewOverview(token);
  } catch (e) {
    if (token !== viewToken) return;
    disposeScene();
    mount(app, shell("overview", "Something went wrong", "", newAuditButton(),
      h("section", { class: "card" }, h("p", {}, e.message || String(e)), h("button", { class: "btn", onclick: render }, icon("refresh"), "Retry"))));
    schedule(() => render(), false);
  }
}

window.addEventListener("hashchange", () => { issueState.offset = 0; render(); });
try {
  const saved = localStorage.getItem("qc-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  else if (window.matchMedia("(prefers-color-scheme: light)").matches) document.documentElement.dataset.theme = "light";
} catch { /* storage may be blocked */ }
render();
