// QC V5 Dashboard — data explorers built ONLY on the frozen API (docs/api/API_CONTRACT.md).
// Large datasets: dataGrid (virtualized + server-side paging/filter/sort). All crawled text is inserted as text.
import { api } from "./api.js";
import { h, mount, fmt, toast, icon } from "./ui.js";
import { dataGrid } from "./grid.js";
import { CardReveal, StaggerReveal, ModalTransition } from "./motion.js";

const SEV = ["HIGH", "MEDIUM", "LOW", "INFO"];
const ISSUE_STATUSES = ["OPEN", "IN_PROGRESS", "VERIFIED", "RESOLVED", "DISMISSED"];
const go = (hash) => { location.hash = hash; };
const ext = (url, text) => url ? h("a", { class: "url mono", href: url, target: "_blank", rel: "noopener noreferrer nofollow", title: url }, text || url) : "—";
const sevPill = (s) => h("span", { class: `sev sev-${s}` }, s);
const statusCell = (code, bucket) => {
  const c = Number(code);
  const cls = !code ? "st-unknown" : c >= 500 ? "st-5xx" : c >= 400 ? "st-4xx" : c >= 300 ? "st-3xx" : "st-2xx";
  return h("span", { class: `code ${cls}`, title: bucket || "" }, code || bucket || "—");
};
const sel = (label, options, value, onchange) => h("select", { "aria-label": label, onchange: (e) => onchange(e.target.value) },
  options.map(([v, t]) => h("option", { value: v, selected: v === value || null }, t)));
function search(placeholder, onchange) {
  let t;
  return h("input", { type: "search", placeholder, "aria-label": placeholder, oninput: (e) => { clearTimeout(t); t = setTimeout(() => onchange(e.target.value.trim()), 300); } });
}
function card(title, ...body) {
  const c = h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, title)), ...body);
  CardReveal(c);
  return c;
}
function kv(rows) {
  return h("dl", { class: "kv" }, rows.filter(([, v]) => v !== undefined).map(([k, v]) =>
    [h("dt", {}, k), h("dd", {}, v === null || v === "" ? "—" : v instanceof Node ? v : String(v))]));
}

// ------------------------------------------------------------------ URL audit
export function urlsView(id) {
  const state = {};
  const grid = dataGrid({
    label: "URL audit", storageKey: "urls",
    fetchPage: (p) => api.urls(id, p),
    onRowClick: (r) => go(`#/audit/${id}/url?u=${encodeURIComponent(r.source_page_url)}`),
    columns: [
      { key: "source_page_url", label: "URL", width: "minmax(260px, 2.2fr)", sortKey: "url", render: (r) => h("span", { class: "url mono", title: r.source_page_url }, r.source_page_url) },
      { key: "http_status", label: "Status", width: "104px", sortKey: "status", render: (r) => statusCell(r.initial_http_status || r.http_status) },
      { key: "crawl_state", label: "State", width: "110px" },
      { key: "redirect_count", label: "Hops", width: "64px", num: true },
      { key: "response_time_ms", label: "ms", width: "80px", num: true, sortKey: "time", render: (r) => fmt.n(r.response_time_ms) },
      { key: "page_title", label: "Title", width: "minmax(180px, 1.4fr)", render: (r) => h("span", { class: "truncate", title: r.page_title || "" }, r.page_title || "—") },
      { key: "indexable", label: "Index", width: "70px", render: (r) => r.indexable === null || r.indexable === undefined ? "—" : r.indexable ? "yes" : h("b", { class: "bad" }, "no") },
      { key: "h1_count", label: "H1", width: "52px", num: true },
      { key: "word_count", label: "Words", width: "76px", num: true, render: (r) => fmt.n(r.word_count) },
      { key: "relationships_count", label: "Links", width: "70px", num: true, sortKey: "links" },
      { key: "image_relationships_count", label: "Imgs", width: "64px", num: true },
      { key: "business", label: "Business", width: "120px" },
      { key: "page_type", label: "Page type", width: "120px" },
    ],
  });
  const set = (k, v) => { state[k] = v; grid.setParams({ ...grid.params(), ...state }); };
  grid.setParams({});
  return card("URL audit", h("div", { class: "filters" },
    sel("HTTP status", [["", "Any status"], ["2xx", "2xx"], ["3xx", "3xx"], ["4xx", "4xx"], ["5xx", "5xx"]], "", (v) => set("status_bucket", v)),
    sel("Crawl state", [["", "Any state"], ["COMPLETED", "Completed"], ["FAILED", "Failed"], ["BLOCKED", "Blocked"], ["TIMED_OUT", "Timed out"], ["PENDING", "Pending"]], "", (v) => set("crawl_state", v)),
    search("Search URL…", (v) => set("q", v))), grid.el);
}

