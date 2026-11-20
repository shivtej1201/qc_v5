// GSAP motion (design spec §6): every tween lives in a per-view gsap.context and is reverted when the view unmounts;
// prefers-reduced-motion turns tweens into instant sets; live refreshes never replay entrance animations.
import { gsap } from "gsap";
import { useLayoutEffect, useRef, type RefObject } from "react";

export const reducedMotion = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Fade/rise the view, then stagger its [data-reveal] children (max 8). Runs once per mount. */
export function useViewMotion<T extends HTMLElement>(deps: unknown[] = []): RefObject<T> {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || reducedMotion()) return;
    const ctx = gsap.context(() => {
      gsap.fromTo(el, { autoAlpha: 0, y: 8 }, { autoAlpha: 1, y: 0, duration: 0.28, ease: "power2.out", clearProps: "all" });
      const items = Array.from(el.querySelectorAll("[data-reveal]")).slice(0, 8);
      if (items.length) gsap.fromTo(items, { autoAlpha: 0, y: 10 }, { autoAlpha: 1, y: 0, duration: 0.32, stagger: 0.04, ease: "power2.out", delay: 0.05, clearProps: "all" });
    }, el);
    return () => ctx.revert();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}

/** Count a number from its previous value to the new one. */
export function countTo(el: HTMLElement | null, to: number, format: (n: number) => string = (n) => String(Math.round(n))) {
  if (!el) return;
  const from = Number(el.dataset.v ?? to);
  el.dataset.v = String(to);
  if (reducedMotion() || from === to) { el.textContent = format(to); return; }
  const o = { v: from };
  gsap.to(o, { v: to, duration: 0.6, ease: "power1.out", onUpdate: () => { el.textContent = format(o.v); } });
}

export function flash(el: Element | null) {
  if (!el || reducedMotion()) return;
  gsap.fromTo(el, { backgroundColor: "var(--measure-soft)" }, { backgroundColor: "transparent", duration: 0.6, clearProps: "backgroundColor" });
}

export function slideIn(el: Element | null, backdrop: Element | null) {
  if (reducedMotion()) return;
  if (el) gsap.fromTo(el, { x: 24, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.3, ease: "power3.out" });
  if (backdrop) gsap.fromTo(backdrop, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.25 });
}

export function fadeText(el: Element | null) {
  if (!el || reducedMotion()) return;
  gsap.fromTo(el, { autoAlpha: 0.2 }, { autoAlpha: 1, duration: 0.2 });
}
