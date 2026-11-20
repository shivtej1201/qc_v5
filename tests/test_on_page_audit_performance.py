"""
QC V5 on-page audit — performance-enhancement contract tests (Section 35).

Every test runs the real OnPageAuditEngine pipeline against a real local HTTP server that
records every request (path + monotonic timestamp), so "exactly one HTTP validation" is proven
from the server side, not from engine bookkeeping.
"""
import asyncio
import csv
import http.server
import os
import shutil
import socketserver
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict

import httpx

from qcv5.crawler.on_page_audit_engine import OnPageAuditEngine
from qcv5.reports.on_page_report_packager import build_on_page_audit_package

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench"))
from sim_site import page_html  # noqa: E402

AKAMAI_403 = (
    b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1>"
    b"You don't have permission to access this server.<P>Reference #18.6092e17.1794940412.deadbeef"
    b"<P>https://errors.edgesuite.net/18.6092e17</BODY></HTML>"
)


class RecordingServer:
    """Threaded local server; `handler(path) -> (status, headers, body, delay_s)`."""

    def __init__(self, handler):
        self.handler = handler
        self.hits = Counter()
        self.times = defaultdict(list)
        self.all_times = []
        self.lock = threading.Lock()

    def start(self):
        parent = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                now = time.monotonic()
                with parent.lock:
                    parent.hits[path] += 1
                    parent.times[path].append(now)
                    if path != "/robots.txt":
                        parent.all_times.append(now)
                status, headers, body, delay = parent.handler(path)
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass

        class S(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def html_resp(body: str, status: int = 200, delay: float = 0.0):
    return status, {"Content-Type": "text/html; charset=utf-8"}, body.encode(), delay


def robots_ok():
    return 200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private\n", 0.0


def make_engine(db_path, job_id, **kw):
    params = dict(
        db_path=db_path, job_id=job_id, job_name="perf-test", allow_local=True, primary_domain="",
        source_concurrency=4, dest_concurrency=6, max_retries=0, connect_timeout=2.0, read_timeout=2.0,
        batch_size=5, flush_interval_sec=0.2,
    )
    params.update(kw)
    return OnPageAuditEngine(**params)


def sources_for(base, paths):
    return [{"original_url": f"{base}{p}", "normalized_url": f"{base}{p}", "source_sitemap": "sm", "source_sitemap_number": 1} for p in paths]


class Ctx:
    def __init__(self, handler):
        self.srv = RecordingServer(handler).start()
        self.dir = tempfile.mkdtemp(prefix="qc_v5_perf_")
        self.db = os.path.join(self.dir, "t.db")

    def close(self):
        self.srv.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def q(db, sql, *args):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


# --------------------------------------------------------------------------------------
# 1. Duplicate destination: 10 source pages -> same destination -> 1 HTTP request, 10 rels
# --------------------------------------------------------------------------------------
def test_duplicate_destination_one_http_validation_many_relationships():
    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        if path.startswith("/s/"):
            return html_resp('<html><body><a href="/product/123">P</a></body></html>')
        if path == "/product/123":
            return html_resp("<html>ok</html>", delay=0.3)  # slow -> concurrent discoveries overlap in flight
        return html_resp("nf", 404)

    ctx = Ctx(handler)
    try:
        eng = make_engine(ctx.db, "dup", source_concurrency=10)
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/s/{i}" for i in range(10)]))
        st = asyncio.run(eng.run())
        eng.close()
        assert st["status"] == "COMPLETED"
        assert ctx.srv.hits["/product/123"] == 1, ctx.srv.hits
        rows = q(ctx.db, "SELECT validation_source FROM on_page_link_relationships WHERE job_id='dup' AND normalized_found_url=?",
                 f"{ctx.srv.base}/product/123")
        assert len(rows) == 10
        assert Counter(r[0] for r in rows) == Counter({"NEW_CHECK": 1, "REUSED": 9})
        assert st["counter_reconciliation"]["passed"], st["counter_reconciliation"]
    finally:
        ctx.close()


# --------------------------------------------------------------------------------------
# 2. In-flight dedup: concurrent workers discover the same URL -> 1 request, same result
# --------------------------------------------------------------------------------------
def test_inflight_dedup_single_flight_same_result():
    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        return html_resp("<html>ok</html>", delay=0.4)

    ctx = Ctx(handler)
    try:
        eng = make_engine(ctx.db, "inflight")
        url = f"{ctx.srv.base}/product/123"

        async def go():
            async with httpx.AsyncClient(follow_redirects=False) as client:
                return await asyncio.gather(*[eng.validate_destination(client, url) for _ in range(8)])

        results = asyncio.run(go())
        assert ctx.srv.hits["/product/123"] == 1, ctx.srv.hits
        assert all(r is results[0] for r in results), "all concurrent callers must receive the same result object"
        assert results[0]["http_status"] == 200 and results[0]["validation_state"] == "VALIDATED"

        async def again():
            async with httpx.AsyncClient() as client:
                return await eng.validate_destination(client, url)

        later = asyncio.run(again())
        assert later["reused"] is True and ctx.srv.hits["/product/123"] == 1
    finally:
        ctx.close()


# --------------------------------------------------------------------------------------
# 3. Source page is also a destination -> no duplicate HTTP request (both orders)
# --------------------------------------------------------------------------------------
def test_source_page_also_destination_is_not_refetched():
    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        if path.startswith("/page-"):
            me = path
            links = "".join(f'<a href="/page-{c}">x</a>' for c in "abcde")
            return html_resp(f'<html><head><link rel="canonical" href="{me}"></head><body>{links}<a href="/other">o</a></body></html>')
        if path == "/other":
            return html_resp("<html>ok</html>")
        return html_resp("nf", 404)

    ctx = Ctx(handler)
    try:
        eng = make_engine(ctx.db, "srcdst", source_concurrency=2)
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/page-{c}" for c in "abcde"]))
        st = asyncio.run(eng.run())
        eng.close()
        assert st["status"] == "COMPLETED"
        for c in "abcde":
            assert ctx.srv.hits[f"/page-{c}"] == 1, f"/page-{c} fetched {ctx.srv.hits[f'/page-{c}']}x"
        assert ctx.srv.hits["/other"] == 1
        # destination rows for source pages exist, are VALIDATED, referenced, and came from the source fetch
        rows = q(ctx.db, "SELECT normalized_url, validation_state, http_status, referenced FROM on_page_validation_cache WHERE job_id='srcdst'")
        by = {r[0]: r for r in rows}
        for c in "abcde":
            r = by[f"{ctx.srv.base}/page-{c}"]
            assert r[1:] == ("VALIDATED", 200, 1), r
        # source-seeded results are not mirrored as destination checks into url_results
        assert eng.metrics["dest_validations"] == 1  # only /other
        assert st["unique_http_destinations"] == 6 and st["new_http_validations"] == 6
        assert st["counter_reconciliation"]["passed"], st["counter_reconciliation"]
    finally:
        ctx.close()


# --------------------------------------------------------------------------------------
# 3b. Rule 17: invalid source results (timeout / WAF) never seed destination validation
# --------------------------------------------------------------------------------------
def test_non_authoritative_source_results_do_not_seed_cache():
    state = {"slow_calls": 0, "waf_calls": 0}

    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        if path == "/hub":
            return html_resp('<html><body><a href="/slow">s</a><a href="/waf">w</a></body></html>')
        if path == "/slow":
            state["slow_calls"] += 1
            # first fetch (as a source) times out; the later destination check succeeds
            return html_resp("<html>ok</html>", delay=1.5 if state["slow_calls"] == 1 else 0)
        if path == "/waf":
            return 403, {"Content-Type": "text/html", "Server": "AkamaiGHost"}, AKAMAI_403, 0.0
        return html_resp("nf", 404)

    ctx = Ctx(handler)
    try:
        eng = make_engine(ctx.db, "rule17", source_concurrency=1, read_timeout=0.7,
                          primary_hosts=["127.0.0.1"], target_rps=50, min_rps=40, max_rps=60,
                          waf_cooldown_threshold=100)
        # /slow and /waf are sources fetched BEFORE /hub references them
        eng.initialize_inventory(sources_for(ctx.srv.base, ["/slow", "/waf", "/hub"]))
        st = asyncio.run(eng.run())
        eng.close()
        src = dict(q(ctx.db, "SELECT source_page_url, crawl_state FROM on_page_source_pages WHERE job_id='rule17'"))
        assert src[f"{ctx.srv.base}/slow"] == "TIMED_OUT"
        assert src[f"{ctx.srv.base}/waf"] == "BLOCKED"
        cache = {r[0]: r[1:] for r in q(ctx.db, "SELECT normalized_url, validation_state, status_bucket FROM on_page_validation_cache WHERE job_id='rule17'")}
        # the timed-out source did NOT become a TIMED_OUT destination: it was validated on its own -> 200
        assert cache[f"{ctx.srv.base}/slow"] == ("VALIDATED", "2xx")
        assert ctx.srv.hits["/slow"] == 2
        # the WAF-blocked source result was not reused; the destination got its own (WAF) validation
        assert cache[f"{ctx.srv.base}/waf"][1] == "WAF blocked"
        mirrored = q(ctx.db, "SELECT COUNT(*) FROM url_results WHERE normalized_url=?", f"{ctx.srv.base}/waf")[0][0]
        assert mirrored == 1, "WAF destination result must come from a destination validation, not the source fetch"
        assert ctx.srv.hits["/waf"] == 6  # 3 source attempts (2 WAF requeues) + 3 destination attempts
        assert st["counter_reconciliation"]["passed"]
    finally:
        ctx.close()


# --------------------------------------------------------------------------------------
# 4. Akamai: WAF -> cooldown -> 15% RPS reduction -> retry; shared budget, no herd
# --------------------------------------------------------------------------------------
def test_akamai_cooldown_reduces_rps_and_shared_budget_is_respected():
    state = {"waf_until": None, "lock": threading.Lock(), "waf_served": 0}

    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        with state["lock"]:
            if path == "/p/6" and state["waf_until"] is None:
                state["waf_until"] = time.monotonic() + 1.0  # edge starts blocking for 1s
            blocking = state["waf_until"] is not None and time.monotonic() < state["waf_until"]
            if blocking:
                state["waf_served"] += 1
                return 403, {"Content-Type": "text/html", "Server": "AkamaiGHost"}, AKAMAI_403, 0.0
        if path.startswith("/p/"):
            i = int(path.split("/")[-1])
            return html_resp(f'<html><body><a href="/d/{i}">d</a><a href="/d/{(i+1)%30}">d</a></body></html>')
        return html_resp("<html>ok</html>")

    ctx = Ctx(handler)
    try:
        rps = 12.0
        eng = make_engine(ctx.db, "waf", source_concurrency=6, dest_concurrency=8, primary_hosts=["127.0.0.1"],
                          target_rps=rps, min_rps=3.0, max_rps=rps, waf_cooldown_threshold=3,
                          akamai_cooldown_sec=0.5, probe_url=None, probe_interval_sec=0.2)
        eng.probe_url = f"{ctx.srv.base}/robots.txt"
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(30)]))
        st = asyncio.run(eng.run())
        eng.close()
        assert eng.cooldown_count >= 1
        assert eng.target_rps == round(rps * 0.85, 2) or eng.target_rps < rps
        assert st["status"] == "COMPLETED" and st["source_pages_completed"] == 30
        # every source and destination finally validated after the requeue policy (no lost work)
        assert st["unique_http_destinations"] == st["unique_http_destinations_validated"]
        assert state["waf_served"] > 0
        # Shared budget: source + destination requests together never exceed the target in any 1s window
        ts = sorted(ctx.srv.all_times)
        worst = max(sum(1 for t in ts if s <= t < s + 1.0) for s in ts)
        assert worst <= rps + 2, f"primary budget exceeded: {worst} req in 1s (target {rps})"
        # No thundering herd: no burst of many requests within a tiny window
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        tight = sum(1 for g in gaps if g < (1.0 / rps) * 0.5)
        assert tight <= 3, f"{tight} near-simultaneous primary requests (herd)"
        assert st["counter_reconciliation"]["passed"]
    finally:
        ctx.close()


