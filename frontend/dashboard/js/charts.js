// QC V5 Dashboard — dependency-free animated SVG charts (gauge, donut, bar list, area sparkline).
import { h, fmt, motionOff as reducedMotion } from "./ui.js";

const SEV_COLORS = { HIGH: "var(--sev-high)", MEDIUM: "var(--sev-medium)", LOW: "var(--sev-low)", INFO: "var(--sev-info)" };

/**
 * Radial gauge: value 0..max on a 270° arc, with a marker band (e.g. the 15–20 min target window).
 */
export function gauge({ value, max, label, display, band, color = "var(--brand-500)", size = 220 }) {
  const r = size / 2 - 16, c = size / 2, arc = 0.75, len = 2 * Math.PI * r;
  const frac = Math.max(0, Math.min(1, (value || 0) / (max || 1)));
  const track = h("circle", { cx: c, cy: c, r, fill: "none", stroke: "var(--stroke)", "stroke-width": 14, "stroke-linecap": "round",
    "stroke-dasharray": `${len * arc} ${len}`, transform: `rotate(135 ${c} ${c})` });
  const bar = h("circle", { cx: c, cy: c, r, fill: "none", stroke: color, "stroke-width": 14, "stroke-linecap": "round",
    transform: `rotate(135 ${c} ${c})` });
  const children = [track];
  if (band) {
    const a = Math.max(0, Math.min(1, band[0] / max)), b = Math.max(0, Math.min(1, band[1] / max));
    children.push(h("circle", { cx: c, cy: c, r: r + 13, fill: "none", stroke: "var(--ok)", "stroke-width": 3, opacity: 0.8,
      "stroke-dasharray": `0 ${len * arc * a * ((r + 13) / r)} ${len * arc * (b - a) * ((r + 13) / r)} ${len * 2}`,
      transform: `rotate(135 ${c} ${c})` }));
  }
  children.push(bar);
  const svg = h("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: "img", "aria-label": `${label}: ${display}` }, ...children);
  const used = Math.max(0.001, len * arc * frac);
  bar.style.strokeDasharray = `${used} ${len}`;
  if (reducedMotion()) bar.style.strokeDashoffset = "0";
  else {
    bar.style.strokeDashoffset = `${used}`;          // start hidden, sweep in
    bar.style.transition = "stroke-dashoffset 900ms cubic-bezier(.2,.8,.2,1)";
    requestAnimationFrame(() => requestAnimationFrame(() => { bar.style.strokeDashoffset = "0"; }));
  }
  return h("div", { class: "gauge-wrap" }, svg, h("div", { class: "gauge-center" }, h("b", {}, display), h("span", {}, label)));
}

/** Donut by severity. data: {HIGH: n, MEDIUM: n, ...} */
export function severityDonut(data, size = 190) {
  const keys = ["HIGH", "MEDIUM", "LOW", "INFO"];
  const total = keys.reduce((s, k) => s + (data[k] || 0), 0);
  const r = size / 2 - 18, c = size / 2, len = 2 * Math.PI * r;
  const segs = [h("circle", { cx: c, cy: c, r, fill: "none", stroke: "var(--stroke)", "stroke-width": 18 })];
  let acc = 0;
  for (const k of keys) {
    const v = data[k] || 0;
    if (!v) continue;
    const seg = (len * v) / total;
    const circle = h("circle", { cx: c, cy: c, r, fill: "none", stroke: SEV_COLORS[k], "stroke-width": 18,
      transform: `rotate(-90 ${c} ${c})`, "stroke-dasharray": `${Math.max(0, seg - 2)} ${len}`, "stroke-dashoffset": `${-acc}` });
    if (!reducedMotion()) {
      circle.style.opacity = "0";
      circle.style.transition = "opacity 600ms ease";
      requestAnimationFrame(() => requestAnimationFrame(() => { circle.style.opacity = "1"; }));
    }
    segs.push(circle);
    acc += seg;
  }
  const svg = h("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: "img",
    "aria-label": `Issues by severity: ${keys.map((k) => `${k} ${data[k] || 0}`).join(", ")}` }, ...segs);
  const center = h("div", { class: "gauge-center" }, h("b", {}, fmt.n(total)), h("span", {}, "issues"));
  const legend = h("div", { class: "donut-legend" }, keys.map((k) =>
    h("div", { class: "row" }, h("span", { class: `sev sev-${k}` }, k), h("span", { class: "spacer" }), h("b", {}, fmt.n(data[k] || 0)))));
  return h("div", {}, h("div", { class: "gauge-wrap" }, svg, center), legend);
}

/** Horizontal bar list. rows: [{label, value, color, title}] */
export function barList(rows, { max } = {}) {
  const m = max || Math.max(1, ...rows.map((r) => r.value || 0));
  const out = h("div", { class: "bars" });
  for (const r of rows) {
    const fill = h("i", { style: { background: r.color || "linear-gradient(90deg,var(--brand-500),var(--accent-500))" } });
    out.append(h("div", { class: "bar-row", title: r.title || "" },
      h("span", { class: "truncate" }, r.label), h("div", { class: "track" }, fill), h("span", { class: "num" }, fmt.n(r.value))));
    const w = `${(100 * (r.value || 0)) / m}%`;
    if (reducedMotion()) fill.style.width = w;
    else requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = w; }));
  }
  return out;
}

/** Area sparkline for live throughput. series: [{values:[...], color, label}] */
export function areaChart(series, { height = 120, width = 560, yLabel = "" } = {}) {
  const all = series.flatMap((s) => s.values);
  const maxV = Math.max(1, ...all);
  const n = Math.max(2, ...series.map((s) => s.values.length));
  const x = (i) => (i / (n - 1)) * width;
  const y = (v) => height - 6 - (v / maxV) * (height - 18);
  const gid = `g${Math.random().toString(36).slice(2, 8)}`;
  const kids = [h("defs", {}, series.map((s, i) => h("linearGradient", { id: `${gid}${i}`, x1: 0, x2: 0, y1: 0, y2: 1 },
    h("stop", { offset: "0%", "stop-color": s.color, "stop-opacity": "0.35" }), h("stop", { offset: "100%", "stop-color": s.color, "stop-opacity": "0" }))))];
  for (const f of [0.25, 0.5, 0.75]) kids.push(h("line", { x1: 0, x2: width, y1: height * f, y2: height * f, stroke: "var(--stroke)", "stroke-dasharray": "3 5" }));
  series.forEach((s, i) => {
    if (s.values.length < 2) return;
    const pts = s.values.map((v, j) => `${x(j + (n - s.values.length))},${y(v)}`);
    kids.push(h("polygon", { points: `${x(n - s.values.length)},${height} ${pts.join(" ")} ${x(n - 1)},${height}`, fill: `url(#${gid}${i})` }));
    kids.push(h("polyline", { points: pts.join(" "), fill: "none", stroke: s.color, "stroke-width": 2.2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  });
  kids.push(h("text", { x: width - 4, y: height - 4, "text-anchor": "end", fill: "var(--text-3)", "font-size": 10 }, `max ${fmt.dec(maxV)} ${yLabel}`));
  return h("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", height, role: "img",
    "aria-label": series.map((s) => `${s.label}: latest ${fmt.dec(s.values.at(-1) || 0)}`).join("; ") }, ...kids);
}
