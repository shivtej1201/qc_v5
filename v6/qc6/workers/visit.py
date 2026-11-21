"""Real-visitor review (blueprint §6): pages opened in real Chromium by visitors who behave like people.

Each visitor: one browser context (desktop or mobile profile), up to 10 pages one after another, then a fresh
context (a new person). Per page: navigate -> wait for the page to settle -> handle the cookie banner -> read
(think time) -> scroll down in steps so lazy content loads -> one keyboard interaction -> measure (web-vitals, axe,
rendered DOM vs server HTML, console errors) -> screenshot -> read again.

Honesty and manners: the real Chromium user agent plus a `QC-Audit/6.0` suffix and the site's `X-QC-Audit` token;
every request to the site's own hosts takes a token from the shared host budget; robots.txt is respected by the
visit plan; a bot challenge stops visits on that host for the run (never retried, never solved).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from qc6.crawl import challenge
from qc6.crawl.budget import HostBudget, Paused
from qc6.crawl.pacing import Pacer, Persona
from qc6.settings import settings

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
AXE_JS = open(os.path.join(ASSETS, "axe.min.js"), encoding="utf-8").read()
VITALS_JS = open(os.path.join(ASSETS, "web-vitals.iife.js"), encoding="utf-8").read()
VITALS_HOOK = VITALS_JS + """
;(function(){ window.__qcVitals = {}; var W = (typeof webVitals !== 'undefined') ? webVitals : window.webVitals; if (!W) return;   /* init scripts run in a function scope */
  var put = function(m){ window.__qcVitals[m.name] = Math.round(m.value * (m.name === 'CLS' ? 1000 : 1)) / (m.name === 'CLS' ? 1000 : 1); };
  ['onLCP','onCLS','onINP','onFCP','onTTFB'].forEach(function(f){ try { W[f](put, {reportAllChanges: true}); } catch(e){} });
})();"""

CONSENT_ACCEPT = re.compile(r"^\s*(accept( all)?( cookies)?|allow( all)?|i agree|agree|got it|ok(ay)?)\s*$", re.I)
CONSENT_REJECT = re.compile(r"^\s*(reject( all)?|decline|only (necessary|essential)( cookies)?|deny)\s*$", re.I)

DEVICES = {
    "desktop": {"viewport": {"width": 1366, "height": 768}, "device_scale_factor": 1, "is_mobile": False, "has_touch": False},
    "mobile": {"viewport": {"width": 412, "height": 915}, "device_scale_factor": 2.625, "is_mobile": True, "has_touch": True},
}

RENDERED_FACTS_JS = """() => {
  const q = (s) => Array.from(document.querySelectorAll(s));
  const canon = document.querySelector('link[rel="canonical"]');
  const main = document.querySelector('main') || document.body;
  return { title: document.title || '', h1: q('h1').map(e => e.innerText.trim()).filter(Boolean).slice(0, 5),
           links: q('a[href]').length, canonical: canon ? canon.href : '', text_len: (main ? main.innerText : '').length,
           height: Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement.scrollHeight),
           images_loaded: q('img').filter(i => i.complete && i.naturalWidth > 0).length, images: q('img').length };
}"""


@dataclass
class VisitTarget:
    url: str
    device: str = "desktop"
    template_id: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)     # coverage facts for this page (h1_count, internal_links …)


@dataclass
class VisitResult:
    url: str
    device: str
    template_id: Optional[int]
    status: str = "ok"                                    # ok | failed | challenged | skipped
    http_status: Optional[int] = None
    vitals: Dict[str, float] = field(default_factory=dict)
    a11y: List[Dict[str, Any]] = field(default_factory=list)
    render_diff: Dict[str, Any] = field(default_factory=dict)
    console: List[str] = field(default_factory=list)
    consent: Dict[str, Any] = field(default_factory=dict)
    screenshot: Optional[str] = None
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    duration_ms: int = 0


def is_primary(host: str, primary: str) -> bool:
    host, primary = (host or "").lower(), primary.lower()
    base = primary[4:] if primary.startswith("www.") else primary
    return host == primary or host == base or host.endswith("." + base)


def make_route_guard(site: Dict[str, Any], budget: Optional[HostBudget]):
    """Playwright route handler shared by visits and journeys: only http(s)/data/blob; no private, loopback or
    metadata addresses (same SSRF guard as the crawler, checked for every request, including redirects the page
    triggers); every request to the site's own hosts takes a token from the shared per-host budget."""
    from qcv5.crawler.url_normalizer import URLNormalizer

    async def guard(route):
        req = route.request
        u = urllib.parse.urlsplit(req.url)
        if u.scheme not in ("http", "https", "data", "blob"):
            return await route.abort()
        if u.scheme in ("http", "https"):
            if not settings.allow_private_targets and not URLNormalizer.is_safe_url(req.url):
                return await route.abort("blockedbyclient")
            if budget is not None and is_primary(u.hostname or "", site["primary_host"]):
                try:
                    await budget.take()
                except Paused:
                    return await route.abort("blockedbyclient")
        await route.continue_()
    return guard