# --------------------------------------------------------------------------------------
# 5. Restart: interrupt -> restart -> reuse cache -> no duplicate validation -> no lost rels
# --------------------------------------------------------------------------------------
def _site_handler(n_sources):
    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        if path.startswith("/p/"):
            i = int(path.split("/")[-1])
            links = "".join(f'<a href="/p/{(i * 7 + k) % n_sources}">r</a>' for k in range(1, 6))
            links += "".join(f'<a href="/d/{(i + k) % 15}">d</a>' for k in range(3))
            return html_resp(f'<html><head><link rel="canonical" href="/p/{i}"></head><body>{links}<a href="mailto:a@b.c">m</a></body></html>', delay=0.02)
        if path.startswith("/d/"):
            return html_resp("<html>ok</html>", delay=0.02)
        return html_resp("nf", 404)
    return handler


def test_restart_reuses_cache_no_duplicate_validation_no_lost_relationships():
    n = 60
    ref = Ctx(_site_handler(n))
    ctx = Ctx(_site_handler(n))
    try:
        # reference: uninterrupted run
        e0 = make_engine(ref.db, "R")
        e0.initialize_inventory(sources_for(ref.srv.base, [f"/p/{i}" for i in range(n)]))
        st0 = asyncio.run(e0.run())
        e0.close()

        # interrupted run: graceful stop after ~20 source pages
        e1 = make_engine(ctx.db, "R")
        e1.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(n)]))

        async def run_and_stop():
            t = asyncio.create_task(e1.run())
            while e1._run_sources_done < 20:
                await asyncio.sleep(0.01)
            e1.request_stop()
            return await t

        mid = asyncio.run(run_and_stop())
        e1.close()
        assert 0 < mid["source_pages_pending"] < n
        hits_before = Counter(ctx.srv.hits)

        e2 = make_engine(ctx.db, "R")
        e2.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(n)]))
        st = asyncio.run(e2.run())
        e2.close()

        assert st["status"] == "COMPLETED" and st["source_pages_pending"] == 0
        for path, c in ctx.srv.hits.items():
            if path == "/robots.txt":
                continue
            assert c == 1, f"{path} requested {c}x across interrupt+restart (before restart: {hits_before[path]})"
        assert ctx.srv.hits.keys() == ref.srv.hits.keys()
        assert st["url_relationships_persisted"] == st0["url_relationships_persisted"]
        assert st["unique_http_destinations"] == st0["unique_http_destinations"]
        assert st["new_http_validations"] == st0["new_http_validations"]
        assert st["reused_http_validations"] == st0["reused_http_validations"]
        a = set(q(ref.db, "SELECT source_page_url, normalized_found_url FROM on_page_link_relationships WHERE job_id='R'"))
        b = set(q(ctx.db, "SELECT source_page_url, normalized_found_url FROM on_page_link_relationships WHERE job_id='R'"))
        norm = lambda s, base: {(x.replace(base, ""), y.replace(base, "")) for x, y in s}
        assert norm(a, ref.srv.base) == norm(b, ctx.srv.base), "relationships lost or invented across restart"
        assert st["counter_reconciliation"]["passed"]
    finally:
        ref.close()
        ctx.close()


