"""
QC V5 on-page URL + image audit — command-line runner (existing engine + streaming CSV/ZIP package).

    python run_on_page_audit.py --urls sitemap_urls.txt --db audit_cache.db --out-dir reports/onpage \
        --job-id onpage_10k --primary-domain bajajfinserv.in --target-rps 8 --max-rps 10

Re-running with the same --job-id RESUMES (completed pages skipped, validated results reused,
interrupted in-flight work retried). Every limit is a flag; nothing here changes an API contract.
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time

import signal

import psutil

from config import settings
from on_page_audit_engine import OnPageAuditEngine
from on_page_csv_packager import build_on_page_csv_package
from url_normalizer import URLNormalizer


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--urls", required=True, help="file with one source (sitemap) URL per line")
    p.add_argument("--db", default=settings.DB_FILE)
    p.add_argument("--job-id", required=True)
    p.add_argument("--job-name", default="QC V5 On-Page URL + Image Audit")
    p.add_argument("--out-dir", default="reports/onpage")
    p.add_argument("--package-name", default="QC_V5_10K_AUDIT")
    p.add_argument("--primary-domain", default="bajajfinserv.in",
                   help="hosts equal to / ending in this domain share ONE Akamai request budget")
    p.add_argument("--primary-hosts", default="", help="comma-separated extra hosts charged to the primary budget")
    p.add_argument("--target-rps", type=float, default=8.0)
    p.add_argument("--min-rps", type=float, default=3.0)
    p.add_argument("--max-rps", type=float, default=10.0)
    p.add_argument("--rps-increase-window", type=int, default=2000)
    p.add_argument("--source-concurrency", type=int, default=10)
    p.add_argument("--dest-concurrency", type=int, default=20)
    p.add_argument("--image-concurrency", type=int, default=10)
    p.add_argument("--per-host-concurrency", type=int, default=6, help="non-primary hosts (CDNs, external sites)")
    p.add_argument("--max-connections", type=int, default=0, help="0 = 2x the largest worker pool")
    p.add_argument("--host-budget", action="append", default=[], metavar="HOST=RPS[:MAX]",
                   help="independent rate budget for a non-primary host (repeatable); a leading '.' matches "
                        "subdomains, e.g. --host-budget cms-assets.bajajfinserv.in=10:15")
    p.add_argument("--host-budget-cooldown-sec", type=float, default=60.0)
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--http2", action="store_true")
    p.add_argument("--allow-local", action="store_true", help="testing only: allow private/loopback targets")
    p.add_argument("--probe-url", default="")
    p.add_argument("--akamai-cooldown-sec", type=float, default=120.0)
    p.add_argument("--read-timeout", type=float, default=settings.READ_TIMEOUT)
    p.add_argument("--max-retries", type=int, default=settings.MAX_RETRIES)
    p.add_argument("--max-pages", type=int, default=0, help="stop after N pages (checkpoint test); 0 = all")
    p.add_argument("--progress-json", default="")
    p.add_argument("--no-package", action="store_true")
    p.add_argument("--max-source-bytes", type=int, default=5 * 1024 * 1024)
    p.add_argument("--stop-file", default="", help="graceful stop when this file appears (cross-platform)")
    p.add_argument("--result-json", default="", help="write the run result (status, metrics, package) here")
    p.add_argument("--sitemap-meta", default="",
                   help="JSON from sitemap_config (page -> sitemap/lastmod, sitemap tree) for the sitemap + SEO reports")
    p.add_argument("--no-seo", action="store_true", help="skip SEO / technical-SEO page facts")
    return p.parse_args(argv)


def store_sitemap_meta(conn, job_id: str, meta: dict):
    """Idempotent (safe on resume): page -> sitemap/lastmod and the sitemap tree with fetch/hygiene stats."""
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO on_page_sitemap_entries (job_id, normalized_url, sitemap_url, lastmod, changefreq, priority) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(job_id, URLNormalizer.normalize(e["u"]), e.get("sm") or "", str(e.get("lastmod") or "")[:60],
              str(e.get("changefreq") or "")[:20], e.get("priority")) for e in meta.get("entries", [])])
        conn.executemany(
            "INSERT OR IGNORE INTO on_page_sitemap_sources (job_id, sitemap_url, parent_sitemap_url, depth, kind, status, "
            "http_status, classification, urls_listed, duplicates, rejected, size_bytes, selected, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(job_id, s["sitemap_url"], s.get("parent_sitemap_url") or "", int(s.get("depth") or 0), s.get("kind") or "",
              s.get("status") or "", s.get("http_status"), s.get("classification") or "", int(s.get("urls_listed") or 0),
              int(s.get("duplicates") or 0), int(s.get("rejected") or 0), int(s.get("size_bytes") or 0),
              int(s.get("selected", 1)), str(s.get("error_message") or "")[:300]) for s in meta.get("sources", [])])


def run(a) -> dict:
    # Safety caps live in config; raise them only when the operator explicitly asks for more.
    if a.source_concurrency > settings.PER_DOMAIN_CONCURRENCY:
        print(f"[config] PER_DOMAIN_CONCURRENCY {settings.PER_DOMAIN_CONCURRENCY} -> {a.source_concurrency} (explicit)")
        settings.PER_DOMAIN_CONCURRENCY = a.source_concurrency
    if a.dest_concurrency > settings.LINK_AUDIT_CONCURRENCY:
        print(f"[config] LINK_AUDIT_CONCURRENCY {settings.LINK_AUDIT_CONCURRENCY} -> {a.dest_concurrency} (explicit)")
        settings.LINK_AUDIT_CONCURRENCY = a.dest_concurrency
    if a.per_host_concurrency > settings.LINK_AUDIT_PER_HOST_CONCURRENCY:
        settings.LINK_AUDIT_PER_HOST_CONCURRENCY = a.per_host_concurrency

    with open(a.urls, encoding="utf-8") as f:
        urls = [u.strip() for u in f if u.strip()]
    sm_meta = None
    if a.sitemap_meta:
        with open(a.sitemap_meta, encoding="utf-8") as f:
            sm_meta = json.load(f)
    sm_of = {URLNormalizer.normalize(e["u"]): e for e in (sm_meta or {}).get("entries", [])}
    sm_num = {s["sitemap_url"]: i for i, s in enumerate((sm_meta or {}).get("sources", []), 1)}
    entries = []
    for u in urls:
        n = URLNormalizer.normalize(u)
        sm = (sm_of.get(n) or {}).get("sm") or os.path.basename(a.urls)
        entries.append({"original_url": u, "normalized_url": n, "source_sitemap": sm,
                        "source_sitemap_number": sm_num.get(sm, 1)})

    budgets = {}
    for spec in a.host_budget:
        host, _, rates = spec.partition("=")
        lo, _, hi = rates.partition(":")
        if not host.strip() or not lo:
            raise SystemExit(f"--host-budget expects HOST=RPS[:MAX], got {spec!r}")
        budgets[host.strip()] = {"rps": float(lo), "max_rps": float(hi or lo)}

    eng = OnPageAuditEngine(
        db_path=a.db, job_id=a.job_id, job_name=a.job_name, allow_local=a.allow_local,
        primary_domain=a.primary_domain, primary_hosts=[h for h in a.primary_hosts.split(",") if h.strip()],
        target_rps=a.target_rps, min_rps=a.min_rps, max_rps=a.max_rps, rps_increase_window=a.rps_increase_window,
        source_concurrency=a.source_concurrency, dest_concurrency=a.dest_concurrency,
        image_concurrency=a.image_concurrency, per_host_concurrency=a.per_host_concurrency,
        max_connections=a.max_connections or None, audit_images=not a.no_images, http2=a.http2,
        probe_url=a.probe_url or None, akamai_cooldown_sec=a.akamai_cooldown_sec,
        read_timeout=a.read_timeout, max_retries=a.max_retries, progress_json_path=a.progress_json or None,
        host_budgets=budgets, host_budget_cooldown_sec=a.host_budget_cooldown_sec,
        max_source_bytes=a.max_source_bytes, audit_seo=not a.no_seo,
    )
    proc = psutil.Process()
    peak = {"rss": 0.0, "cpu": []}
    stop = threading.Event()

    def sample():
        proc.cpu_percent(None)
        while not stop.is_set():
            time.sleep(1.0)
            peak["rss"] = max(peak["rss"], proc.memory_info().rss / 1048576)
            peak["cpu"].append(proc.cpu_percent(None))

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    t0 = time.time()
    async def _run_with_stop_watch():
        """Graceful stop on SIGTERM/SIGINT (POSIX) or when --stop-file appears (any OS): in-flight work
        finishes and is committed; the job stays resumable with the same --job-id."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, eng.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        run_t = asyncio.create_task(eng.run(max_source_pages_this_run=a.max_pages or None))
        while not run_t.done():
            if a.stop_file and os.path.exists(a.stop_file):
                eng.request_stop()
            await asyncio.wait({run_t}, timeout=1.0)
        return run_t.result()

    try:
        eng.initialize_inventory(entries)
        if sm_meta:
            store_sitemap_meta(eng.conn, a.job_id, sm_meta)
        stats = asyncio.run(_run_with_stop_watch())
    finally:
        eng.close()
        stop.set()
        th.join()
    crawl_sec = time.time() - t0
    perf = dict(stats.get("performance") or {})
    perf["rss_peak_mb"] = round(max(peak["rss"], perf.get("rss_peak_mb") or 0), 1)
    perf["cpu_avg_pct"] = round(sum(peak["cpu"]) / max(1, len(peak["cpu"])), 1)
    perf["elapsed_sec"] = round(crawl_sec, 1)
    el = max(1e-6, crawl_sec)
    perf["total_requests_per_sec"] = round(perf.get("http_requests_total", 0) / el, 2)
    perf["url_validations_per_sec"] = round(perf.get("dest_validations_http", 0) / el, 2)
    perf["image_validations_per_sec"] = round(perf.get("image_validations_http", 0) / el, 2)
    perf["source_pages_per_sec"] = round(perf.get("source_fetches", 0) / el, 2)
    perf["primary_requests_per_sec"] = perf.get("achieved_primary_rps")
    perf["cooldowns"] = perf.get("akamai_cooldowns")
    stopped = bool(eng._stop_requested)
    result = {"status": "STOPPED" if stopped else stats.get("status"), "crawl_seconds": round(crawl_sec, 1),
              "stats": stats, "performance": perf, "stopped": stopped}
    if not a.no_package and not a.max_pages and not stopped:
        t1 = time.time()
        pkg = build_on_page_csv_package(a.db, a.job_id, a.out_dir, package_name=a.package_name, performance=perf)
        result["package"] = {k: v for k, v in pkg.items() if k != "summary"}
        result["package_seconds"] = round(time.time() - t1, 1)
        result["final_qc_status"] = pkg["final_qc_status"]
    if a.result_json:
        tmp = a.result_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        os.replace(tmp, a.result_json)
    return result


if __name__ == "__main__":
    res = run(parse_args())
    print(json.dumps({k: v for k, v in res.items() if k != "stats"}, indent=2, default=str))
    sys.exit(0 if res.get("final_qc_status", "PASS") == "PASS" else 1)