// ------------------------------------------------------------------ URL detail
export async function urlDetailView(id, url) {
  const d = await api.urlDetail(id, url);
  const ok = (v) => v ? "yes" : "no";
  const sections = [
    card("Overview", kv([["Crawl state", d.overview.crawl_state], ["Business", d.overview.business], ["Page type", d.overview.page_type],
      ["Language", d.overview.language], ["Sitemap", d.overview.source_sitemap], ["Checked", d.overview.checked_at]])),
    card("HTTP", kv([["Initial status", statusCell(d.http.initial_http_status)], ["Final status", statusCell(d.http.http_status)],
      ["Final URL", ext(d.http.final_url)], ["Content type", d.http.content_type], ["Error", d.http.error_type ? `${d.http.error_type}: ${d.http.error_message || ""}` : null],
      ["Robots.txt", d.http.robots_status], ["WAF", d.http.waf_status], ["Attempts", d.http.attempts]])),
    card("Redirect", d.redirect.count ? h("ol", { class: "chain" }, d.redirect.chain.map((hop) =>
      h("li", {}, statusCell(hop.status), " ", h("span", { class: "mono" }, hop.url || hop.from || ""), hop.location ? [" → ", h("span", { class: "mono" }, hop.location)] : null)),
    h("li", {}, statusCell(d.http.http_status), " ", h("span", { class: "mono" }, d.redirect.final_url || ""))) : h("p", { class: "muted" }, "No redirect.")),
    card("Canonical & robots", kv([["Canonical", d.canonical ? ext(d.canonical.url) : "missing"], ["Canonical status", d.canonical?.status],
      ["Self-referencing", d.canonical ? ok(d.canonical.self_referencing) : null], ["Meta robots", d.robots.meta_robots], ["X-Robots-Tag", d.robots.x_robots_tag],
      ["Indexable", d.robots.indexable === null || d.robots.indexable === undefined ? null : ok(d.robots.indexable)]])),
    card("Metadata", kv([["Title", d.metadata.title], ["Title length", d.metadata.title_length], ["Titles on page", d.metadata.title_count],
      ["Meta description", d.metadata.meta_description], ["Description length", d.metadata.meta_description_length],
      ["html lang", d.metadata.html_lang], ["Viewport", d.metadata.has_viewport === undefined ? null : ok(d.metadata.has_viewport)],
      ["og:title", d.metadata.og_title], ["og:image", d.metadata.og_image], ["twitter:card", d.metadata.twitter_card], ["Meta refresh", d.metadata.meta_refresh]])),
    card("Headings & content", kv([["H1 count", d.headings.h1_count], ["H1", d.headings.h1_text], ["H2 count", d.headings.h2_count],
      ["Skipped levels", d.headings.heading_skips], ["Words", d.content.word_count], ["JS app shell suspected", d.content.js_rendering_suspected === undefined ? null : ok(d.content.js_rendering_suspected)]])),
    card("Structured data & hreflang", kv([["JSON-LD blocks", d.structured_data.blocks], ["Invalid blocks", d.structured_data.invalid], ["Types", d.structured_data.types]]),
      d.hreflang.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "hreflang"), h("th", {}, "URL"))),
        h("tbody", {}, d.hreflang.map(([code, u]) => h("tr", {}, h("td", {}, code), h("td", {}, ext(u)))))) : null),
    card("Performance & security", kv([["Response time (ms)", fmt.n(d.performance.response_time_ms)], ["HTML bytes", fmt.n(d.performance.html_bytes)],
      ["Truncated", d.performance.html_truncated ? "yes" : "no"], ["Insecure resources", d.security.insecure_resources], ["Examples", d.security.examples]])),
    card("Links & images", h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Type"), h("th", {}, "Scope"), h("th", {}, "Relationships"), h("th", {}, "Broken"))),
      h("tbody", {}, d.links.by_type.map((t) => h("tr", {}, h("td", {}, t.url_type), h("td", {}, t.scope), h("td", { class: "num" }, fmt.n(t.relationships)), h("td", { class: "num" }, fmt.n(t.broken)))))),
    kv([["Image placements", d.images.placements], ["Missing alt", d.images.missing_alt], ["Broken images", d.images.broken]]),
    h("div", { class: "row", style: { marginTop: "var(--s-3)" } },
      h("a", { class: "btn", href: `#/audit/${id}/links?page=${encodeURIComponent(url)}` }, "Open in Link audit"),
      h("a", { class: "btn", href: `#/audit/${id}/images?page=${encodeURIComponent(url)}` }, "Open in Image audit"))),
  ];
  const issues = card(`Issues on this page (${d.issues.length})`, d.issues.length ? h("div", { class: "table-wrap detail-issues" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Severity"), h("th", {}, "Issue"), h("th", {}, "Evidence"), h("th", {}, "Fix"), h("th", {}, "Status"))),
    h("tbody", {}, d.issues.map((i) => h("tr", { class: "clickable", onclick: () => openIssue(id, i.issue_key) },
      h("td", {}, sevPill(i.severity)), h("td", {}, h("b", {}, i.issue_type), h("div", { class: "dim" }, i.category)),
      h("td", {}, h("div", {}, i.detail), i.normalized_url && i.normalized_url !== url ? h("div", { class: "dim mono truncate" }, i.normalized_url) : null),
      h("td", { class: "dim" }, i.recommended_action), h("td", {}, h("span", { class: `pill st-${i.status}` }, i.status)))))))
    : h("p", { class: "muted" }, "No issues."));
  const raw = h("details", { class: "card" }, h("summary", {}, "Raw evidence (stored rows)"),
    h("pre", { class: "mono raw" }, JSON.stringify(d.raw, null, 2)));
  const out = h("div", { class: "stack" }, h("div", { class: "detail-head card" },
    h("div", { class: "dim" }, "URL detail"), h("h2", { class: "mono", style: { wordBreak: "break-all", margin: "4px 0 0" } }, url)),
  h("div", { class: "grid g-2 detail-grid" }, sections), issues, raw);
  StaggerReveal(sections);
  return out;
}

