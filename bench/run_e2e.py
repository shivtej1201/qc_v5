"""
End-to-end on-page audit benchmark: baseline (pre-change) engine vs optimized engine on the
same simulated edge, same source inventory, same fixed request budget.

  python bench/run_e2e.py --engine optimized --code-dir . --sources 3000 --rps 8 --out result.json
  python bench/run_e2e.py --engine baseline  --code-dir /path/to/baseline --sources 3000 --rps 8 ...

--prefill-rels N: before the run, put N relationship rows (and their completed source pages and
validated cache rows) for the same job into the DB, so the run executes at real-run DB scale.
--template-db: reuse/create a prefilled template (copied per run).
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))


def wait_port(host, port, t=20):
    end = time.time() + t
    while time.time() < end:
        try:
            socket.create_connection((host, port), 0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("sim edge did not start")


def build_prefill(path, job, n_rels, n_live_sources, code_dir):
    """Prefilled DB: n_rels relationship rows from ~n_rels/35 completed '/old/' source pages."""
    sys.path.insert(0, code_dir)
    from on_page_audit_engine import OnPageAuditEngine  # noqa
    n_old = max(1, n_rels // 35)
    e = OnPageAuditEngine(db_path=path, job_id=job, primary_domain="")
    base = "http://127.0.0.1:PORT"
    e.initialize_inventory(
        [{"original_url": f"{base}/old/{i}", "normalized_url": f"{base}/old/{i}"} for i in range(n_old)]
    )
    c = e.conn
    with c:
        c.execute("UPDATE on_page_source_pages SET crawl_state='COMPLETED', http_status=200, raw_occurrences_count=36, "
                  "relationships_count=35, business='CD', page_type='PDP' WHERE job_id=?", (job,))
    rows, seeds = [], []
    import random
    rnd = random.Random(1)
    for i in range(n_old):
        src = f"{base}/old/{i}"
        dests = {src} | {f"{base}/old/{rnd.randrange(n_old)}" for _ in range(34)}
        for d in dests:
            rows.append((job, src, 200, src, d, d, "ANCHOR", "ABSOLUTE", "INTERNAL", "x", '["x"]', '["ANCHOR"]', "", "", 1,
                         "Content Section", "CD", "EN", "PDP", "REUSED", "", "2026-11-18T00:00:00Z"))
        if len(rows) >= 100000:
            with c:
                c.executemany("INSERT OR IGNORE INTO on_page_link_relationships (job_id, source_page_url, source_page_status, "
                              "source_page_final_url, found_url, normalized_found_url, found_url_type, url_syntax_type, found_url_scope, "
                              "anchor_text, anchor_texts, link_types, rel, hreflang, occurrence_count, representative_location, business, "
                              "language, page_type, validation_source, remark, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            rows = []
    if rows:
        with c:
            c.executemany("INSERT OR IGNORE INTO on_page_link_relationships (job_id, source_page_url, source_page_status, "
                          "source_page_final_url, found_url, normalized_found_url, found_url_type, url_syntax_type, found_url_scope, "
                          "anchor_text, anchor_texts, link_types, rel, hreflang, occurrence_count, representative_location, business, "
                          "language, page_type, validation_source, remark, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    with c:
        # first occurrence of each destination is the NEW_CHECK (keeps packager invariants true)
        c.execute("""UPDATE on_page_link_relationships SET validation_source='NEW_CHECK' WHERE id IN (
                     SELECT MIN(id) FROM on_page_link_relationships WHERE job_id=? GROUP BY normalized_found_url)""", (job,))
        c.execute("""INSERT OR IGNORE INTO on_page_validation_cache (job_id, normalized_url, url_scope, validation_state, http_status,
                     final_url, status_bucket, status_classification, remark, referenced, checked_at)
                     SELECT DISTINCT ?, normalized_found_url, 'INTERNAL', 'VALIDATED', 200, normalized_found_url, '2xx', 'OK',
                     '200 OK — Working', 1, '2026-11-18T00:00:00Z' FROM on_page_link_relationships WHERE job_id=?""", (job, job))
    e.close()
    return n_old


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--engine", choices=["baseline", "optimized"], required=True)
    p.add_argument("--code-dir", required=True)
    p.add_argument("--sources", type=int, default=3000)
    p.add_argument("--rps", type=float, default=8.0)
    p.add_argument("--max-rps", type=float, default=None)
    p.add_argument("--latency-ms", type=float, default=300.0)
    p.add_argument("--waf-rps", type=float, default=0.0)
    p.add_argument("--waf-block", type=float, default=20.0)
    p.add_argument("--source-concurrency", type=int, default=10)
    p.add_argument("--dest-concurrency", type=int, default=20)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--rps-window", type=int, default=2000)
    p.add_argument("--prefill-rels", type=int, default=0)
    p.add_argument("--template-db", default="")
    p.add_argument("--port", type=int, default=8801)
    p.add_argument("--out", required=True)
    a = p.parse_args()

    code_dir = os.path.abspath(a.code_dir)
    a.out = os.path.abspath(a.out)
    work = tempfile.mkdtemp(prefix="qc_e2e_")
    db = os.path.join(work, "bench.db")
    job = "E2E"
    if a.prefill_rels:
        tpl = a.template_db or os.path.join(work, "tpl.db")
        if not os.path.exists(tpl):
            print(f"[bench] building prefill template with {a.prefill_rels:,} relationships ...", flush=True)
            t = time.time()
            build_prefill(tpl, job, a.prefill_rels, a.sources, code_dir)
            print(f"[bench] template built in {time.time()-t:.0f}s", flush=True)
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(tpl + ext):
                shutil.copy(tpl + ext, db + ext)

    sim = subprocess.Popen([sys.executable, os.path.join(HERE, "sim_edge.py"), "--sources", str(a.sources), "--port", str(a.port),
                            "--latency-ms", str(a.latency_ms), "--waf-rps", str(a.waf_rps), "--waf-block", str(a.waf_block)])
    try:
        wait_port("127.0.0.1", a.port)
        wait_port("127.0.0.2", a.port)
        os.chdir(code_dir)
        sys.path.insert(0, code_dir)
        from on_page_audit_engine import OnPageAuditEngine  # noqa

        base = f"http://127.0.0.1:{a.port}"
        if a.prefill_rels:
            c = sqlite3.connect(db)
            c.execute("UPDATE on_page_source_pages SET source_page_url=replace(source_page_url,'PORT',?), "
                      "normalized_source_url=replace(normalized_source_url,'PORT',?) WHERE job_id=?", (str(a.port), str(a.port), job))
            c.commit(); c.close()
        kw = dict(db_path=db, job_id=job, job_name="e2e", allow_local=True, primary_domain="127.0.0.1",
                  target_rps=a.rps, min_rps=min(3.0, a.rps), max_rps=a.max_rps or a.rps,
                  source_concurrency=a.source_concurrency, dest_concurrency=a.dest_concurrency, max_retries=1,
                  connect_timeout=5.0, read_timeout=15.0, akamai_cooldown_sec=a.waf_block)
        if a.batch_size:
            kw["batch_size"] = a.batch_size
        if a.engine == "optimized":
            kw.update(probe_url=f"{base}/robots.txt", probe_interval_sec=5.0, rps_increase_window=a.rps_window)
        eng = OnPageAuditEngine(**kw)
        # live sources appended after any prefilled inventory (initialize_inventory is idempotent per job,
        # so for prefilled DBs insert the live rows directly)
        live = [{"original_url": f"{base}/p/{i}", "normalized_url": f"{base}/p/{i}", "source_sitemap": "sim", "source_sitemap_number": 1}
                for i in range(a.sources)]
        if a.prefill_rels:
            eng.conn = eng._connect_db()
            n0 = eng.conn.execute("SELECT COUNT(*) FROM on_page_source_pages WHERE job_id=?", (job,)).fetchone()[0]
            with eng.conn:
                eng.conn.executemany("INSERT OR IGNORE INTO on_page_source_pages (job_id, source_page_url, source_page_id, source_sitemap, "
                                     "source_sitemap_number, normalized_source_url, language) VALUES (?,?,?,?,?,?,?)",
                                     [(job, s["original_url"], f"SRC_{n0+i+1:06d}", "sim", 1, s["normalized_url"], "EN") for i, s in enumerate(live)])
                eng.conn.execute("UPDATE on_page_audit_jobs SET source_pages_total=? WHERE job_id=?", (n0 + len(live), job))
        else:
            eng.initialize_inventory(live)

        proc = psutil.Process()
        samples = {"rss": 0.0, "cpu": []}
        stop = threading.Event()

        def sampler():
            proc.cpu_percent(None)
            while not stop.is_set():
                time.sleep(1.0)
                samples["rss"] = max(samples["rss"], proc.memory_info().rss / 1048576)
                samples["cpu"].append(proc.cpu_percent(None))

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        t0 = time.time()
        stats = asyncio.run(eng.run())
        wall = time.time() - t0
        stop.set()
        th.join()
        eng.close()
        sim_stats = json.loads(urllib.request.urlopen(f"{base}/__stats", timeout=10).read())

        c = sqlite3.connect(db)
        live_rels = c.execute("SELECT COUNT(*) FROM on_page_link_relationships WHERE job_id=? AND source_page_url LIKE ?", (job, f"{base}/p/%")).fetchone()[0]
        c.close()
        k = sim_stats["kind"]
        primary_span = max(1e-6, sim_stats["primary_last_ts"] - sim_stats["primary_first_ts"])
        n_primary = k.get("source_page", 0) + k.get("extra_dest", 0) + k.get("robots", 0) + k.get("waf_403", 0) + k.get("other", 0)
        res = {
            "engine": a.engine, "sources": a.sources, "rps_target": a.rps, "latency_ms": a.latency_ms,
            "prefill_rels": a.prefill_rels, "waf_rps_model": a.waf_rps,
            "wall_sec": round(wall, 1),
            "source_pages_per_sec": round(a.sources / wall, 2),
            "relationships_persisted_live": live_rels,
            "relationships_per_sec": round(live_rels / wall, 1),
            "status": stats.get("status"),
            "unique_http_destinations": stats.get("unique_http_destinations"),
            "new_http_validations": stats.get("new_http_validations"),
            "reused_http_validations": stats.get("reused_http_validations"),
            "server_requests": k, "server_total_requests": sim_stats["total"],
            "primary_requests": n_primary,
            "achieved_primary_rps": round(n_primary / primary_span, 2),
            "max_primary_req_in_any_1s": sim_stats["max_primary_req_in_1s"],
            "duplicate_paths": sim_stats["duplicate_paths"], "duplicate_examples": sim_stats["duplicate_examples"],
            "waf_403_served": sim_stats["waf_403_served"], "waf_blocks": sim_stats["waf_blocks"],
            "rss_peak_mb": round(samples["rss"], 1),
            "cpu_avg_pct": round(sum(samples["cpu"]) / max(1, len(samples["cpu"])), 1),
            "cpu_peak_pct": round(max(samples["cpu"] or [0]), 1),
            "engine_performance": stats.get("performance"),
            "counter_reconciliation": stats.get("counter_reconciliation"),
            "cooldowns": getattr(eng, "cooldown_count", None), "target_rps_final": getattr(eng, "target_rps", None),
        }
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2, default=str)
        print(json.dumps({k2: v for k2, v in res.items() if k2 not in ("engine_performance", "duplicate_examples")}, indent=1, default=str))
    finally:
        sim.terminate()
        sim.wait()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