class Visitor:
    """One simulated person. Not thread-safe; one per asyncio task."""

    def __init__(self, browser, site: Dict[str, Any], budget: Optional[HostBudget], device: str, seed: int,
                 objects_dir: str, persona: Persona = Persona(), consent: str = "accept", ua: str = "") -> None:
        self.browser, self.site, self.budget, self.device = browser, site, budget, device
        self.pacer = Pacer(persona, seed)
        self.objects_dir, self.consent_choice, self.ua = objects_dir, consent, ua
        self.ctx = None
        self.pages_in_ctx = 0
        self.stopped_reason: Optional[str] = None
        self.on_step = None          # async (url, device, step) -> None, for the live view

    async def _new_context(self):
        if self.ctx is not None:
            await self.ctx.close()
        prof = DEVICES[self.device]
        self.ctx = await self.browser.new_context(
            **prof, user_agent=self.ua or None, locale=self.site.get("settings", {}).get("locale", "en-IN"),
            timezone_id=self.site.get("settings", {}).get("timezone", "Asia/Kolkata"),
            extra_http_headers={settings.access_header: self.site["access_token"]}, accept_downloads=False,
            java_script_enabled=True, service_workers="block")
        await self.ctx.add_init_script(VITALS_HOOK)
        self._guard = make_route_guard(self.site, self.budget)
        await self.ctx.route("**/*", self._route)
        self.pages_in_ctx = 0

    async def _route(self, route):
        await self._guard(route)

    async def close(self):
        if self.ctx is not None:
            await self.ctx.close()
            self.ctx = None

    def _mark(self, res: VisitResult, step: str, t0: float, **kw):
        res.timeline.append({"step": step, "at_ms": int((time.perf_counter() - t0) * 1000), **kw})
        if self.on_step is not None:
            asyncio.get_running_loop().create_task(self.on_step(res.url, self.device, step))

    async def visit(self, target: VisitTarget) -> VisitResult:
        res = VisitResult(target.url, self.device, target.template_id)
        t0 = time.perf_counter()
        if self.ctx is None or self.pages_in_ctx >= 10:
            await self._new_context()
        self.pages_in_ctx += 1
        page = await self.ctx.new_page()
        console: List[str] = []
        page.on("console", lambda m: console.append(m.text[:300]) if m.type == "error" and len(console) < 20 else None)
        page.on("pageerror", lambda e: console.append(str(e)[:300]) if len(console) < 20 else None)
        try:
            self._mark(res, "open", t0)
            try:
                resp = await page.goto(target.url, wait_until="load", timeout=20_000)
            except Exception as e:
                res.status, res.error = "failed", f"Page did not load: {str(e).splitlines()[0][:200]}"
                return res
            res.http_status = resp.status if resp else None
            head = (await page.content())[:20_000]
            vendor = challenge.detect(res.http_status, dict(resp.headers) if resp else {}, head)
            if vendor:
                res.status, res.error = "challenged", vendor
                self.stopped_reason = vendor
                if self.budget is not None:
                    await self.budget.pause(6 * 3600, f"challenged by {vendor} during a real-visitor check")
                return res
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
            self._mark(res, "settled", t0)

            res.consent = await self._consent(page)
            if res.consent.get("clicked"):
                self._mark(res, "consent", t0, choice=res.consent["clicked"])

            pause = self.pacer.think()
            await asyncio.sleep(pause)
            self._mark(res, "read", t0, seconds=round(pause, 2))

            facts = await page.evaluate(RENDERED_FACTS_JS)
            plan = self.pacer.scroll_plan(int(facts.get("height") or 0), DEVICES[self.device]["viewport"]["height"])
            for y, wait in plan[:40]:
                await page.evaluate("y => window.scrollTo({top: y, behavior: 'instant'})", y)
                await asyncio.sleep(wait)
            if plan:
                self._mark(res, "scrolled", t0, steps=len(plan[:40]))
                await asyncio.sleep(self.pacer.think() / 3)
                await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")

            await page.keyboard.press("Tab")              # one real interaction so INP can be measured
            await asyncio.sleep(0.4)
            await page.evaluate("() => { document.dispatchEvent(new Event('visibilitychange')); }")
            res.vitals = await page.evaluate("() => window.__qcVitals || {}")
            self._mark(res, "measured", t0)

            res.a11y = await self._axe(page)
            facts = await page.evaluate(RENDERED_FACTS_JS)
            res.render_diff = self._diff(target.raw, facts)
            res.console = console[:20]

            os.makedirs(self.objects_dir, exist_ok=True)
            name = f"visit-{abs(hash((target.url, self.device, time.time()))) % 10**12}.jpg"
            path = os.path.join(self.objects_dir, name)
            vp = DEVICES[self.device]["viewport"]
            await page.screenshot(path=path, type="jpeg", quality=62, full_page=False,
                                  clip={"x": 0, "y": 0, "width": vp["width"], "height": min(vp["height"] * 2, max(vp["height"], int(facts.get("height") or vp["height"])))})
            res.screenshot = name
            pause = self.pacer.think()
            await asyncio.sleep(pause / 2)
            self._mark(res, "done", t0)
        except Exception as e:
            res.status, res.error = "failed", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        finally:
            res.duration_ms = int((time.perf_counter() - t0) * 1000)
            await page.close()
        return res

    async def _consent(self, page) -> Dict[str, Any]:
        before = await page.evaluate("() => (window.__qcVitals || {}).CLS || 0")
        pattern = CONSENT_REJECT if self.consent_choice == "reject" else CONSENT_ACCEPT
        try:
            btn = page.get_by_role("button", name=pattern).first
            try:                                           # banners often slide in a moment after load
                await btn.wait_for(state="visible", timeout=2500)
            except Exception:
                return {"found": False}
            if await btn.is_visible():
                box = await btn.bounding_box()
                if box:
                    for x, y in self.pacer.mouse_path(box["x"] - 120, box["y"] - 60, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2):
                        await page.mouse.move(x, y)
                await asyncio.sleep(self.pacer.rng.uniform(0.3, 0.9))
                await btn.click(timeout=3000)
                await asyncio.sleep(0.6)
                after = await page.evaluate("() => (window.__qcVitals || {}).CLS || 0")
                return {"found": True, "clicked": self.consent_choice, "cls_added": round(max(0.0, after - before), 3)}
        except Exception:
            pass
        return {"found": False}

    async def _axe(self, page) -> List[Dict[str, Any]]:
        try:
            await page.add_script_tag(content=AXE_JS)
            out = await page.evaluate("""async () => {
              const r = await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa'] },
                                                 resultTypes: ['violations'] });
              return r.violations.map(v => ({ id: v.id, impact: v.impact, help: v.help, count: v.nodes.length,
                                              target: (v.nodes[0] && v.nodes[0].target || []).join(' ').slice(0, 200) }));
            }""")
            return out[:40]
        except Exception as e:
            return [{"id": "axe-error", "impact": None, "help": str(e)[:200], "count": 0, "target": ""}]

    @staticmethod
    def _diff(raw: Dict[str, Any], rendered: Dict[str, Any]) -> Dict[str, Any]:
        raw_h1 = int(raw.get("h1_count") or 0)
        raw_links = int(raw.get("internal_links") or 0) + int(raw.get("external_links") or 0)
        return {"raw_h1": raw_h1, "rendered_h1": len(rendered.get("h1") or []), "h1_text": (rendered.get("h1") or [""])[0][:200],
                "raw_links": raw_links, "rendered_links": int(rendered.get("links") or 0),
                "links_only_after_js": max(0, int(rendered.get("links") or 0) - raw_links) if raw else 0,
                "h1_only_after_js": bool(raw) and raw_h1 == 0 and bool(rendered.get("h1")),
                "text_len": rendered.get("text_len"), "images": rendered.get("images"), "images_loaded": rendered.get("images_loaded")}