// ------------------------------------------------------------------ Link audit
export function linksView(id, query) {
  const state = { source_page: query.get("page") || "" };
  const grid = dataGrid({
    label: "Link audit", storageKey: "links",
    fetchPage: (p) => api.links(id, p),
    onRowClick: (r) => go(`#/audit/${id}/url?u=${encodeURIComponent(r.source_page_url)}`),
    columns: [
      { key: "source_page_url", label: "Source page", width: "minmax(200px, 1.4fr)", sortKey: "page", render: (r) => h("span", { class: "url mono", title: r.source_page_url }, r.source_page_url) },
      { key: "normalized_url", label: "Destination", width: "minmax(220px, 1.6fr)", sortKey: "url", render: (r) => h("span", { class: "url mono", title: r.normalized_url }, r.normalized_url) },
      { key: "http_status", label: "Status", width: "104px", sortKey: "status", render: (r) => statusCell(r.initial_http_status || r.http_status, r.status_bucket || r.validation_source) },
      { key: "final_url", label: "Final URL", width: "minmax(160px, 1fr)", render: (r) => r.redirect_count ? h("span", { class: "url mono", title: r.final_url }, r.final_url) : "—" },
      { key: "url_type", label: "Type", width: "118px" },
      { key: "internal_external", label: "Scope", width: "96px" },
      { key: "anchor_text", label: "Anchor", width: "minmax(120px, 1fr)", render: (r) => h("span", { class: "truncate", title: r.anchor_text || "" }, r.anchor_text || "—") },
      { key: "rel", label: "nofollow", width: "80px", render: (r) => /(^|\s)nofollow(\s|$)/i.test(r.rel || "") ? "yes" : "" },
      { key: "component", label: "Component", width: "120px" },
      { key: "language", label: "Lang", width: "64px" },
      { key: "business", label: "Business", width: "110px" },
      { key: "page_type", label: "Page type", width: "110px" },
      { key: "validation_source", label: "Validation", width: "120px" },
    ],
  });
  const set = (k, v) => { state[k] = v; grid.setParams({ ...grid.params(), ...state }); };
  grid.setParams(state);
  return card("Link audit", h("div", { class: "filters" },
    sel("Scope", [["", "Internal + external"], ["INTERNAL", "Internal"], ["EXTERNAL", "External"], ["NON_HTTP", "Non-HTTP"], ["INVALID", "Invalid"]], "", (v) => set("scope", v)),
    sel("URL type", [["", "All URL types"], ...["ANCHOR", "CANONICAL", "HREFLANG", "IFRAME", "SCRIPT", "STYLESHEET", "LINK_RESOURCE", "CSS_RESOURCE", "MEDIA", "EMBED", "SOCIAL_URL", "JSONLD_URL", "META_REFRESH", "FORM", "AREA", "LINK_META"].map((t) => [t, t])], "", (v) => set("url_type", v)),
    sel("Status", [["", "Any status"], ["2xx", "2xx"], ["3xx", "3xx"], ["4xx", "4xx"], ["5xx", "5xx"], ["Timeout", "Timeout"], ["DNS/Network", "DNS / network"], ["Robots blocked", "Robots blocked"], ["WAF blocked", "WAF blocked"]], "", (v) => set("status_bucket", v)),
    search("Search destination or anchor…", (v) => set("q", v)),
    state.source_page ? h("button", { class: "chip-btn", onclick: (e) => { e.currentTarget.remove(); set("source_page", ""); } }, "page: ", h("b", {}, state.source_page), " ×") : null), grid.el);
}

