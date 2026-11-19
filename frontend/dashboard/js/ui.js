// QC V5 Dashboard — DOM, formatting, motion helpers.
// SECURITY: crawled pages control anchor/alt text and URLs. Everything goes through h()/text nodes;
// innerHTML is never used with data.

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
/** No entrance/chart animation: user preference OR a background (polling) refresh. */
export const motionOff = () => reducedMotion() || document.documentElement.classList.contains("refreshing");

/** h("div", {class: "x", onclick: fn}, "text", child) -> Element. Strings become text nodes. */
export function h(tag, attrs, ...children) {
  const isSvg = ["svg", "path", "circle", "g", "line", "polyline", "polygon", "rect", "defs", "linearGradient", "stop", "text"].includes(tag);
  const el = isSvg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}
export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
  return container;
}

const nf = new Intl.NumberFormat();
export const fmt = {
  n: (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? "—" : nf.format(Math.round(Number(v)))),
  dec: (v, d = 1) => (v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(d)),
  pct: (a, b) => (b ? `${((100 * a) / b).toFixed(1)}%` : "—"),
  dur: (sec) => {
    if (sec === null || sec === undefined || !isFinite(sec)) return "—";
    sec = Math.max(0, Math.round(sec));
    const h_ = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h_ ? `${h_}h ${m}m` : m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
  },
  ago: (iso) => {
    if (!iso) return "—";
    const d = (Date.now() - new Date(iso).getTime()) / 1000;
    if (d < 60) return "just now";
    if (d < 3600) return `${Math.floor(d / 60)} min ago`;
    if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
    return new Date(iso).toLocaleDateString();
  },
};

/** Animated number (count-up). Respects reduced motion. */
export function countUp(el, to, { duration = 900, format = fmt.n } = {}) {
  const target = Number(to);
  if (!isFinite(target)) { el.textContent = format(to); return; }
  const from = Number(el.dataset.v || 0);
  el.dataset.v = String(target);
  if (motionOff() || from === target) { el.textContent = format(target); return; }
  const t0 = performance.now();
  const step = (t) => {
    const p = Math.min(1, (t - t0) / duration);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = format(from + (target - from) * e);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export function toast(msg, kind = "") {
  let host = document.querySelector(".toasts");
  if (!host) { host = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(host); }
  const t = h("div", { class: `toast ${kind}` }, msg);
  host.append(t);
  setTimeout(() => t.remove(), 4500);
}

export const stateClass = (s) => `pill st-${String(s || "QUEUED").toUpperCase()}${["RUNNING", "STOPPING"].includes(s) ? " live" : ""}`;

// Inline icon set (stroke icons, 24px grid). Static markup only, never data.
const ICONS = {
  overview: "M3 12l9-8 9 8M5 10v10h14V10",
  audits: "M4 6h16M4 12h16M4 18h10",
  plus: "M12 5v14M5 12h14",
  stop: "M7 7h10v10H7z",
  play: "M8 5l11 7-11 7z",
  download: "M12 4v12m0 0l-5-5m5 5l5-5M5 20h14",
  sun: "M12 4V2m0 20v-2m8-8h2M2 12h2m13.66-5.66l1.41-1.41M4.93 19.07l1.41-1.41m0-11.32L4.93 4.93m14.14 14.14l-1.41-1.41M12 7a5 5 0 100 10 5 5 0 000-10z",
  back: "M15 18l-6-6 6-6",
  refresh: "M4 4v6h6M20 20v-6h-6M5.5 15a7 7 0 0011.9 2.5M18.5 9A7 7 0 006.6 6.5",
  classic: "M4 5h16v14H4zM4 9h16",
  sitemap: "M10 3h4v4h-4zM4 17h4v4H4zM10 17h4v4h-4zM16 17h4v4h-4zM12 7v5M6 17v-5h12v5",
  seo: "M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-4-4M8 11l2 2 4-4",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
};
export function icon(name) {
  return h("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    h("path", { d: ICONS[name] || "" }));
}