async def default_user_agent(browser) -> str:
    ctx = await browser.new_context()
    page = await ctx.new_page()
    ua = await page.evaluate("navigator.userAgent")
    await ctx.close()
    return f"{ua} {settings.ua_suffix}"


async def run_visits(browser, site: Dict[str, Any], targets: List[VisitTarget], budget: Optional[HostBudget],
                     visitors: int, on_result=None, seed: int = 7, consent: str = "accept",
                     persona: Optional[Persona] = None, on_step=None,
                     stop: Optional[asyncio.Event] = None) -> List[VisitResult]:
    """Run targets with `visitors` people in parallel; each person takes pages one at a time."""
    visitors = max(1, min(visitors, settings.visitors_max))
    q: asyncio.Queue = asyncio.Queue()
    for t in targets:
        q.put_nowait(t)
    ua = await default_user_agent(browser)
    results: List[VisitResult] = []
    stop = stop or asyncio.Event()                     # set by a challenge, or by Pause (visitors finish their page)

    async def person(i: int):
        per_device: Dict[str, Visitor] = {}
        try:
            while not q.empty() and not stop.is_set():
                t: VisitTarget = q.get_nowait()
                v = per_device.get(t.device)
                if v is None:
                    v = per_device[t.device] = Visitor(browser, site, budget, t.device, seed * 1000 + i,
                                                       settings.objects_dir, persona=persona or Persona(),
                                                       consent=consent, ua=ua)
                    if on_step is not None:
                        v.on_step = (lambda lane: (lambda url, device, step: on_step(url, device, step, lane)))(i)
                r = await v.visit(t)
                results.append(r)
                if on_result:
                    await on_result(r, i)
                if r.status == "challenged":
                    stop.set()
        finally:
            for v in per_device.values():
                await v.close()

    await asyncio.gather(*(person(i) for i in range(visitors)))
    challenged = any(r.status == "challenged" for r in results)
    while not q.empty():
        t = q.get_nowait()
        if challenged:
            results.append(VisitResult(t.url, t.device, t.template_id, status="skipped", error="host challenged; visits stopped"))
    return results