// ------------------------------------------------------------------ Image audit
export function imagesView(id, query) {
  const state = { source_page: query.get("page") || "" };
  const grid = dataGrid({
    label: "Image audit", storageKey: "images",
    fetchPage: (p) => api.images(id, p),
    onRowClick: (r) => go(`#/audit/${id}/url?u=${encodeURIComponent(r.source_page_url)}`),
    columns: [
      { key: "normalized_image_url", label: "Image", width: "minmax(220px, 1.6fr)", sortKey: "url", render: (r) => h("span", { class: "url mono", title: r.normalized_image_url }, r.normalized_image_url) },
      { key: "source_page_url", label: "Source page", width: "minmax(180px, 1.2fr)", render: (r) => h("span", { class: "url mono", title: r.source_page_url }, r.source_page_url) },
      { key: "http_status", label: "Status", width: "104px", sortKey: "status", render: (r) => statusCell(r.http_status, r.status_bucket || r.validation_source) },
      { key: "content_type", label: "Type", width: "110px" },
      { key: "content_length", label: "Bytes", width: "90px", num: true, sortKey: "size", render: (r) => fmt.n(r.content_length) },
      { key: "alt_text", label: "Alt", width: "minmax(110px, 1fr)", render: (r) => r.alt_present === 0 ? h("b", { class: "bad" }, "missing") : r.alt_present === 1 && !r.alt_text ? h("span", { class: "dim" }, "empty") : h("span", { class: "truncate", title: r.alt_text || "" }, r.alt_text || "—") },
      { key: "dims", label: "W × H", width: "96px", render: (r) => r.width || r.height ? `${r.width || "?"} × ${r.height || "?"}` : "—" },
      { key: "loading", label: "Loading", width: "80px" },
      { key: "attribute_source", label: "Source attr", width: "120px" },
      { key: "internal_external", label: "Scope", width: "120px" },
      { key: "image_issue", label: "Image issue", width: "160px", render: (r) => r.image_issue ? h("b", { class: "bad" }, r.image_issue) : (r.error_type || "") },
    ],
  });
  const set = (k, v) => { state[k] = v; grid.setParams({ ...grid.params(), ...state }); };
  grid.setParams(state);
  return card("Image audit", h("div", { class: "filters" },
    sel("Status", [["", "Any status"], ["2xx", "2xx"], ["3xx", "Redirected (3xx)"], ["4xx", "Broken (4xx)"], ["5xx", "5xx"], ["Timeout", "Timeout"]], "", (v) => set("status_bucket", v)),
    sel("Alt text", [["", "Any alt"], ["missing", "Missing alt"], ["empty", "Empty alt"], ["present", "Has alt"]], "", (v) => set("alt", v)),
    sel("Scope", [["", "Internal + external"], ["INTERNAL_IMAGE", "Internal"], ["EXTERNAL_IMAGE", "External"]], "", (v) => set("internal_external", v)),
    h("label", { class: "row dim" }, "Sort by size ↓", h("input", { type: "checkbox", onchange: (e) => grid.setParams({ ...grid.params(), sort: e.target.checked ? "size" : "", order: e.target.checked ? "desc" : "asc" }) })),
    search("Search image URL…", (v) => set("q", v))), grid.el);
}

