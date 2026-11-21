"""Coverage crawl: the V5 engine (proven at 300K pages / 3 h 22 min), wrapped for V6 (blueprint §7).

* identifies honestly: V5's request headers + `User-Agent: … QC-Audit/6.0` + `X-QC-Audit: <site token>`
* fingerprints every parsed page for template clustering (no second fetch)
* runs V5's report builder (18-file ZIP kept for compatibility), then imports pages, templates and issues into
  Postgres as V6 findings -> fixes.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from qcv5.audit.on_page_issue_rules import ACTIONS
from qcv5.crawler.on_page_audit_engine import OnPageAuditEngine
from qcv5.reports.on_page_csv_packager import build_on_page_csv_package

from qc6.domain import templates as T
from qc6.settings import settings
from qc6.workers.analysis import Item, apply, close_run, notify, snapshot_score

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


class FingerprintEngine(OnPageAuditEngine):
    """V5 engine + a SimHash of each parsed page's layout, computed in the existing parse pass."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fingerprints: Dict[str, int] = {}
        self._fp_lock = threading.Lock()

    def _parse_and_classify(self, src_url, final_url, html, seo_ctx=None):
        out = super()._parse_and_classify(src_url, final_url, html, seo_ctx)
        if html:
            fp = T.simhash(html)
            with self._fp_lock:
                self.fingerprints[src_url] = fp
        return out


# V5 rule code -> V6 rule (+ how to build subject/fields). Anything else becomes PAGE_CHECK / LINK_CHECK with V5's
# own recommended action as the fix text, so nothing V5 finds is lost.
LINK_BROKEN = {"NOT_FOUND", "GONE", "CLIENT_ERROR", "BROKEN_REDIRECT", "REDIRECT_TO_BROKEN", "SERVER_ERROR",
               "DNS_FAILURE", "CONNECTION_FAILED", "SSL_ERROR", "REDIRECT_LOOP", "TIMEOUT"}
DIRECT = {"H1_MISSING": "H1_MISSING", "MISSING_TITLE": "TITLE_MISSING", "META_DESCRIPTION_MISSING": "DESCRIPTION_MISSING",
          "NOINDEX_IN_SITEMAP": "NOINDEX_IN_SITEMAP", "SITEMAP_URL_NOT_CANONICAL": "CANONICAL_ELSEWHERE",
          "SLOW_RESPONSE": "SLOW_SERVER", "MIXED_CONTENT": "MIXED_CONTENT", "MIXED_CONTENT_IMAGE": "MIXED_CONTENT",
          "SOURCE_NOT_FOUND": "PAGE_NOT_FOUND", "SOURCE_SERVER_ERROR": "PAGE_SERVER_ERROR",
          "DUPLICATE_TITLE": "TITLE_DUPLICATE", "SOURCE_WAF_BLOCKED": "ACCESS_CHALLENGED"}
SKIP = {"CLASSIFICATION_NEEDS_REVIEW"}       # internal bookkeeping, not a site problem


def human(code: str) -> str:
    return code.replace("_", " ").capitalize()


def map_issue(r: sqlite3.Row, template_id: Optional[int], host: str) -> Optional[Item]:
    code, el = r["rule_code"], (r["element_type"] or "").upper()
    page = r["page_url"]
    target = r["subject_url"] or r["found_url"] or ""
    if code in SKIP:
        return None
    ev = {"v5_rule": code, "detail": (r["detail"] or "")[:300], "status": r["http_status"]}
    if code in LINK_BROKEN and r["category"] != "SOURCE_PAGE":
        rule = "BROKEN_IMAGE" if "IMAGE" in el or r["category"] in ("IMAGE", "BROKEN_IMAGE") else "BROKEN_LINK"
        return Item(rule, page, "coverage", template_id, target, {"value": r["http_status"] or code}, ev)
    if code in ("REDIRECTED", "REDIRECT_CHAIN", "IMAGE_REDIRECTED"):
        return Item("REDIRECTED_LINK", page, "coverage", template_id, target, {"value": r["final_url"] or "the final address"}, ev)
    if code in DIRECT:
        rule = DIRECT[code]
        subject = {"TITLE_DUPLICATE": (r["anchor_or_alt_text"] or target)[:200], "ACCESS_CHALLENGED": host}.get(rule, "")
        value = {"PAGE_NOT_FOUND": r["http_status"] or 404, "PAGE_SERVER_ERROR": r["http_status"] or 500,
                 "CANONICAL_ELSEWHERE": target, "SLOW_SERVER": (r["detail"] or "over 3 s")[:40]}.get(rule, "")
        return Item(rule, page, "coverage", template_id if rule not in ("TITLE_DUPLICATE",) else None, subject, {"value": value}, ev)
    action = ACTIONS.get(code) or (r["detail"] or human(code))
    if r["category"] in ("LINK", "INTERNAL", "EXTERNAL", "IMAGE", "BROKEN_LINK", "REDIRECT") and target:
        return Item("LINK_CHECK", page, "coverage", template_id, f"{code}|{target}", {"value": human(code), "subject": action}, ev)
    return Item("PAGE_CHECK", page, "coverage", template_id, code, {"value": human(code).lower(), "subject": action}, ev)


