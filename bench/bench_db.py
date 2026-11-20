"""Sections 21/22/25 SQLite benchmark at real-run scale (4.33M relationships, 125K sources).
1) cost of the existing hot-path sync_job_counters() as the relationship table grows
2) write throughput per batch size (100/250/500/1000 source pages per transaction)
3) write cost of each relationship secondary index"""
import os, sys, time, json, random, sqlite3, tempfile, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qcv5.crawler.on_page_audit_engine import OnPageAuditEngine

RELS_PER_PAGE = 35
def rel_rows(job, src_i, n_sources):
    rnd = random.Random(src_i)
    src = f"https://www.x.in/p/{src_i}"
    out = []
    for k in range(RELS_PER_PAGE):
        d = f"https://www.x.in/p/{rnd.randrange(n_sources)}" if k else src
        out.append(dict(job_id=job, source_page_url=src, source_page_status=200, source_page_final_url=src,
            found_url=d, normalized_found_url=d + ("" if k else ""), found_url_type="ANCHOR", url_syntax_type="ABSOLUTE",
            found_url_scope="INTERNAL", anchor_text="Related product name", anchor_texts='["Related product name"]',
            link_types='["ANCHOR"]', rel="", hreflang="", occurrence_count=1, representative_location="Content Section",
            business="CD", language="EN", page_type="PDP", validation_source="REUSED" if k % 30 else "NEW_CHECK",
            remark="Already validated; reused existing result", extracted_at="2026-11-18T00:00:00Z"))
    # dedupe within page like the engine
    seen = {}; [seen.setdefault(r["normalized_found_url"], r) for r in out]
    return list(seen.values())

INS = """INSERT OR IGNORE INTO on_page_link_relationships (job_id, source_page_url, source_page_status, source_page_final_url,
 found_url, normalized_found_url, found_url_type, url_syntax_type, found_url_scope, anchor_text, anchor_texts, link_types, rel, hreflang,
 occurrence_count, representative_location, business, language, page_type, validation_source, remark, extracted_at) VALUES (
 :job_id, :source_page_url, :source_page_status, :source_page_final_url, :found_url, :normalized_found_url, :found_url_type, :url_syntax_type,
 :found_url_scope, :anchor_text, :anchor_texts, :link_types, :rel, :hreflang, :occurrence_count, :representative_location, :business, :language,
 :page_type, :validation_source, :remark, :extracted_at)"""

def mk_engine(path, job):
    e = OnPageAuditEngine(db_path=path, job_id=job, primary_domain="")
    e.conn = e._connect_db()
    return e

def bench_counters(n_sources=124474):
    d = tempfile.mkdtemp(); path = os.path.join(d, "c.db"); job = "J"
    e = mk_engine(path, job)
    e.initialize_inventory([{"original_url": f"https://www.x.in/p/{i}"} for i in range(n_sources)])
    checkpoints = [250_000, 1_000_000, 2_000_000, 3_000_000, 4_333_000]
    done = 0; i = 0; out = []
    t_ins = time.time()
    while checkpoints:
        rows = []
        while len(rows) < 50_000:
            rows.extend(rel_rows(job, i, n_sources)); i += 1
        with e.conn: e.conn.executemany(INS, rows)
        e.conn.execute("UPDATE on_page_source_pages SET crawl_state='COMPLETED', raw_occurrences_count=36 WHERE job_id=? AND source_page_id<=?", (job, f"SRC_{i:06d}"))
        e.conn.commit()
        done += len(rows)
        if done >= checkpoints[0]:
            checkpoints.pop(0)
            ts = []
            for _ in range(3):
                t = time.perf_counter(); e.sync_job_counters("RUNNING"); ts.append(time.perf_counter() - t)
            out.append((done, statistics.median(ts)))
            print(f"  rels={done:>9,}  sync_job_counters() = {statistics.median(ts)*1000:8.1f} ms/call", flush=True)
    print(f"  (bulk build {time.time()-t_ins:.0f}s, db {os.path.getsize(path)/1e9:.2f} GB)")
    e.close()
    return out, path

def bench_batches(drop=()):
    d = tempfile.mkdtemp(); path = os.path.join(d, "b.db"); job = "J"
    e = mk_engine(path, job)
    for ix in drop: e.conn.execute(f"DROP INDEX IF EXISTS {ix}")
    res = {}
    n_src = 125000
    src_i = 0
    # pre-fill 1M rows so index depth is realistic
    rows = []
    for _ in range(28000): rows.extend(rel_rows(job, src_i, n_src)); src_i += 1
    with e.conn: e.conn.executemany(INS, rows)
    for bs in (100, 250, 500, 1000):
        lat = []; n_rows = 0; t0 = time.perf_counter()
        for _ in range(max(2, 3000 // bs)):
            rows = []
            for _ in range(bs): rows.extend(rel_rows(job, src_i, n_src)); src_i += 1
            t = time.perf_counter()
            with e.conn: e.conn.executemany(INS, rows)
            lat.append(time.perf_counter() - t); n_rows += len(rows)
        tot = sum(lat)
        res[bs] = (n_rows / tot, statistics.median(lat) * 1000, max(lat) * 1000)
    e.close()
    return res

if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "counters"):
        print("1) hot-path sync_job_counters() cost vs relationship table size")
        bench_counters()
    if what in ("all", "batches"):
        print("2) relationship write throughput per batch size (35 rels/source page, 1M rows pre-filled)")
        for bs, (rps, med, mx) in bench_batches().items():
            print(f"  batch={bs:5d} pages ({bs*35:6d} rows): {rps:9,.0f} rows/s | txn median {med:7.1f} ms max {mx:7.1f} ms")
    if what in ("all", "indexes"):
        print("3) index write cost (batch=250 pages)")
        for label, drop in [("all current indexes", ()), ("- idx_oplr_job (dup of UNIQUE prefix)", ("idx_oplr_job",)),
                            ("- idx_oplr_job, idx_oplr_valsrc, idx_oplr_scope", ("idx_oplr_job", "idx_oplr_valsrc", "idx_oplr_scope"))]:
            r = bench_batches(drop)[250]
            print(f"  {label:50s} {r[0]:9,.0f} rows/s | txn median {r[1]:7.1f} ms")
