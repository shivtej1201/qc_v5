"""Visit plan -> real-visitor review -> findings (blueprint §5 steps 5-7, §6.3)."""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Any, Dict, List, Optional

from qc6.crawl.budget import HostBudget
from qc6.settings import settings
from qc6.workers.analysis import Item, apply, close_run, notify
from qc6.workers.visit import VisitResult, VisitTarget, run_visits

LCP_POOR_MS, CLS_POOR, INP_POOR_MS = 2500, 0.1, 200
SERIOUS = {"serious", "critical"}


async def plan(pool, run_id: int, per_template: int = 25, mobile_share: float = 0.6, seed: int = 11,
               max_visits: int = 10_000, traffic: Optional[Dict[str, int]] = None) -> List[VisitTarget]:
    """Every template gets up to `per_template` pages (random but reproducible), plus every page the server HTML
    marks as a likely JavaScript shell. Device per visit follows the mobile share (from GA4 when connected)."""
    rng = random.Random(seed)
    rows = await pool.fetch("SELECT url, template_id, facts FROM pages WHERE run_id = $1 AND http_status BETWEEN 200 AND 299",
                            run_id)
    by_tpl: Dict[Optional[int], List] = defaultdict(list)
    for r in rows:
        by_tpl[r["template_id"]].append(r)
    chosen, seen = [], set()
    import urllib.parse as _up
    traffic = traffic or {}
    for tpl, rs in sorted(by_tpl.items(), key=lambda kv: -len(kv[1])):
        # pages people actually land on first (GA4), the rest of the sample at random, so rare pages still get seen
        busy = sorted((r for r in rs if traffic.get(_up.urlsplit(r["url"]).path, 0) > 0),
                      key=lambda r: -traffic[_up.urlsplit(r["url"]).path])
        top = busy[: (per_template + 1) // 2]
        rest = [r for r in rs if r not in top]
        for r in top + rng.sample(rest, min(per_template - len(top), len(rest))):
            chosen.append(r)
            seen.add(r["url"])
    for r in rows:
        if r["url"] not in seen and (r["facts"] or {}).get("js_rendering_suspected"):
            chosen.append(r)
            seen.add(r["url"])
    chosen = chosen[:max_visits]
    return [VisitTarget(r["url"], "mobile" if rng.random() < mobile_share else "desktop", r["template_id"], dict(r["facts"] or {}))
            for r in chosen]


def findings_from(results: List[VisitResult], host: str) -> Dict[str, Any]:
    """Per template: a problem counts when it shows up on at least half of that template's successful visits, so one
    flaky visit doesn't create a fix, and a confirmed one is projected to every page of the template."""
    by_tpl: Dict[Optional[int], List[VisitResult]] = defaultdict(list)
    for r in results:
        if r.status == "ok":
            by_tpl[r.template_id].append(r)
    items: List[Item] = []
    for tpl, vs in by_tpl.items():
        n = len(vs)
        need = max(1, (n + 1) // 2)

        def hits(pred):
            return [v for v in vs if pred(v)]

        def add(rule, pred, value_fn, subject=""):
            h = hits(pred)
            if len(h) >= need:
                for v in h:
                    items.append(Item(rule, v.url, "visit", tpl, subject, {"value": value_fn(h)},
                                      {"device": v.device, "vitals": v.vitals, "screenshot": v.screenshot}))

        p75 = lambda key: (lambda h: _p75([v.vitals.get(key) for v in h if v.vitals.get(key) is not None]))
        add("LAYOUT_SHIFT", lambda v: (v.vitals.get("CLS") or 0) > CLS_POOR, lambda h: f"{p75('CLS')(h):.2f}")
        add("SLOW_MAIN_CONTENT", lambda v: (v.vitals.get("LCP") or 0) > LCP_POOR_MS, lambda h: f"{p75('LCP')(h) / 1000:.1f} s")
        add("SLOW_RESPONSE_TO_TAP", lambda v: (v.vitals.get("INP") or 0) > INP_POOR_MS, lambda h: f"{int(p75('INP')(h))} ms")
        add("CONTENT_ONLY_AFTER_JS", lambda v: v.render_diff.get("h1_only_after_js"), lambda h: "")
        add("LINKS_ONLY_AFTER_JS", lambda v: (v.render_diff.get("links_only_after_js") or 0) >= 5,
            lambda h: str(sorted(v.render_diff["links_only_after_js"] for v in h)[len(h) // 2]))
        add("CONSOLE_ERRORS", lambda v: bool(v.console), lambda h: h[0].console[0][:120])
        add("CONSENT_SHIFTS_PAGE", lambda v: (v.consent.get("cls_added") or 0) > 0.05, lambda h: "")
        viol = defaultdict(list)
        for v in vs:
            for a in v.a11y:
                if a.get("impact") in SERIOUS:
                    viol[a["id"]].append((v, a))
        for vid, pairs in viol.items():
            if len(pairs) >= need:
                for v, a in pairs:
                    items.append(Item("A11Y_SERIOUS", v.url, "visit", tpl, vid, {"value": a["help"][:110]},
                                      {"target": a.get("target"), "count": a.get("count"), "impact": a.get("impact")}))
    challenged = [r for r in results if r.status == "challenged"]
    if challenged:
        items.append(Item("ACCESS_CHALLENGED", challenged[0].url, "visit", None, host, {}, {"vendor": challenged[0].error}))
    return {"items": items, "visited": sum(len(v) for v in by_tpl.values()), "templates": len(by_tpl),
            "challenged": len(challenged), "failed": sum(1 for r in results if r.status == "failed")}


async def visual_changes(pool, run_id: int, site_id: int, threshold: float = 0.15) -> List[Dict[str, Any]]:
    """Compare each visit's screenshot with the same page and device from the site's previous check. Store the
    changed share per visit; report page types where the median page changed by more than `threshold`."""
    import os
    from qc6.domain.visual import changed_ratio
    rows = await pool.fetch("""
        SELECT v.id, v.template_id, v.screenshot, prev.screenshot AS prev_shot
          FROM visits v
          JOIN LATERAL (SELECT p.screenshot FROM visits p JOIN runs r ON r.id = p.run_id
                         WHERE r.site_id = $2 AND p.run_id < $1 AND p.page_url = v.page_url AND p.device = v.device
                           AND p.status = 'ok' AND p.screenshot IS NOT NULL ORDER BY p.id DESC LIMIT 1) prev ON true
         WHERE v.run_id = $1 AND v.status = 'ok' AND v.screenshot IS NOT NULL""", run_id, site_id)
    by_tpl: Dict[Optional[int], List[float]] = defaultdict(list)
    for r in rows:
        a, b = (os.path.join(settings.objects_dir, x) for x in (r["prev_shot"], r["screenshot"]))
        if not (os.path.exists(a) and os.path.exists(b)):
            continue
        try:
            ratio = round(changed_ratio(a, b), 4)
        except Exception:
            continue
        await pool.execute("UPDATE visits SET visual_change = $2 WHERE id = $1", r["id"], ratio)
        by_tpl[r["template_id"]].append(ratio)
    out = []
    names = {t["id"]: t["name"] for t in await pool.fetch("SELECT id, name FROM templates WHERE site_id = $1", site_id)}
    for tpl, vals in by_tpl.items():
        med = sorted(vals)[len(vals) // 2]
        if med > threshold:
            out.append({"template_id": tpl, "template": names.get(tpl, "Pages"), "changed": med, "pages_compared": len(vals)})
    return sorted(out, key=lambda x: -x["changed"])


async def _all_results(pool, run_id: int) -> List[VisitResult]:
    rows = await pool.fetch("SELECT * FROM visits WHERE run_id = $1", run_id)
    return [VisitResult(r["page_url"], r["device"], r["template_id"], r["status"], r["http_status"], dict(r["vitals"]),
                        list(r["a11y"]), dict(r["render_diff"]), list(r["console"]), dict(r["consent"]), r["screenshot"],
                        list(r["timeline"]), r["error"], r["duration_ms"] or 0) for r in rows]


def _p75(vals: List[float]) -> float:
    vals = sorted(v for v in vals if v is not None)
    return vals[min(len(vals) - 1, int(0.75 * len(vals)))] if vals else 0.0


async def run_visit_stage(pool, browser, run_id: int, site: Dict[str, Any], opts: Dict[str, Any],
                          control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ga = None
    try:
        from qc6 import ga4
        ga = await ga4.traffic(site)
    except Exception:
        ga = None                                  # GA4 is optional; the plan falls back to random sampling
    mobile_share = (ga or {}).get("mobile_share") or float(site["settings"].get("mobile_share", 0.6))
    targets = await plan(pool, run_id, per_template=int(opts.get("per_template", 25)), mobile_share=mobile_share,
                         traffic=(ga or {}).get("paths"))
    already = {(r["page_url"], r["device"]) for r in await pool.fetch(
        "SELECT page_url, device FROM visits WHERE run_id = $1 AND status IN ('ok','failed')", run_id)}
    targets = [t for t in targets if (t.url, t.device) not in already]      # resume: skip pages already visited
    rate = float(opts.get("rate_rps", site["settings"].get("rate_rps", 12)))
    await HostBudget.ensure(pool, site["primary_host"], rate_rps=rate, burst=max(2.0, rate))
    budget = HostBudget(pool, site["primary_host"], share=settings.visit_share)
    done = {"n": 0}

    async def on_result(r: VisitResult, lane: int):
        done["n"] += 1
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO visits (run_id, page_url, template_id, device, status, http_status, vitals, a11y, render_diff,
                                       console, consent, screenshot, timeline, error, duration_ms)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                run_id, r.url, r.template_id, r.device, r.status, r.http_status, r.vitals, r.a11y, r.render_diff,
                r.console, r.consent, r.screenshot, r.timeline, r.error, r.duration_ms)
            prog = {"stage": "visits", "visits_done": done["n"], "visits_total": len(targets)}
            await conn.execute("UPDATE runs SET progress = progress || $2::jsonb WHERE id = $1", run_id, prog)
            await notify(conn, run_id, "visit", lane=lane, url=r.url, device=r.device, status=r.status,
                         steps=[s["step"] for s in r.timeline], **prog)

    async def on_step(url: str, device: str, step: str, lane: int):
        if step in ("settled", "done"):
            return                       # keep the stream light: the lane shows what the visitor is doing now
        try:
            async with pool.acquire() as conn:
                await notify(conn, run_id, "visit", lane=lane, url=url, device=device, status="running", steps=[step])
        except Exception:
            pass

    visitors = int(opts.get("visitors", site["settings"].get("visitors", settings.visitors_per_host)))
    persona = None
    if opts.get("fast_visits"):          # tests and demos only: shorter pauses, same steps
        from qc6.crawl.pacing import Persona
        persona = Persona(think_median_s=0.2, think_min_s=0.05, think_max_s=0.4, scroll_pause_min_s=0.05, scroll_pause_max_s=0.1)
    results = await run_visits(browser, site, targets, budget, visitors, on_result=on_result,
                               consent=site["settings"].get("consent", "accept"), persona=persona, on_step=on_step,
                               stop=(control or {}).get("stop"))
    if control is not None and control.get("pause"):
        return {"paused": True}
    if already:                                          # resumed: judge templates on every visit of this run
        results = await _all_results(pool, run_id)
    changed = await visual_changes(pool, run_id, site["id"])
    f = findings_from(results, site["primary_host"])
    tpl_rows = await pool.fetch("SELECT id, name, page_count FROM templates WHERE site_id = $1 AND id = ANY($2::bigint[])",
                                site["id"], [t for t in {r.template_id for r in results} if t is not None])
    applied = await apply(pool, run_id, site["id"], f["items"],
                          template_pages={r["id"]: r["page_count"] for r in tpl_rows},
                          template_names={r["id"]: r["name"] for r in tpl_rows}, project_templates=True)
    moved = await close_run(pool, run_id, site["id"], ["visit"], complete=f["challenged"] == 0)
    return {"templates_changed": changed, "visits_planned": len(targets), "visited": f["visited"], "templates_visited": f["templates"],
            "challenged": f["challenged"], "visits_failed": f["failed"], **{f"visit_{k}": v for k, v in applied.items()},
            "visit_lifecycle": moved}