def identity_headers(site: Dict[str, Any]) -> Dict[str, str]:
    """How QC introduces itself on every request: a real browser UA plus 'QC-Audit/6.0', and the site's access token."""
    return {"User-Agent": f"{BROWSER_UA} {settings.ua_suffix}", settings.access_header: site["access_token"]}


async def run_coverage(pool, run_id: int, site: Dict[str, Any], urls: List[str], opts: Dict[str, Any],
                       control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    run_dir = os.path.join(settings.data_dir, "runs", str(run_id))
    os.makedirs(run_dir, exist_ok=True)
    db_path = os.path.join(run_dir, "coverage.db")
    job = f"RUN{run_id}"
    host = site["primary_host"]
    rate = float(opts.get("rate_rps", site["settings"].get("rate_rps", 12)))
    coverage_rate = max(0.5, rate * (1.0 - settings.visit_share)) if opts.get("with_visits", True) else rate
    eng = FingerprintEngine(
        db_path=db_path, job_id=job, job_name=f"Run {run_id}", allow_local=settings.allow_private_targets,
        primary_domain=host, target_rps=coverage_rate, min_rps=max(0.5, coverage_rate / 3), max_rps=coverage_rate,
        source_concurrency=int(opts.get("source_concurrency", 20)), dest_concurrency=int(opts.get("dest_concurrency", 20)),
        image_concurrency=int(opts.get("image_concurrency", 30)), audit_images=opts.get("images", True),
        audit_seo=True, probe_url=f"{site['base_url'].rstrip('/')}/robots.txt")
    eng.extra_headers = identity_headers(site)
    entries = [{"url": u, "source_sitemap": "", "source_sitemap_number": 1} for u in urls]
    eng.initialize_inventory(entries)                 # idempotent: on resume, finished pages stay finished
    fp_path = os.path.join(run_dir, "fingerprints.json")
    if os.path.exists(fp_path):                       # layout fingerprints of pages parsed before a pause
        import json as _json
        with open(fp_path, encoding="utf-8") as f:
            eng.fingerprints.update({k: int(v) for k, v in _json.load(f).items()})
    if control is not None:
        control["engine"] = eng

    async def progress_loop():
        while True:
            await asyncio.sleep(2.0)
            try:
                c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
                done = c.execute("SELECT COUNT(*) FROM on_page_source_pages WHERE job_id = ? AND crawl_state != 'PENDING'",
                                 (job,)).fetchone()[0]
                c.close()
            except sqlite3.Error:
                continue
            prog = {"stage": "coverage", "pages_done": done, "pages_total": len(urls)}
            async with pool.acquire() as conn:
                await conn.execute("UPDATE runs SET progress = progress || $2::jsonb WHERE id = $1", run_id, prog)
                await notify(conn, run_id, "progress", **prog)

    ticker = asyncio.create_task(progress_loop())
    try:
        stats = await eng.run()
    finally:
        ticker.cancel()
        eng.close()
        if control is not None:
            control["engine"] = None
    if eng._stop_requested:                           # paused: keep the checkpoint, build nothing yet
        import json as _json
        with open(fp_path, "w", encoding="utf-8") as f:
            _json.dump({k: str(v) for k, v in eng.fingerprints.items()}, f)
        return {"paused": True, "pages_done": int(stats.get("source_pages_processed") or 0)}
    pkg = await asyncio.to_thread(build_on_page_csv_package, db_path, job, os.path.join(run_dir, "reports"),
                                  package_name="QC_V6_COVERAGE", project_id=host)
    from qc6 import facts
    fact_counts = await asyncio.to_thread(facts.export, db_path, job, run_id)
    out = await import_coverage(pool, run_id, site, db_path, job, eng.fingerprints, stats, pkg)
    out["facts"] = fact_counts
    return out


async def import_coverage(pool, run_id: int, site: Dict[str, Any], db_path: str, job: str,
                          fingerprints: Dict[str, int], stats: Dict[str, Any], pkg: Dict[str, Any]) -> Dict[str, Any]:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    pages = c.execute("""SELECT s.source_page_url AS url, s.http_status, s.final_url, s.page_title,
                                p.h1_count, p.title_length, p.meta_description_length, p.word_count, p.internal_links,
                                p.external_links, p.noindex, p.canonical_url, p.schema_types, p.js_rendering_suspected
                           FROM on_page_source_pages s LEFT JOIN on_page_seo_pages p
                             ON p.job_id = s.job_id AND p.source_page_url = s.source_page_url
                          WHERE s.job_id = ?""", (job,)).fetchall() if _has_col(c, "on_page_seo_pages", "canonical_url") else \
        c.execute("""SELECT s.source_page_url AS url, s.http_status, s.final_url, s.page_title, p.h1_count, p.title_length,
                            p.meta_description_length, p.word_count, p.internal_links, p.external_links, p.noindex,
                            '' AS canonical_url, p.schema_types, p.js_rendering_suspected
                       FROM on_page_source_pages s LEFT JOIN on_page_seo_pages p
                         ON p.job_id = s.job_id AND p.source_page_url = s.source_page_url
                      WHERE s.job_id = ?""", (job,)).fetchall()

    # templates
    idx = T.TemplateIndex(radius=10)
    page_tpl: Dict[str, int] = {}
    for r in pages:
        fp = fingerprints.get(r["url"])
        if fp is not None:
            page_tpl[r["url"]] = idx.assign(fp, urllib.parse.urlsplit(r["url"]).path)
    db_tpl: Dict[int, int] = {}
    names: Dict[int, str] = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Templates keep their identity across runs (fix keys depend on it): a cluster joins the site's existing
            # template with the nearest layout fingerprint (within the clustering radius) and the same URL pattern
            # family; only genuinely new layouts get a new template.
            existing = [(r["id"], T.from_signed(r["fingerprint"]), r["path_pattern"])
                        for r in await conn.fetch("SELECT id, fingerprint, path_pattern FROM templates WHERE site_id = $1", site["id"])]
            taken: set = set()
            for t in idx.summary():
                fp = idx.centroids[t["id"]]
                cands = [(T.distance(fp, efp), eid) for eid, efp, pat in existing
                         if eid not in taken and T.distance(fp, efp) <= idx.radius]
                same_pattern = [c for c in cands if next(p for i, _, p in existing if i == c[1]) == t["pattern"]]
                pick = min(same_pattern or cands, default=None)
                if pick is not None:
                    tid = pick[1]
                    await conn.execute("UPDATE templates SET name = $2, path_pattern = $3, page_count = $4 WHERE id = $1",
                                       tid, t["name"], t["pattern"], t["pages"])
                else:
                    tid = await conn.fetchval(
                        "INSERT INTO templates (site_id, fingerprint, name, path_pattern, page_count) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                        site["id"], T.to_signed(fp), t["name"], t["pattern"], t["pages"])
                taken.add(tid)
                db_tpl[t["id"]] = tid
                names[tid] = t["name"]
            await conn.executemany(
                """INSERT INTO pages (run_id, url, path, template_id, http_status, final_url, title, facts, fetched_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now()) ON CONFLICT (run_id, url) DO NOTHING""",
                [(run_id, r["url"], urllib.parse.urlsplit(r["url"]).path or "/",
                  db_tpl.get(page_tpl.get(r["url"], -1)), r["http_status"], r["final_url"], r["page_title"],
                  {k: r[k] for k in ("h1_count", "title_length", "meta_description_length", "word_count", "internal_links",
                                     "external_links", "noindex", "canonical_url", "schema_types", "js_rendering_suspected")})
                 for r in pages])

    # issues -> findings -> fixes
    issues = c.execute("SELECT * FROM on_page_issues WHERE job_id = ?", (job,)).fetchall()
    c.close()
    items = []
    for r in issues:
        tpl = db_tpl.get(page_tpl.get(r["page_url"], -1))
        it = map_issue(r, tpl, site["primary_host"])
        if it is not None:
            items.append(it)
    applied = await apply(pool, run_id, site["id"], items, template_names=names)
    complete = int(stats.get("source_pages_pending") or 0) == 0
    moved = await close_run(pool, run_id, site["id"], ["coverage"], complete=complete)
    sc = await snapshot_score(pool, run_id, site["id"], len(pages))
    summary = {"pages": len(pages), "templates": len(db_tpl), "issues_v5": len(issues), **applied, "lifecycle": moved,
               "score": sc.get("score"), "trust_gate": pkg.get("final_qc_status"), "zip": pkg.get("zip_path"),
               "links": stats.get("url_relationships_persisted"), "images": stats.get("image_relationships_persisted")}
    return summary


def _has_col(c: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in c.execute(f"PRAGMA table_info({table})"))