# --------------------------------------------------------------------------------------
# 6. Reconciliation on a large relationship dataset: CSV == DB, trust gate PASS
# --------------------------------------------------------------------------------------
def test_large_dataset_reconciliation_csv_equals_db():
    n = 700
    holder = {}

    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        if path.startswith("/p/"):
            i = int(path.split("/")[-1])
            return html_resp(page_html(i, n, holder["base"], holder["base"] + "/x"))
        return html_resp("<html>ok</html>")

    ctx = Ctx(handler)
    holder["base"] = ctx.srv.base
    try:
        eng = make_engine(ctx.db, "BIG", source_concurrency=12, dest_concurrency=12, batch_size=100, flush_interval_sec=0.5,
                          reconcile_interval_sec=0.5)
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(n)]))
        st = asyncio.run(eng.run())
        eng.close()
        assert st["status"] == "COMPLETED"
        assert st["url_relationships_persisted"] > 20000
        assert st["counter_reconciliation"]["passed"], st["counter_reconciliation"]
        assert eng.metrics["reconciliations"] >= 1 and eng.metrics["reconciliation_mismatches"] == 0
        # each source fetched once; every other URL validated at most once
        assert all(c == 1 for p, c in ctx.srv.hits.items() if p != "/robots.txt"), [p for p, c in ctx.srv.hits.items() if c > 1][:5]
        pkg = build_on_page_audit_package(db_path=ctx.db, job_id="BIG", output_dir=ctx.dir, package_name="BIG_TEST")
        assert pkg["trust_gate_passed"] is True, pkg["trust_gate"]
        db_rels = q(ctx.db, "SELECT COUNT(*) FROM on_page_link_relationships WHERE job_id='BIG'")[0][0]
        csv_path = None
        for root, _, files in os.walk(ctx.dir):
            for f in files:
                if f == "all_on_page_url_relationships.csv":
                    csv_path = os.path.join(root, f)
        assert csv_path, "relationship CSV not produced"
        with open(csv_path, newline="", encoding="utf-8") as fh:
            csv_rows = sum(1 for _ in csv.reader(fh)) - 1
        assert csv_rows == db_rels == st["url_relationships_persisted"]
        ce = pkg["cache_evidence"]
        assert ce["invariant_http_rels_eq_new_plus_reused"] and ce["invariant_new_eq_unique_http"]
    finally:
        ctx.close()