// ------------------------------------------------------------------ Issue explorer + detail
export function issuesView(id, query) {
  const state = { severity: query.get("severity") || "", issue_type: query.get("type") || "" };
  const grid = dataGrid({
    label: "Issues", storageKey: "issues", rowHeight: 52,
    fetchPage: (p) => api.issues(id, { ...p, sort: p.sort || "severity" }),
    onRowClick: (r) => openIssue(id, r.issue_key, () => grid.refresh()),
    columns: [
      { key: "severity", label: "Severity", width: "92px", sortKey: "severity", render: (r) => sevPill(r.severity) },
      { key: "issue_type", label: "Rule", width: "minmax(160px, 1fr)", sortKey: "rule", render: (r) => h("div", {}, h("b", {}, r.issue_type), h("div", { class: "dim" }, r.category)) },
      { key: "source_page_url", label: "Page", width: "minmax(200px, 1.4fr)", sortKey: "page", render: (r) => h("span", { class: "url mono", title: r.source_page_url }, r.source_page_url) },
      { key: "normalized_url", label: "Subject", width: "minmax(180px, 1.2fr)", render: (r) => r.normalized_url && r.normalized_url !== r.source_page_url ? h("span", { class: "url mono", title: r.normalized_url }, r.normalized_url) : "—" },
      { key: "detail", label: "Evidence", width: "minmax(200px, 1.5fr)", render: (r) => h("span", { class: "truncate", title: r.detail || "" }, r.detail) },
      { key: "status", label: "Status", width: "118px", sortKey: "status", render: (r) => h("span", { class: `pill st-${r.status}` }, r.status) },
    ],
  });
  const set = (k, v) => { state[k] = v; grid.setParams({ ...grid.params(), ...state }); };
  grid.setParams(state);
  return card("Issue explorer", h("div", { class: "filters" },
    sel("Severity", [["", "All severities"], ...SEV.map((s) => [s, s])], state.severity, (v) => set("severity", v)),
    sel("Category", [["", "All categories"], ...["SOURCE_PAGE", "LINK", "IMAGE", "RESOURCE", "CANONICAL", "HREFLANG", "ON_PAGE_SEO", "TECH_SEO", "CONTENT", "INTERNATIONAL", "URL_STRUCTURE", "SITEMAP", "METADATA_URL", "IFRAME", "FORM"].map((c) => [c, c])], "", (v) => set("category", v)),
    sel("Status", [["", "Any status"], ...ISSUE_STATUSES.map((s) => [s, s])], "", (v) => set("status", v)),
    state.issue_type ? h("button", { class: "chip-btn", onclick: (e) => { e.currentTarget.remove(); set("issue_type", ""); } }, "rule: ", h("b", {}, state.issue_type), " ×") : null,
    search("Search page, subject or URL…", (v) => set("q", v))), grid.el);
}

