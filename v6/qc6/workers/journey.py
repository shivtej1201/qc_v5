"""User journeys (blueprint §8): YAML paths walked at a person's pace, checked the way a person would notice.

Spec (all keys optional except name and steps):
    name: Check loan eligibility
    device: mobile                    # desktop | mobile
    persona: { think_time: { median_s: 2.5, max_s: 8 }, typing: { wpm: 38 } }
    steps:
      - open: /personal-loan
        expect: [ { visible: "role=heading[name=/Personal Loan/i]" }, { lcp_under_ms: 2500 } ]
      - click: "role=link[name='Check eligibility']"
      - fill: { "label=Monthly income": "85000" }
      - select: { "label=City": "Pune" }
      - click: "role=button[name='Continue']"
        expect: [ { visible: "text=/eligible/i" }, { no_console_errors: true } ]
      - stop_before: "role=button[name='Submit application']"

Safety: a journey can never press a button or link whose name looks irreversible (submit, pay, apply now, confirm,
place order, delete …) — the runner refuses the step. `stop_before` checks the element is there and ends the journey.
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from qc6.crawl.pacing import Pacer, Persona
from qc6.settings import settings
from qc6.workers.visit import DEVICES, VITALS_HOOK, make_route_guard

ACTIONS = {"open", "click", "fill", "select", "wait", "stop_before"}
IRREVERSIBLE = re.compile(r"\b(submit|pay( now)?|place order|buy now|confirm|apply now|delete|remove|transfer|"
                          r"send money|book now|checkout|subscribe)\b", re.I)


class SpecError(ValueError):
    pass


@dataclass
class StepOutcome:
    idx: int
    action: str
    label: str
    status: str = "passed"            # passed | failed | skipped
    detail: str = ""
    screenshot: Optional[str] = None
    duration_ms: int = 0


@dataclass
class JourneyOutcome:
    name: str
    device: str
    status: str = "passed"            # passed | failed | error
    failed_step: Optional[int] = None
    failure_text: Optional[str] = None
    steps: List[StepOutcome] = field(default_factory=list)
    duration_ms: int = 0


def parse(source_yaml: str) -> Dict[str, Any]:
    try:
        spec = yaml.safe_load(source_yaml)
    except yaml.YAMLError as e:
        raise SpecError(f"The journey file is not valid YAML: {e}") from None
    if not isinstance(spec, dict) or not spec.get("name") or not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise SpecError("A journey needs a name and at least one step.")
    if spec.get("device", "desktop") not in DEVICES:
        raise SpecError("device must be desktop or mobile.")
    if spec.get("schedule"):
        from qc6.scheduler import ScheduleError, parse as parse_schedule
        try:
            parse_schedule(str(spec["schedule"]))
        except ScheduleError as e:
            raise SpecError(str(e)) from None
    for i, st in enumerate(spec["steps"], 1):
        if not isinstance(st, dict):
            raise SpecError(f"Step {i} must be a mapping like '- click: ...'.")
        acts = [k for k in st if k in ACTIONS]
        if len(acts) != 1:
            raise SpecError(f"Step {i} must have exactly one action ({', '.join(sorted(ACTIONS))}).")
        target = st[acts[0]]
        if acts[0] == "click" and isinstance(target, str) and IRREVERSIBLE.search(_name_of(target)):
            raise SpecError(f"Step {i} would press '{_name_of(target)}', which looks irreversible. "
                            f"Use 'stop_before' for that element instead.")
    return spec


def _name_of(selector: str) -> str:
    m = re.search(r"name=['\"/]?([^'\"\]/]+)", selector)
    return m.group(1) if m else selector


def readable(sel: str) -> str:
    """What a person would call the thing a selector points at."""
    sel = str(sel)
    if "name=" in sel:
        return f'"{_name_of(sel)}"'
    if sel.startswith("text="):
        return '"' + sel[5:].strip("/").rstrip("/i").strip("/") + '"'
    if sel.startswith("label="):
        return f'the "{sel[6:]}" field'
    return sel


def label_for(action: str, target: Any) -> str:
    if action == "open":
        return f"Open {target}"
    if action in ("click", "stop_before"):
        n = _name_of(str(target))
        return f'Tap "{n}"' if action == "click" else f'Stop before "{n}"'
    if action == "fill":
        return "Type " + ", ".join(_name_of(k).replace("label=", "") for k in target)
    if action == "select":
        return "Choose " + ", ".join(f"{v}" for v in target.values())
    if action == "wait":
        return f"Wait {target} s"
    return action


def _locator(page, sel: str):
    if sel.startswith("label="):
        return page.get_by_label(sel[6:], exact=False).first
    return page.locator(sel).first


class JourneyRunner:
    def __init__(self, browser, site: Dict[str, Any], objects_dir: str, seed: int = 5, fast: bool = False, budget=None):
        self.browser, self.site, self.objects_dir, self.seed, self.fast = browser, site, objects_dir, seed, fast
        self.budget = budget

    def _persona(self, spec) -> Persona:
        p = spec.get("persona") or {}
        tt, ty = p.get("think_time") or {}, p.get("typing") or {}
        if self.fast:
            return Persona(think_median_s=0.15, think_min_s=0.05, think_max_s=0.3, typing_wpm=float(ty.get("wpm", 400)))
        return Persona(think_median_s=float(tt.get("median_s", 2.5)), think_max_s=float(tt.get("max_s", 8)),
                       typing_wpm=float(ty.get("wpm", 38)))

    async def run(self, spec: Dict[str, Any]) -> JourneyOutcome:
        device = spec.get("device", "desktop")
        out = JourneyOutcome(spec["name"], device)
        pacer = Pacer(self._persona(spec), self.seed)
        ua = None
        ctx = await self.browser.new_context(**DEVICES[device], extra_http_headers={settings.access_header: self.site["access_token"]},
                                             locale="en-IN", timezone_id="Asia/Kolkata", accept_downloads=False)
        await ctx.add_init_script(VITALS_HOOK)
        await ctx.route("**/*", make_route_guard(self.site, self.budget))
        page = await ctx.new_page()
        ua = await page.evaluate("navigator.userAgent")
        await ctx.set_extra_http_headers({settings.access_header: self.site["access_token"],
                                          "User-Agent": f"{ua} {settings.ua_suffix}"})
        errors: List[str] = []
        requests = {"n": 0}
        page.on("console", lambda m: errors.append(m.text[:300]) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
        page.on("request", lambda r: requests.__setitem__("n", requests["n"] + 1))
        t_start = time.perf_counter()
        try:
            for i, st in enumerate(spec["steps"], 1):
                action = next(k for k in st if k in ACTIONS)
                target = st[action]
                so = StepOutcome(i, action, label_for(action, target))
                t0 = time.perf_counter()
                err_before, req_before = len(errors), requests["n"]
                await asyncio.sleep(pacer.think())
                try:
                    await self._do(page, pacer, action, target)
                    if action == "click":
                        await self._settle(page)
                        if requests["n"] == req_before and not await self._changed(page):
                            pass  # judged by expectations below
                    problems = await self._expect(page, st.get("expect") or [], errors, err_before)
                    if problems:
                        raise AssertionError(problems)
                except AssertionError as e:
                    so.status = "failed"
                    so.detail = self._explain(so.label, str(e.args[0] if e.args else e), errors[err_before:],
                                              requests["n"] - req_before)
                except Exception as e:
                    so.status = "failed"
                    so.detail = self._explain(so.label, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                                              errors[err_before:], requests["n"] - req_before)
                so.screenshot = await self._shot(page, out, i)
                so.duration_ms = int((time.perf_counter() - t0) * 1000)
                out.steps.append(so)
                if so.status == "failed":
                    out.status, out.failed_step, out.failure_text = "failed", i, so.detail
                    for j, rest in enumerate(spec["steps"][i:], i + 1):
                        a = next(k for k in rest if k in ACTIONS)
                        out.steps.append(StepOutcome(j, a, label_for(a, rest[a]), "skipped", "Not reached"))
                    break
                if action == "stop_before":
                    break
        except Exception as e:
            out.status, out.failure_text = "error", f"The journey could not run: {e}"
        finally:
            out.duration_ms = int((time.perf_counter() - t_start) * 1000)
            await ctx.close()
        return out

    async def _do(self, page, pacer: Pacer, action: str, target: Any):
        base = self.site.get("base_url", "")
        if action == "open":
            url = urllib.parse.urljoin(base, str(target))
            await page.goto(url, wait_until="load", timeout=20_000)
            await self._settle(page)
        elif action in ("click", "stop_before"):
            loc = _locator(page, str(target))
            await loc.wait_for(state="visible", timeout=8_000)
            name = (await loc.inner_text(timeout=2000) or "").strip() or _name_of(str(target))
            if action == "stop_before":
                return
            if IRREVERSIBLE.search(name):
                raise AssertionError(f'Refused to press "{name}": it looks irreversible.')
            box = await loc.bounding_box()
            if box:
                for x, y in pacer.mouse_path(box["x"] - 80, box["y"] - 40, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2):
                    await page.mouse.move(x, y)
            await loc.click(timeout=5_000)
        elif action == "fill":
            for sel, value in target.items():
                loc = _locator(page, sel)
                await loc.wait_for(state="visible", timeout=8_000)
                await loc.click()
                await loc.fill("")
                for ch, delay in zip(str(value), pacer.key_delays(str(value))):
                    await page.keyboard.type(ch)
                    await asyncio.sleep(delay)
                await asyncio.sleep(pacer.think() / 3)
        elif action == "select":
            for sel, value in target.items():
                loc = _locator(page, sel)
                await loc.wait_for(state="visible", timeout=8_000)
                await loc.select_option(label=str(value))
        elif action == "wait":
            await asyncio.sleep(float(target))

    async def _settle(self, page):
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000 if not self.fast else 3_000)
        except Exception:
            pass

    async def _changed(self, page) -> bool:
        return True

    async def _expect(self, page, expects: List[Dict[str, Any]], errors: List[str], err_before: int) -> str:
        problems = []
        for e in expects:
            (k, v), = e.items()
            if k == "visible":
                try:
                    await _locator(page, str(v)).wait_for(state="visible", timeout=8_000 if not self.fast else 4_000)
                except Exception:
                    problems.append(f"{readable(v)} never appeared.")
            elif k == "url_contains":
                if str(v) not in page.url:
                    problems.append(f"Expected the address to contain {v}, but it is {page.url}.")
            elif k == "no_console_errors" and v:
                if len(errors) > err_before:
                    problems.append("BROWSER_ERRORS")
            elif k == "lcp_under_ms":
                lcp = (await page.evaluate("() => (window.__qcVitals || {}).LCP")) or 0
                if lcp and lcp > float(v):
                    problems.append(f"The main content took {lcp / 1000:.1f} s to appear (limit {float(v) / 1000:.1f} s).")
            elif k == "no_layout_shift_over":
                cls = (await page.evaluate("() => (window.__qcVitals || {}).CLS")) or 0
                if cls > float(v):
                    problems.append(f"The page jumped while loading (layout shift {cls:.2f}, limit {v}).")
        return " ".join(problems)

    @staticmethod
    def _explain(label: str, problem: str, new_errors: List[str], new_requests: int) -> str:
        problem = problem.replace("BROWSER_ERRORS", "").strip()
        parts = [f"{label}: {problem}" if problem else f"{label}:"]
        if label.startswith("Tap") and new_requests == 0:
            parts.append("After the tap the page sent nothing to the server.")
        if new_errors:
            n = len(new_errors)
            parts.append(f"{'A browser error' if n == 1 else f'{n} browser errors'} appeared: {new_errors[0][:160]}")
        return " ".join(parts)

    async def _shot(self, page, out: JourneyOutcome, i: int) -> Optional[str]:
        import os
        os.makedirs(self.objects_dir, exist_ok=True)
        name = f"journey-{abs(hash((out.name, out.device, time.time()))) % 10**12}-{i}.jpg"
        try:
            await page.screenshot(path=os.path.join(self.objects_dir, name), type="jpeg", quality=60, full_page=True)
            return name
        except Exception:
            return None
