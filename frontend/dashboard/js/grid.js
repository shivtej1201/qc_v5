// QC V5 Dashboard — virtualized, server-paged data grid for very large audits (300K+ rows).
// Only the rows in view (+ overscan) exist in the DOM; pages of `pageSize` rows are fetched on demand from the
// API (server-side filter / sort / search), cached per page, and dropped when parameters change.
// Rows are plain DOM (never animated). Keyboard: Tab into the grid, Up/Down move, Enter opens.
import { h, mount, fmt } from "./ui.js";

export function dataGrid({ columns, fetchPage, rowHeight = 44, pageSize = 200, height = 560, onRowClick, label, storageKey }) {
  let params = {};
  let total = null;
  let token = 0;
  const pages = new Map();          // page index -> items | Promise
  let hidden = new Set();
  try { hidden = new Set(JSON.parse(localStorage.getItem(`grid-cols:${storageKey}`) || "[]")); } catch { /* blocked */ }
  let focusIdx = -1;

  const visibleCols = () => columns.filter((c) => !hidden.has(c.key));
  const template = () => visibleCols().map((c) => c.width || "minmax(120px, 1fr)").join(" ");

  const status = h("div", { class: "grid-status dim", role: "status", "aria-live": "polite" }, "Loading…");
  const head = h("div", { class: "vgrid-head", role: "row" });
  const spacer = h("div", { class: "vgrid-spacer" });
  const rowsHost = h("div", { class: "vgrid-rows" });
  const viewport = h("div", { class: "vgrid-viewport", tabindex: 0, role: "grid", "aria-label": label || "Data",
    style: { height: `${height}px` } }, spacer, rowsHost);
  const colMenu = h("details", { class: "col-menu" }, h("summary", { class: "btn" }, "Columns"),
    h("div", { class: "col-menu-list" }, columns.map((c) => h("label", { class: "row" },
      h("input", { type: "checkbox", checked: !hidden.has(c.key) || null, onchange: (e) => {
        if (e.target.checked) hidden.delete(c.key); else hidden.add(c.key);
        try { localStorage.setItem(`grid-cols:${storageKey}`, JSON.stringify([...hidden])); } catch { /* blocked */ }
        drawHead(); paint(true);
      } }), c.label))));

  function drawHead() {
    head.style.gridTemplateColumns = template();
    mount(head, visibleCols().map((c) => {
      const active = params.sort && c.sortKey === params.sort;
      const arrow = active ? (params.order === "desc" ? " ↓" : " ↑") : "";
      return c.sortKey
        ? h("button", { class: `vgrid-th sortable${active ? " active" : ""}`, role: "columnheader",
          "aria-sort": active ? (params.order === "desc" ? "descending" : "ascending") : "none",
          onclick: () => setParams({ ...params, sort: c.sortKey, order: active && params.order === "asc" ? "desc" : "asc" }) },
        c.label + arrow)
        : h("div", { class: "vgrid-th", role: "columnheader" }, c.label);
    }));
  }

  function loadPage(i) {
    if (pages.has(i)) return pages.get(i);
    const my = token;
    const p = fetchPage({ ...params, offset: i * pageSize, limit: pageSize }).then((res) => {
      if (my !== token) return null;
      total = res.matched;
      pages.set(i, res.items);
      spacer.style.height = `${total * rowHeight}px`;
      viewport.setAttribute("aria-rowcount", String(total));
      status.textContent = total ? `${fmt.n(total)} row(s)` : "No rows match these filters.";
      paint(true);              // the rows for this page now exist: always redraw
      return res.items;
    }).catch((e) => { pages.delete(i); status.textContent = `Error: ${e.message}`; });
    pages.set(i, p);
    return p;
  }

  let painted = "";
  function paint(force = false) {
    const top = viewport.scrollTop;
    const first = Math.max(0, Math.floor(top / rowHeight) - 8);
    const last = Math.min((total ?? pageSize) - 1, Math.ceil((top + height) / rowHeight) + 8);
    const key = `${first}:${last}:${total}:${focusIdx}`;
    if (!force && key === painted) return;
    painted = key;
    const nodes = [];
    for (let i = first; i <= last; i++) {
      const pi = Math.floor(i / pageSize);
      const page = pages.get(pi);
      if (!Array.isArray(page)) { loadPage(pi); continue; }
      const item = page[i - pi * pageSize];
      if (!item) continue;
      const row = h("div", { class: `vgrid-row${i === focusIdx ? " focus" : ""}${onRowClick ? " clickable" : ""}`, role: "row",
        "aria-rowindex": String(i + 1), style: { top: `${i * rowHeight}px`, height: `${rowHeight}px`, gridTemplateColumns: template() },
        onclick: onRowClick ? () => onRowClick(item) : null },
        visibleCols().map((c) => h("div", { class: `vgrid-td${c.num ? " num" : ""}`, role: "gridcell" },
          c.render ? c.render(item) : (item[c.key] ?? "—"))));
      nodes.push(row);
    }
    mount(rowsHost, nodes);
  }

  viewport.addEventListener("scroll", () => requestAnimationFrame(() => paint()));
  viewport.addEventListener("keydown", (e) => {
    if (!total) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      focusIdx = Math.max(0, Math.min(total - 1, focusIdx + (e.key === "ArrowDown" ? 1 : -1)));
      const y = focusIdx * rowHeight;
      if (y < viewport.scrollTop) viewport.scrollTop = y;
      else if (y + rowHeight > viewport.scrollTop + height) viewport.scrollTop = y + rowHeight - height;
      paint(true);
    } else if (e.key === "Enter" && onRowClick && focusIdx >= 0) {
      const page = pages.get(Math.floor(focusIdx / pageSize));
      if (Array.isArray(page)) onRowClick(page[focusIdx % pageSize]);
    }
  });

  function setParams(p) {
    params = Object.fromEntries(Object.entries(p).filter(([, v]) => v !== "" && v !== null && v !== undefined));
    token++;
    pages.clear();
    total = null;
    focusIdx = -1;
    painted = "";              // new data set: never reuse the previous paint key
    viewport.scrollTop = 0;
    status.textContent = "Loading…";
    drawHead();
    mount(rowsHost);
    loadPage(0);
  }

  drawHead();
  const el = h("div", { class: "vgrid" }, h("div", { class: "row vgrid-bar" }, status, h("span", { class: "spacer" }), colMenu),
    h("div", { class: "vgrid-scroll" }, head, viewport));
  return { el, setParams, params: () => params, refresh: () => setParams(params) };
}