export async function openIssue(id, key, onChange) {
  const it = await api.issueDetail(id, key);
  const back = h("div", { class: "modal-backdrop", role: "dialog", "aria-modal": "true", "aria-labelledby": "issue-title" });
  const close = () => { back.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  const statusSel = sel("Issue status", ISSUE_STATUSES.map((s) => [s, s]), it.status, () => {});
  const note = h("input", { type: "text", placeholder: "Note (ticket, owner…)", "aria-label": "Status note", value: it.status_note || "" });
  const ev = it.evidence;
  const modal = h("div", { class: "modal" },
    h("header", {}, sevPill(it.severity), h("h2", { id: "issue-title", style: { margin: 0, fontSize: "var(--fs-lg)" } }, it.rule_code), h("span", { class: "spacer" }),
      h("button", { class: "btn btn-ghost", onclick: close, "aria-label": "Close" }, "×")),
    h("div", { class: "body" },
      kv([["Category", it.category], ["Score dimension", it.dimension], ["Page", ext(it.page_url)], ["Subject", ext(ev.subject_url)],
        ["Element", ev.element_type], ["Anchor / alt", ev.anchor_or_alt_text], ["Location", ev.location],
        ["HTTP", ev.http_status], ["Initial status", ev.initial_status], ["Final URL", ext(ev.final_url)], ["Hops", ev.hop_count],
        ["Validation", ev.validation_source]]),
      h("div", { class: "evidence" }, h("b", {}, "Evidence: "), ev.detail),
      h("div", { class: "fix" }, h("b", {}, "Recommended fix: "), it.recommended_action),
      h("div", { class: "row" }, h("label", { class: "dim" }, "Status"), statusSel, note),
      it.status_note ? h("div", { class: "dim" }, `Last change: ${it.status_note} (${it.status_updated_at || ""})`) : null),
    h("footer", {}, h("button", { class: "btn btn-ghost", onclick: close }, "Close"),
      h("button", { class: "btn btn-primary", onclick: async () => {
        try { await api.setIssueStatus(id, key, statusSel.value, note.value); toast(`Issue marked ${statusSel.value}`); close(); onChange?.(); }
        catch (e) { toast(e.message, "err"); }
      } }, "Save status")));
  back.append(modal);
  document.body.append(back);
  ModalTransition(modal, back);
  statusSel.focus();
}

// ------------------------------------------------------------------ Remediation
export function remediationView(id) {
  const st = { page: 1, page_size: 25, severity: "", status: "" };
  const host = h("div", {});
  const pager = h("div", { class: "row" });
  async function load() {
    mount(host, h("div", { class: "skeleton", style: { height: "200px" } }));
    const r = await api.remediations(id, Object.fromEntries(Object.entries(st).filter(([, v]) => v !== "")));
    mount(host, r.records.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Priority"), h("th", {}, "Problem"), h("th", {}, "Why it matters"), h("th", {}, "Fix"), h("th", {}, "Affected"), h("th", {}, "Status"))),
      h("tbody", {}, r.records.map((x) => h("tr", {},
        h("td", {}, sevPill(x.severity), h("div", { class: "dim" }, `${x.priority} · ${x.priority_score}`)),
        h("td", {}, h("b", {}, x.title), h("div", { class: "dim" }, x.description)),
        h("td", { class: "dim" }, x.impact),
        h("td", {}, x.recommended_action),
        h("td", { class: "num" }, fmt.n(x.source_count), h("div", { class: "dim" }, x.technical_details?.scope || "")),
        h("td", {}, sel("Remediation status", ["OPEN", "IN_PROGRESS", "VERIFIED", "RESOLVED", "DISMISSED", "NEEDS_REVIEW"].map((s) => [s, s]), x.status,
          async (v) => { try { await api.setRemediationStatus(id, x.remediation_id, v); toast(`Marked ${v}`); } catch (e) { toast(e.message, "err"); } }))))))) :
      h("div", { class: "empty" }, h("h3", {}, "No remediation items yet"), h("p", {}, "Generate them from this audit's issues.")));
    mount(pager, h("span", { class: "dim" }, `Page ${r.page} of ${r.total_pages} · ${fmt.n(r.total_records)} item(s)`), h("span", { class: "spacer" }),
      h("button", { class: "btn", disabled: r.page <= 1 || null, onclick: () => { st.page--; load(); } }, "Previous"),
      h("button", { class: "btn", disabled: r.page >= r.total_pages || null, onclick: () => { st.page++; load(); } }, "Next"));
  }
  load().catch((e) => mount(host, h("p", { class: "muted" }, e.message)));
  return card("Remediation plan", h("div", { class: "filters" },
    h("button", { class: "btn btn-primary", onclick: async (e) => {
      const btn = e.currentTarget;            // currentTarget is null once the event finished dispatching
      btn.disabled = true;
      try { const g = await api.generateRemediations(id); toast(`${fmt.n(g.generated_count)} remediation item(s) generated`); st.page = 1; await load(); }
      catch (err) { toast(err.message, "err"); }
      btn.disabled = false;
    } }, icon("refresh"), "Generate from issues"),
    sel("Severity", [["", "All severities"], ["CRITICAL", "CRITICAL"], ...SEV.slice(0, 3).map((s) => [s, s])], "", (v) => { st.severity = v; st.page = 1; load(); }),
    sel("Status", [["", "Any status"], ...["OPEN", "IN_PROGRESS", "VERIFIED", "RESOLVED", "DISMISSED", "NEEDS_REVIEW"].map((s) => [s, s])], "", (v) => { st.status = v; st.page = 1; load(); })),
  host, pager);
}

