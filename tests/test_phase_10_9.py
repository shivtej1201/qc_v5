import asyncio
import os
import sys
import time
import uuid
import json
import threading
import openpyxl
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from typing import Dict, List, Any

from qcv5.core.db import Database
from qcv5.core.config import settings
from qcv5.crawler.link_validator import LinkValidator
from qcv5.crawler.url_normalizer import URLNormalizer
from qcv5.services.job_manager import JobManager
from qcv5.reports.report_generator import ReportGenerator

# Test HTTP Mock Server with various behaviors
class Phase109MockServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8996):
        self.host = host
        self.port = port
        self.active_per_host_counts = 0
        self.peak_per_host = 0
        self.requests_log = []
        self.retry_counts = {}
        self.lock = threading.Lock()

        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                with parent.lock:
                    parent.active_per_host_counts += 1
                    if parent.active_per_host_counts > parent.peak_per_host:
                        parent.peak_per_host = parent.active_per_host_counts
                    parent.requests_log.append({"path": self.path, "time": time.time(), "headers": dict(self.headers)})

                try:
                    p = self.path
                    if p.startswith("/ok"):
                        time.sleep(0.03)
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.end_headers()
                        self.wfile.write(b"OK")
                    elif p.startswith("/redirect_to_ok"):
                        target = f"http://{parent.host}:{parent.port}/ok_target"
                        self.send_response(301)
                        self.send_header("Location", target)
                        self.end_headers()
                    elif p.startswith("/ok_target"):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"OK Target")
                    elif p.startswith("/redirect_to_ssrf"):
                        # Target is a prohibited loopback IP / AWS metadata address
                        target = "http://169.254.169.254/latest/meta-data/"
                        self.send_response(302)
                        self.send_header("Location", target)
                        self.end_headers()
                    elif p.startswith("/rate_limit_retry"):
                        with parent.lock:
                            count = parent.retry_counts.get(p, 0)
                            parent.retry_counts[p] = count + 1
                        if count == 0:
                            self.send_response(429)
                            self.send_header("Retry-After", "1")
                            self.end_headers()
                            self.wfile.write(b"Rate Limited")
                        else:
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(b"Rate Limit Cleared")
                    elif p.startswith("/transient_503"):
                        with parent.lock:
                            count = parent.retry_counts.get(p, 0)
                            parent.retry_counts[p] = count + 1
                        if count == 0:
                            self.send_response(503)
                            self.end_headers()
                            self.wfile.write(b"Service Unavailable")
                        else:
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(b"Service Recovered")
                    elif p.startswith("/perm_404"):
                        with parent.lock:
                            parent.retry_counts[p] = parent.retry_counts.get(p, 0) + 1
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(b"Not Found")
                    elif p.startswith("/perm_500"):
                        with parent.lock:
                            parent.retry_counts[p] = parent.retry_counts.get(p, 0) + 1
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(b"Internal Error")
                    elif p.startswith("/slow_backing_off"):
                        time.sleep(0.3)
                        self.send_response(503)
                        self.end_headers()
                        self.wfile.write(b"Slow")
                    elif p.startswith("/fast_ok"):
                        time.sleep(0.01)
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"Fast OK")
                    else:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"Default")
                finally:
                    with parent.lock:
                        parent.active_per_host_counts = max(0, parent.active_per_host_counts - 1)

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def reset_metrics(self):
        with self.lock:
            self.active_per_host_counts = 0
            self.peak_per_host = 0
            self.requests_log.clear()
            self.retry_counts.clear()