# --------------------------------------------------------------------------------------
# 7. Security preserved in the pipeline: SSRF at every redirect hop, robots never fetched
# --------------------------------------------------------------------------------------
def test_pipeline_ssrf_every_hop_and_robots_preserved():
    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        if path == "/src":
            return html_resp('<html><body><a href="/to-metadata">a</a><a href="/private/x">b</a>'
                             '<a href="http://169.254.169.254/latest/meta-data/">c</a></body></html>')
        if path == "/to-metadata":
            return 302, {"Location": "http://169.254.169.254/latest/meta-data/iam"}, b"", 0.0
        return html_resp("nf", 404)

    ctx = Ctx(handler)
    try:
        eng = make_engine(ctx.db, "sec")
        eng.initialize_inventory(sources_for(ctx.srv.base, ["/src"]))
        st = asyncio.run(eng.run())
        eng.close()
        cache = {r[0]: r[1:] for r in q(ctx.db, "SELECT normalized_url, status_classification, status_bucket FROM on_page_validation_cache WHERE job_id='sec'")}
        assert cache[f"{ctx.srv.base}/to-metadata"][0] == "Blocked (SSRF)"
        assert cache["http://169.254.169.254/latest/meta-data/"][0] == "Blocked (SSRF)"
        assert cache[f"{ctx.srv.base}/private/x"] == ("ROBOTS_BLOCKED", "Robots blocked")
        assert ctx.srv.hits["/private/x"] == 0 and ctx.srv.hits["/robots.txt"] == 1
        assert st["status"] == "COMPLETED" and st["counter_reconciliation"]["passed"]
    finally:
        ctx.close()


def test_writer_failure_aborts_run_instead_of_hanging():
    def handler(path):
        if path == "/robots.txt":
            return robots_ok()
        return html_resp('<html><body><a href="/d/1">d</a></body></html>')

    ctx = Ctx(handler)
    try:
        eng = make_engine(ctx.db, "wfail")
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/s/{i}" for i in range(30)]))

        def boom(*a, **k):
            raise sqlite3.OperationalError("disk I/O error (simulated)")

        eng._flush_pipeline_sync = boom
        t = time.time()
        try:
            asyncio.run(asyncio.wait_for(eng.run(), 30))
            raise AssertionError("run should have failed")
        except RuntimeError as e:
            assert "writer failed" in str(e)
        assert time.time() - t < 20
        eng.close()
        # nothing half-written: every source page is still PENDING and resumable
        assert q(ctx.db, "SELECT COUNT(*) FROM on_page_source_pages WHERE job_id='wfail' AND crawl_state='PENDING'")[0][0] == 30
    finally:
        ctx.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t = time.time()
            fn()
            print(f"PASS {name} ({time.time() - t:.1f}s)")
