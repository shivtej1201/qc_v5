// QC V5 Dashboard — GSAP animation primitives (vendor/gsap.min.js, loaded locally).
// Rules: every animation belongs to the CURRENT view's gsap.context and is reverted when the view changes
// (no leaked timelines/tweens); nothing animates under prefers-reduced-motion or during a polling refresh;
// tables and per-row content are never animated (only containers, KPIs, bars, status changes).
import { motionOff, fmt } from "./ui.js";

const G = () => window.gsap || null;
let ctx = null;

/** Start a new animation scope for a view root; reverts (kills) everything from the previous view. */
export function beginView(root) {
  ctx?.revert();
  ctx = G() ? G().context(() => {}, root) : null;
}
export function endView() { ctx?.revert(); ctx = null; }

function run(fn) {
  const g = G();
  if (!g || motionOff()) return null;
  return ctx ? ctx.add(fn) : fn();
}

// fromTo with EXPLICIT end values everywhere: a from() reads the end state from computed styles, which is wrong
// while a CSS animation (or a hidden ancestor) is in progress, and GSAP then reverts the tween at init.
export const PageTransition = (el) => run(() => G().fromTo(el, { autoAlpha: 0, y: 10 },
  { autoAlpha: 1, y: 0, duration: 0.32, ease: "power2.out", clearProps: "all" }));

export const CardReveal = (el) => run(() => G().fromTo(el, { autoAlpha: 0, y: 14, scale: 0.985 },
  { autoAlpha: 1, y: 0, scale: 1, duration: 0.42, ease: "power3.out", clearProps: "all" }));

export const StaggerReveal = (els, { stagger = 0.05 } = {}) => run(() => G().fromTo(els, { autoAlpha: 0, y: 12 },
  { autoAlpha: 1, y: 0, duration: 0.38, ease: "power2.out", stagger, clearProps: "all" }));

/** Count a number up from its previous value (el.dataset.v) to `to`; falls back to plain text. */
export function CounterAnimation(el, to, format = fmt.n) {
  const target = Number(to);
  if (!isFinite(target)) { el.textContent = format(to); return; }
  const from = Number(el.dataset.v || 0);
  el.dataset.v = String(target);
  if (!G() || motionOff() || from === target) { el.textContent = format(target); return; }
  const o = { v: from };
  run(() => G().to(o, { v: target, duration: 0.9, ease: "power3.out", onUpdate: () => { el.textContent = format(o.v); } }));
}

/** Animate a bar's width to a fraction 0..1 (the final width is always applied, even without GSAP). */
export function ProgressAnimation(bar, frac) {
  const w = `${(Math.max(0, Math.min(1, frac || 0)) * 100).toFixed(2)}%`;
  if (!G() || motionOff()) { bar.style.width = w; return; }
  run(() => G().fromTo(bar, { width: bar.style.width || "0%" }, { width: w, duration: 0.7, ease: "power2.out" }));
}

/** Gentle repeating pulse for a small number of critical indicators (killed with the view). */
export const IssuePulse = (el) => run(() => G().to(el, { scale: 1.06, duration: 0.8, repeat: -1, yoyo: true, ease: "sine.inOut" }));

/** Flash an element when its status changes (e.g. RUNNING -> COMPLETED). */
export function StatusTransition(el, key) {
  if (!key) return;
  if (el.dataset.prev && el.dataset.prev !== key) {
    run(() => G().fromTo(el, { boxShadow: "0 0 0 0 rgba(34,211,238,0.9)" },
      { boxShadow: "0 0 0 10px rgba(34,211,238,0)", duration: 0.9, ease: "power2.out", clearProps: "boxShadow" }));
  }
  el.dataset.prev = key;
}

export function ModalTransition(modal, backdrop) {
  // modals live outside the view root: animate them without the view context (they close themselves)
  const g = G();
  if (!g || motionOff()) return;
  g.fromTo(backdrop, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.2, clearProps: "opacity,visibility" });
  g.fromTo(modal, { autoAlpha: 0, y: 18, scale: 0.98 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.3, ease: "power3.out", clearProps: "all" });
}

/** Charts: grow bars / fade SVG once per view (never on polling refreshes). */
export const ChartReveal = (el) => run(() => G().fromTo(el, { autoAlpha: 0, scale: 0.96, transformOrigin: "50% 50%" },
  { autoAlpha: 1, scale: 1, duration: 0.5, ease: "power2.out", clearProps: "all" }));

export const gsapAvailable = () => !!G();