async def run_all_tests():
    print("=" * 80)
    print("PHASE 10.9 AUTOMATED TEST SUITE: PARALLEL LINK VALIDATOR")
    print("=" * 80)

    server = Phase109MockServer(host="127.0.0.1", port=8996)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    db_file = f"test_p109_{uuid.uuid4().hex[:6]}.sqlite"
    db = Database(db_file)
    await db.connect()

    passed = 0
    failed = 0

    async def execute_test(tc_name: str, coro):
        nonlocal passed, failed
        try:
            print(f"[RUNNING] {tc_name}...", end=" ", flush=True)
            await coro
            print("--> PASS")
            passed += 1
        except Exception as e:
            print(f"--> FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    try:
        # TC-1: Concurrency 1 vs Concurrency 10 Speedup
        async def tc01():
            urls = [f"{base_url}/ok?id={i}" for i in range(20)]
            # Run concurrency 1
            v1 = LinkValidator(db=db, concurrency=1, per_host_concurrency=1, allow_local=True)
            t0 = time.time()
            for u in urls:
                await v1.check_destination(u)
            t1_duration = max(0.001, time.time() - t0)
            await v1.aclose()

            # Run concurrency 10
            v10 = LinkValidator(db=db, concurrency=10, per_host_concurrency=10, allow_local=True)
            t0 = time.time()
            tasks = [v10.check_destination(u) for u in urls]
            await asyncio.gather(*tasks)
            t10_duration = max(0.001, time.time() - t0)
            await v10.aclose()

            assert t10_duration < t1_duration, f"Concurrency 10 ({t10_duration:.2f}s) was not faster than Concurrency 1 ({t1_duration:.2f}s)"
            speedup = t1_duration / t10_duration
            assert speedup >= 1.2, f"Expected noticeable speedup (>= 1.2x), got {speedup:.2f}x"
        await execute_test("TC-1: Concurrency 1 vs Concurrency 10 Speedup", tc01())

        # TC-2: Strict Concurrency Bounding (clamped to 1..32)
        async def tc02():
            v_low = LinkValidator(db=db, concurrency=0)
            assert v_low.concurrency == 1, f"Expected 0 to be clamped to 1, got {v_low.concurrency}"
            await v_low.aclose()

            v_neg = LinkValidator(db=db, concurrency=-5)
            assert v_neg.concurrency == 1, f"Expected negative to be clamped to 1, got {v_neg.concurrency}"
            await v_neg.aclose()

            v_high = LinkValidator(db=db, concurrency=100)
            assert v_high.concurrency in (32, 50), f"Expected 100 to be clamped to bounded max (32/50), got {v_high.concurrency}"
            await v_high.aclose()

            v_normal = LinkValidator(db=db, concurrency=16)
            assert v_normal.concurrency == 16, f"Expected 16 to be maintained, got {v_normal.concurrency}"
            await v_normal.aclose()
        await execute_test("TC-2: Strict Concurrency Bounding (1..50)", tc02())

        # TC-3: Per-Host Concurrency Limiting
        async def tc03():
            server.reset_metrics()
            # Validator concurrency = 10, but per_host_concurrency = 3
            v = LinkValidator(db=db, concurrency=10, per_host_concurrency=3, allow_local=True)
            urls = [f"{base_url}/ok?seq={i}" for i in range(12)]
            tasks = [v.check_destination(u) for u in urls]
            await asyncio.gather(*tasks)
            await v.aclose()

            assert server.peak_per_host <= 3, f"Peak per-host requests was {server.peak_per_host}, expected <= 3"
        await execute_test("TC-3: Per-Host Concurrency Limiting", tc03())

        # TC-4: Hop-by-Hop SSRF Validation on 301/302 Redirects
        async def tc04():
            # In production, allow_local=False blocks private/loopback and metadata endpoints
            v = LinkValidator(db=db, concurrency=5, allow_local=False)
            res = await v.check_destination("http://169.254.169.254/latest/meta-data/")
            assert res["status"] == "Blocked (SSRF)"
            assert res["check_method"] == "SSRF_FILTER"
            await v.aclose()

            # Now test redirect hop to SSRF destination
            v_test = LinkValidator(db=db, concurrency=5, allow_local=True)
            # The initial URL is allowed by test server, but the redirect target is 169.254.169.254
            res_redir = await v_test.check_destination(f"{base_url}/redirect_to_ssrf")
            assert res_redir["status"] == "Blocked (SSRF)", f"Expected Blocked (SSRF), got {res_redir['status']}"
            assert "169.254.169.254" in res_redir["error_message"]
            await v_test.aclose()
        await execute_test("TC-4: Hop-by-Hop SSRF Validation on Redirects", tc04())

        # TC-5: Transient Retry on 429 Respecting Retry-After
        async def tc05():
            server.reset_metrics()
            v = LinkValidator(db=db, concurrency=5, allow_local=True)
            url = f"{base_url}/rate_limit_retry_1"
            res = await v.check_destination(url)
            await v.aclose()

            assert res["final_status_code"] == 200, f"Expected final 200 after retry, got {res['final_status_code']}"
            assert res["retries_count"] == 1, f"Expected 1 retry, got {res['retries_count']}"
            assert server.retry_counts.get("/rate_limit_retry_1") == 2
        await execute_test("TC-5: Transient Retry on 429 with Retry-After", tc05())

        # TC-6: Transient Retry on 503 / 502 / 504 / Network Timeout
        async def tc06():
            server.reset_metrics()
            v = LinkValidator(db=db, concurrency=5, allow_local=True)
            url = f"{base_url}/transient_503_1"
            res = await v.check_destination(url)
            await v.aclose()

            assert res["final_status_code"] == 200, f"Expected final 200 after retry, got {res['final_status_code']}"
            assert res["retries_count"] == 1, f"Expected 1 retry, got {res['retries_count']}"
            assert server.retry_counts.get("/transient_503_1") == 2
        await execute_test("TC-6: Transient Retry on 503 Recovery", tc06())

        # TC-7: No Retries on Permanent Errors (404, 500, SSRF)
        async def tc07():
            server.reset_metrics()
            v = LinkValidator(db=db, concurrency=5, allow_local=True)
            res404 = await v.check_destination(f"{base_url}/perm_404")
            res500 = await v.check_destination(f"{base_url}/perm_500")
            await v.aclose()

            assert res404["final_status_code"] == 404
            assert res404["retries_count"] == 0, f"Expected 0 retries on 404, got {res404['retries_count']}"
            assert res500["final_status_code"] == 500
            assert res500["retries_count"] == 0, f"Expected 0 retries on 500, got {res500['retries_count']}"
            assert server.retry_counts.get("/perm_404") == 1
            assert server.retry_counts.get("/perm_500") == 1
        await execute_test("TC-7: No Retries on Permanent 404 / 500", tc07())

        # TC-8: Non-Blocking Backoff Across Hosts
        async def tc08():
            server.reset_metrics()
            v = LinkValidator(db=db, concurrency=5, per_host_concurrency=2, allow_local=True)
            # When one request is delayed by backoff, another fast request should finish immediately
            t0 = time.time()
            t_slow = asyncio.create_task(v.check_destination(f"{base_url}/slow_backing_off"))
            await asyncio.sleep(0.02)
            t_fast = asyncio.create_task(v.check_destination(f"{base_url}/fast_ok"))
            
            # Fast should finish before slow
            res_fast = await t_fast
            t_fast_finish = time.time() - t0
            res_slow = await t_slow
            t_slow_finish = time.time() - t0
            await v.aclose()

            assert res_fast["final_status_code"] == 200
            assert t_fast_finish < 0.2, f"Fast request took too long ({t_fast_finish:.2f}s)"
            assert t_slow_finish >= 0.3
        await execute_test("TC-8: Non-Blocking Backoff Across Requests", tc08())

        # TC-9: Incremental Database Batch Persistence
        async def tc09():
            job_id = f"job_tc09_{uuid.uuid4().hex[:6]}"
            upload_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "test.xlsx", "link_audit", "PROCESSING", now))
            await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 40, 0, "VALIDATING", now, now))

            # Insert 40 unique extracted links
            links = []
            for i in range(40):
                links.append({
                    "job_id": job_id,
                    "batch_id": "b1",
                    "source_url": f"{base_url}/src",
                    "original_url": f"{base_url}/ok?n={i}",
                    "normalized_url": URLNormalizer.normalize(f"{base_url}/ok?n={i}"),
                    "anchor_text": f"A{i}",
                    "component_name": "Nav",
                    "image_name": "",
                    "loading_type": "",
                    "extracted_timestamp": now
                })
            sql_ext = """
                INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
                VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
            """
            await db.conn.executemany(sql_ext, links)
            await db.conn.commit()

            intermediate_observed = []
            def on_prog(st):
                intermediate_observed.append(st["completed_unique"])

            v = LinkValidator(db=db, concurrency=5, allow_local=True)
            await v.validate_job_destinations(job_id=job_id, force_recheck=True, on_progress=on_prog)
            await v.aclose()

            # Verify that database has 40 records
            dests = await db.get_job_destinations(job_id)
            assert len(dests) == 40, f"Expected 40 persisted records, got {len(dests)}"
            assert len(intermediate_observed) > 1, "Expected multiple incremental progress updates"
        await execute_test("TC-9: Incremental Database Batch Persistence", tc09())

        # TC-10: Live Metrics Reporting
        async def tc10():
            job_id = f"job_tc10_{uuid.uuid4().hex[:6]}"
            upload_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "test.xlsx", "link_audit", "PROCESSING", now))
            await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 10, 0, "VALIDATING", now, now))

            links = []
            for i in range(10):
                links.append({
                    "job_id": job_id,
                    "batch_id": "b1",
                    "source_url": f"{base_url}/src",
                    "original_url": f"{base_url}/ok?m={i}",
                    "normalized_url": URLNormalizer.normalize(f"{base_url}/ok?m={i}"),
                    "anchor_text": f"A{i}",
                    "component_name": "Nav",
                    "image_name": "",
                    "loading_type": "",
                    "extracted_timestamp": now
                })
            sql_ext = """
                INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
                VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
            """
            await db.conn.executemany(sql_ext, links)
            await db.conn.commit()

            v = LinkValidator(db=db, concurrency=4, allow_local=True)
            task = asyncio.create_task(v.validate_job_destinations(job_id=job_id, force_recheck=True))
            
            # Poll live metrics while task is in flight
            await asyncio.sleep(0.04)
            live = v.get_live_stats(job_id)
            assert live is not None, "Expected live stats to be available during execution"
            assert "in_flight_validations" in live
            assert "queued_destinations" in live
            assert "urls_per_minute" in live
            assert "current_concurrency" in live
            assert live["current_concurrency"] == 4

            await task
            await v.aclose()
        await execute_test("TC-10: Live Metrics Reporting", tc10())

        # TC-11: Progress Denominator Reflects Unique Destination Count
        async def tc11():
            job_id = f"job_tc11_{uuid.uuid4().hex[:6]}"
            upload_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            # Originally total_records was 2 (e.g. 2 source pages in sheet)
            await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "test.xlsx", "link_audit", "PROCESSING", now))
            await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 2, 0, "VALIDATING", now, now))

            # But 6 unique destinations were extracted
            links = []
            for i in range(6):
                links.append({
                    "job_id": job_id,
                    "batch_id": "b1",
                    "source_url": f"{base_url}/src_{i % 2}",
                    "original_url": f"{base_url}/ok?u={i}",
                    "normalized_url": URLNormalizer.normalize(f"{base_url}/ok?u={i}"),
                    "anchor_text": f"A{i}",
                    "component_name": "Nav",
                    "image_name": "",
                    "loading_type": "",
                    "extracted_timestamp": now
                })
            sql_ext = """
                INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
                VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
            """
            await db.conn.executemany(sql_ext, links)
            await db.conn.commit()

            v = LinkValidator(db=db, concurrency=3, allow_local=True)
            await v.validate_job_destinations(job_id=job_id, force_recheck=True)
            await v.aclose()

            # Verify that jobs table total_records was updated to 6 (unique destinations) and processed_records is 6
            async with db.conn.execute("SELECT total_records, processed_records FROM jobs WHERE id = ?", (job_id,)) as cur:
                row = await cur.fetchone()
                assert row[0] == 6, f"Expected total_records to be 6, got {row[0]}"
                assert row[1] == 6, f"Expected processed_records to be 6, got {row[1]}"
        await execute_test("TC-11: Progress Denominator Reflects Unique Destinations", tc11())

        # TC-12: Preserves Relationship Mappings in extracted_links and Report
        async def tc12():
            job_id = f"job_tc12_{uuid.uuid4().hex[:6]}"
            upload_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "test.xlsx", "link_audit", "PROCESSING", now))
            await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 1, 0, "VALIDATING", now, now))

            # 1 destination referenced by 4 different sources
            dest = f"{base_url}/ok?shared=1"
            norm = URLNormalizer.normalize(dest)
            links = []
            for i in range(4):
                links.append({
                    "job_id": job_id,
                    "batch_id": "b1",
                    "source_url": f"https://example.com/source_{i}",
                    "original_url": dest,
                    "normalized_url": norm,
                    "anchor_text": f"Anchor {i}",
                    "component_name": f"Comp {i}",
                    "image_name": "",
                    "loading_type": "",
                    "extracted_timestamp": now
                })
            sql_ext = """
                INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
                VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
            """
            await db.conn.executemany(sql_ext, links)
            await db.conn.commit()

            v = LinkValidator(db=db, concurrency=2, allow_local=True)
            await v.validate_job_destinations(job_id=job_id, force_recheck=True)
            await v.aclose()

            # Generate report and verify all 4 rows exist in detailed sheet
            report_path = f"test_report_{job_id}.xlsx"
            rg = ReportGenerator(db)
            await rg.generate_report(job_id, "link_audit", report_path)

            wb = openpyxl.load_workbook(report_path)
            assert "Link Audit" in wb.sheetnames, f"Expected 'Link Audit' in {wb.sheetnames}"
            ws = wb["Link Audit"]
            assert ws.max_row == 5, f"Expected 5 rows (1 header + 4 data), got {ws.max_row}"
            wb.close()
            if os.path.exists(report_path):
                os.remove(report_path)
        await execute_test("TC-12: Preserves All Relationship Rows in Report", tc12())

        # TC-13: Preserves Source-Page Classification
        async def tc13():
            src = "https://example.com/credit-card-offers"
            await db.update_source_classification(src, "PLP", "Credit Cards", "ATTR_LOOKUP")
            sc = await db.get_source_classification(src)
            assert sc["page_type"] == "PLP"
            assert sc["business"] == "Credit Cards"
            assert sc["classification_method"] == "ATTR_LOOKUP"
        await execute_test("TC-13: Preserves Source-Page Classification", tc13())

        # TC-14: No Playwright Browser Fallback During Destination Validation
        async def tc14():
            # URLChecker had BROWSER_FALLBACK; LinkValidator never invokes Playwright for HTTP health checks
            v = LinkValidator(db=db, concurrency=2, allow_local=True)
            res = await v.check_destination(f"{base_url}/perm_404")
            assert res["check_method"] == "HTTP"
            assert res["browser_verified"] == "NO"
            await v.aclose()
        await execute_test("TC-14: No Playwright Fallback for Link Validation", tc14())

        # TC-15: Job Pause/Cancel Resilience During Concurrent Validation
        async def tc15():
            job_id = f"job_tc15_{uuid.uuid4().hex[:6]}"
            upload_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "test.xlsx", "link_audit", "PROCESSING", now))
            await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 50, 0, "VALIDATING", now, now))

            links = []
            for i in range(50):
                links.append({
                    "job_id": job_id,
                    "batch_id": "b1",
                    "source_url": f"{base_url}/src",
                    "original_url": f"{base_url}/ok?cancel_test={i}",
                    "normalized_url": URLNormalizer.normalize(f"{base_url}/ok?cancel_test={i}"),
                    "anchor_text": f"A{i}",
                    "component_name": "Nav",
                    "image_name": "",
                    "loading_type": "",
                    "extracted_timestamp": now
                })
            sql_ext = """
                INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
                VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
            """
            await db.conn.executemany(sql_ext, links)
            await db.conn.commit()

            v = LinkValidator(db=db, concurrency=4, allow_local=True)
            task = asyncio.create_task(v.validate_job_destinations(job_id=job_id, force_recheck=True))

            # Simulate user canceling job while validating
            await asyncio.sleep(0.05)
            await db.conn.execute("UPDATE jobs SET status = 'CANCELLED' WHERE id = ?", (job_id,))
            await db.conn.commit()

            # Task should terminate promptly without throwing unhandled exceptions
            await asyncio.wait_for(task, timeout=3.0)
            await v.aclose()
        await execute_test("TC-15: Job Cancellation Resilience", tc15())

        # TC-16: Regression Safety for Image Audit and Page & API Audit
        async def tc16():
            # Check Image results table insert and fetch
            img_item = {
                "normalized_url": "https://example.com/hero.png",
                "initial_status_code": 200,
                "final_status_code": 200,
                "status": "Working",
                "image_format": "PNG",
                "image_size_kb": 124.5,
                "is_dam_asset": "YES",
                "source_platform": "DAM",
                "error_type": "",
                "error_message": "",
                "checked_at": datetime.now(timezone.utc).isoformat()
            }
            await db.batch_insert_image_results([img_item])
            all_imgs = await db.get_all_image_results()
            assert any(im["normalized_url"] == "https://example.com/hero.png" for im in all_imgs)

            # Check Page & API Audit results insert
            api_res = [{
                "job_id": "test_api_job",
                "batch_id": "batch_api",
                "page_url": "https://example.com/products",
                "final_url": "https://example.com/products",
                "page_status": "FOUND",
                "http_status": 200,
                "response_time_ms": 350.0,
                "error_message": "",
                "screenshot_path": "",
                "api_count": 5,
                "failed_api_count": 0,
                "summary_reason": "Expected product cards found",
                "evaluated_at": datetime.now(timezone.utc).isoformat()
            }]
            await db.batch_insert_page_audit_results(api_res)
            page_results = await db.get_page_audit_results("test_api_job")
            assert len(page_results) == 1
            assert page_results[0]["page_status"] == "FOUND"
        await execute_test("TC-16: Regression Safety for Image Audit and Page & API Audit", tc16())

    finally:
        server.stop()
        await db.close()
        try:
            if os.path.exists(db_file):
                os.remove(db_file)
        except Exception:
            pass

    print("\n" + "=" * 80)
    print(f"PHASE 10.9 TEST SUITE SUMMARY: {passed} PASSED, {failed} FAILED")
    print("=" * 80)
    assert failed == 0, f"{failed} test(s) failed."

if __name__ == "__main__":
    asyncio.run(run_all_tests())
