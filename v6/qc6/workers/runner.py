"""Run orchestration and the job queue (blueprint §2, §5).

`python -m qc6.workers.runner` starts a worker: it claims queued runs with SELECT … FOR UPDATE SKIP LOCKED, runs the
stages the run asks for (coverage -> visits -> journeys), and reports progress over LISTEN/NOTIFY.
One site never has two runs at once (the claim skips sites that already have a running run).
"""
from __future__ import annotations

import asyncio
import logging
import os
import traceback
from typing import Any, Dict, List, Optional

from qc6.db.pool import get_pool
from qc6.settings import settings
from qc6.workers.analysis import Item, apply, close_run, notify, snapshot_score

log = logging.getLogger("qc6.runner")

CLAIM_SQL = """
UPDATE runs SET status = 'running', started_at = now()
WHERE id = (
  SELECT r.id FROM runs r WHERE r.status = 'queued'
    AND NOT EXISTS (SELECT 1 FROM runs x WHERE x.site_id = r.site_id AND x.status IN ('running', 'pausing'))
  ORDER BY r.id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id"""


async def discover_urls(site: Dict[str, Any], opts: Dict[str, Any]) -> List[str]:
    if opts.get("urls"):
        return list(dict.fromkeys(opts["urls"]))[: int(opts.get("max_pages", 300_000))]
    from qcv5.crawler.sitemap_extractor import SitemapExtractor
    ex = SitemapExtractor(allow_local=settings.allow_private_targets, max_urls=int(opts.get("max_pages", 300_000)))
    res = await ex.extract_all([opts.get("sitemap") or site["base_url"]])
    return [u["normalized_url"] for u in res.get("unique_urls", [])]


async def journey_stage(pool, browser, run_id: int, site: Dict[str, Any], fast: bool = False) -> Dict[str, Any]:
    from qc6.workers.journey import JourneyRunner
    rows = await pool.fetch("SELECT id, name, spec FROM journeys WHERE site_id = $1 AND enabled ORDER BY id", site["id"])
    runner = JourneyRunner(browser, site, settings.objects_dir, fast=fast)
    items, passed = [], 0
    for j in rows:
        out = await runner.run(j["spec"])
        async with pool.acquire() as conn:
            jr = await conn.fetchval(
                "INSERT INTO journey_runs (journey_id, run_id, device, status, failed_step, failure_text, duration_ms) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                j["id"], run_id, out.device, out.status, out.failed_step, out.failure_text, out.duration_ms)
            await conn.executemany(
                "INSERT INTO step_results (journey_run_id, idx, action, label, status, detail, screenshot, duration_ms) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                [(jr, s.idx, s.action, s.label, s.status, s.detail, s.screenshot, s.duration_ms) for s in out.steps])
            await notify(conn, run_id, "journey", name=out.name, status=out.status, failed_step=out.failed_step)
        if out.status == "passed":
            passed += 1
        else:
            first = out.failed_step or 0
            url = site["base_url"]
            items.append(Item("JOURNEY_FAILED", url, "journey", None, out.name, {"value": first},
                              {"failure": out.failure_text, "device": out.device}))
    applied = await apply(pool, run_id, site["id"], items) if items else {}
    moved = await close_run(pool, run_id, site["id"], ["journey"], complete=True)
    return {"journeys": len(rows), "journeys_passed": passed, "journey_lifecycle": moved, **{f"journey_{k}": v for k, v in applied.items()}}


async def execute_run(pool, run_id: int, browser=None, fast_journeys: bool = False) -> Dict[str, Any]:
    run = await pool.fetchrow("SELECT * FROM runs WHERE id = $1", run_id)
    site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", run["site_id"]))
    opts: Dict[str, Any] = dict(run["options"] or {})
    kind = run["kind"]
    summary: Dict[str, Any] = {}
    own_pw = None
    try:
        await pool.execute("UPDATE runs SET status = 'running', started_at = COALESCE(started_at, now()) WHERE id = $1", run_id)
        async with pool.acquire() as conn:
            await notify(conn, run_id, "started", kind=kind)
        need_browser = kind in ("visit", "journey", "full") or opts.get("with_visits")
        if need_browser and browser is None:
            from playwright.async_api import async_playwright
            own_pw = await async_playwright().start()
            browser = await own_pw.chromium.launch()
        if kind in ("coverage", "full"):
            from qc6.workers.coverage import run_coverage
            urls = await discover_urls(site, opts)
            if not urls:
                raise RuntimeError("No pages found: the sitemap is empty or could not be read.")
            summary.update(await run_coverage(pool, run_id, site, urls, {**opts, "with_visits": kind == "full"}))
        if kind in ("visit", "full"):
            from qc6.workers.visit_stage import run_visit_stage
            pages_run = run_id
            if kind == "visit":       # visit the pages of the site's latest coverage run
                pages_run = await pool.fetchval("SELECT id FROM runs WHERE site_id = $1 AND kind IN ('coverage','full') "
                                                "AND status = 'completed' ORDER BY id DESC LIMIT 1", site["id"])
                if pages_run is None:
                    raise RuntimeError("Run a coverage check first; real visits sample its pages.")
                await pool.execute("INSERT INTO pages (run_id, url, path, template_id, http_status, final_url, title, facts, fetched_at) "
                                   "SELECT $1, url, path, template_id, http_status, final_url, title, facts, fetched_at FROM pages "
                                   "WHERE run_id = $2 ON CONFLICT DO NOTHING", run_id, pages_run)
            summary.update(await run_visit_stage(pool, browser, run_id, site, opts))
        if kind in ("journey", "full"):
            summary.update(await journey_stage(pool, browser, run_id, site, fast=fast_journeys or opts.get("fast_journeys", False)))
        pages_total = await pool.fetchval("SELECT COUNT(*) FROM pages WHERE run_id = $1", run_id) or \
            await pool.fetchval("SELECT COUNT(*) FROM pages WHERE run_id = (SELECT MAX(id) FROM runs WHERE site_id = $1 "
                                "AND kind IN ('coverage','full') AND status = 'completed')", site["id"]) or 0
        sc = await snapshot_score(pool, run_id, site["id"], pages_total)
        summary["score"] = sc.get("score")
        await pool.execute("UPDATE runs SET status = 'completed', finished_at = now(), summary = $2 WHERE id = $1", run_id, summary)
        async with pool.acquire() as conn:
            await notify(conn, run_id, "completed", score=sc.get("score"))
    except Exception as e:
        log.error("run %s failed: %s", run_id, traceback.format_exc())
        await pool.execute("UPDATE runs SET status = 'failed', finished_at = now(), error = $2, summary = $3 WHERE id = $1",
                           run_id, f"{type(e).__name__}: {e}"[:500], summary)
        async with pool.acquire() as conn:
            await notify(conn, run_id, "failed", error=str(e)[:300])
    finally:
        if own_pw is not None:
            await browser.close()
            await own_pw.stop()
    return summary


async def worker_loop(poll_s: float = 2.0) -> None:
    pool = await get_pool()
    log.info("worker %s ready", settings.worker_id)
    while True:
        run_id = await pool.fetchval(CLAIM_SQL)
        if run_id is None:
            await asyncio.sleep(poll_s)
            continue
        log.info("claimed run %s", run_id)
        await execute_run(pool, run_id)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("QC6_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(worker_loop())