// ------------------------------------------------------------------ Reports (manifest + downloads)
export function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const [head, ...body] = rows;
  return body.filter((r) => r.length > 1).map((r) => Object.fromEntries(head.map((k, i) => [k, r[i]])));
}

export function reportsView(id, d) {
  const host = h("div", {});
  if (!d.package_available) {
    mount(host, h("p", { class: "muted" }, "The report package is built automatically when the crawl completes."));
  } else {
    Promise.all([api.fileText(id, "00_manifest.csv"), api.fileText(id, "11_reconciliation.csv")]).then(([m, rec]) => {
      const man = parseCsv(m), checks = parseCsv(rec);
      const failed = checks.filter((c) => c.status !== "PASS");
      mount(host,
        h("div", { class: "row report-head" },
          h("span", { class: `pill ${failed.length ? "st-FAILED" : "st-PASS"}` }, failed.length ? `${failed.length} check(s) FAILED` : `Trust gate PASS · ${checks.length} checks`),
          h("span", { class: `pill ${man.every((r) => r.status === "OK") ? "st-PASS" : "st-FAILED"}` }, `Manifest ${man.filter((r) => r.status === "OK").length}/${man.length} OK · SHA-256 verified`),
          h("span", { class: "spacer" }), h("a", { class: "btn btn-primary", href: api.packageUrl(id) }, icon("download"), "Download ZIP")),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", {}, "Dataset"), h("th", {}, "Rows"), h("th", {}, "Size"), h("th", {}, "SHA-256"), h("th", {}, "Status"), h("th", {}, ""))),
          h("tbody", {}, man.map((r) => h("tr", {},
            h("td", { class: "mono nowrap" }, r.filename), h("td", { class: "dim" }, r.dataset), h("td", { class: "num" }, fmt.n(r.row_count)),
            h("td", { class: "num nowrap" }, Number(r.file_size) >= 1048576 ? `${fmt.dec(Number(r.file_size) / 1048576, 1)} MB` : `${fmt.dec(Number(r.file_size) / 1024, 1)} KB`),
            h("td", { class: "mono dim nowrap", title: r.sha256 }, r.sha256.slice(0, 12) + "…"),
            h("td", {}, h("span", { class: `pill ${r.status === "OK" ? "st-PASS" : "st-FAILED"}` }, r.status)),
            h("td", { class: "nowrap" }, h("a", { href: api.fileUrl(id, r.filename), download: "" }, "CSV"))))))),
        failed.length ? h("div", { class: "card" }, h("h3", {}, "Failed checks"), failed.map((c) => h("div", { class: "mono" }, `${c.check}: expected ${c.expected}, actual ${c.actual}`))) : null);
    }).catch((e) => mount(host, h("p", { class: "muted" }, e.message)));
  }
  return card("Reports & export", host);
}
