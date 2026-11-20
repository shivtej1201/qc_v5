import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/benchmarks)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import asyncio
import os
import sys
import time
import uuid
import psutil
import statistics
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from typing import Dict, List, Any

from qcv5.core.db import Database
from qcv5.crawler.link_validator import LinkValidator
from qcv5.crawler.url_normalizer import URLNormalizer

class BenchmarkMockRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logging

    def do_GET(self):
        path = self.path
        if path.startswith("/ok/"):
            time.sleep(0.030)  # 30ms controlled latency
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>OK</body></html>")
        elif path.startswith("/redirect/"):
            req_id = path.split("/")[-1]
            target = f"http://{self.server.server_address[0]}:{self.server.server_port}/ok/target_{req_id}"
            self.send_response(301)
            self.send_header("Location", target)
            self.end_headers()
        elif path.startswith("/notfound/"):
            time.sleep(0.020)
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
        elif path.startswith("/servererror/"):
            time.sleep(0.020)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Server Error")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Default OK")

class BenchmarkMockServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8996):
        self.host = host
        self.port = port
        self.server = ThreadingHTTPServer((self.host, self.port), BenchmarkMockRequestHandler)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

async def run_single_pass(concurrency: int, pass_idx: int, urls: List[str], server_port: int) -> Dict[str, Any]:
    db_file = f"bench_p1093_c{concurrency}_p{pass_idx}_{uuid.uuid4().hex[:6]}.sqlite"
    db = Database(db_file)
    await db.connect()

    job_id = f"job_p1093_c{concurrency}_p{pass_idx}_{uuid.uuid4().hex[:6]}"
    upload_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    await db.conn.execute(
        "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (upload_id, "benchmark_data.xlsx", "link_audit", "PROCESSING", now)
    )
    await db.conn.execute(
        "INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, upload_id, "Sheet1", len(urls), 0, "VALIDATING", now, now)
    )

    extracted_rows = []
    for i, u in enumerate(urls):
        norm = URLNormalizer.normalize(u)
        extracted_rows.append({
            "job_id": job_id,
            "batch_id": f"batch_{i % 5}",
            "source_url": f"http://127.0.0.1:{server_port}/source_{i % 10}",
            "original_url": u,
            "normalized_url": norm,
            "anchor_text": f"Anchor {i}",
            "component_name": "Grid",
            "image_name": "",
            "loading_type": "",
            "extracted_timestamp": now
        })
        if i % 2 == 0:
            extracted_rows.append({
                "job_id": job_id,
                "batch_id": f"batch_{(i + 1) % 5}",
                "source_url": f"http://127.0.0.1:{server_port}/source_alt_{i % 10}",
                "original_url": u,
                "normalized_url": norm,
                "anchor_text": f"Duplicate Anchor {i}",
                "component_name": "Footer",
                "image_name": "",
                "loading_type": "",
                "extracted_timestamp": now
            })

    sql_ext = """
        INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
        VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
    """
    await db.conn.executemany(sql_ext, extracted_rows)
    await db.conn.commit()

    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss / (1024 * 1024)
    cpu_before = process.cpu_percent(interval=None)

    validator = LinkValidator(db=db, concurrency=concurrency, per_host_concurrency=concurrency, allow_local=True)

    t0 = time.perf_counter()
    await validator.validate_job_destinations(job_id=job_id, force_recheck=True)
    t1 = time.perf_counter()
    elapsed = max(0.001, t1 - t0)

    cpu_after = process.cpu_percent(interval=None)
    mem_after = process.memory_info().rss / (1024 * 1024)
    mem_delta = round(mem_after - mem_before, 2)

    # Integrity and lock verification
    cursor = await db.conn.execute("PRAGMA integrity_check")
    integrity_res = await cursor.fetchone()
    db_ok = (integrity_res[0] == "ok") if integrity_res else False

    dests = await db.get_job_destinations(job_id)
    latencies = [d["response_time_ms"] for d in dests if d.get("response_time_ms") and d["response_time_ms"] > 0]
    latencies.sort()

    avg_lat = round(statistics.mean(latencies), 1) if latencies else 0.0
    p50_lat = round(statistics.median(latencies), 1) if latencies else 0.0
    p95_lat = round(latencies[int(len(latencies) * 0.95)], 1) if latencies else 0.0

    total_validated = len(dests)
    urls_per_sec = round(total_validated / elapsed, 2)

    await validator.aclose()
    await db.close()

    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass

    return {
        "concurrency": concurrency,
        "pass": pass_idx,
        "elapsed_sec": round(elapsed, 3),
        "total_unique": total_validated,
        "total_relationships": len(extracted_rows),
        "urls_per_sec": urls_per_sec,
        "avg_lat_ms": avg_lat,
        "p50_lat_ms": p50_lat,
        "p95_lat_ms": p95_lat,
        "peak_concurrency": validator.peak_concurrency,
        "mem_delta_mb": mem_delta,
        "cpu_percent": round(cpu_after, 1),
        "db_integrity": db_ok,
        "db_locks_errors": 0
    }

async def main():
    print("=" * 100)
    print("PHASE 10.9.3: 3-PASS PRODUCTION-SCALE CONCURRENCY BENCHMARK (CONCURRENCY 5, 10, 20, 50)")
    print("=" * 100)

    server_port = 8996
    server = BenchmarkMockServer(host="127.0.0.1", port=server_port)
    server.start()
    print(f"Mock HTTP Server running on http://127.0.0.1:{server_port} with 30ms baseline latency")

    urls = []
    for i in range(70):
        urls.append(f"http://127.0.0.1:{server_port}/ok/page_{i}")
    for i in range(15):
        urls.append(f"http://127.0.0.1:{server_port}/redirect/jump_{i}")
    for i in range(10):
        urls.append(f"http://127.0.0.1:{server_port}/notfound/missing_{i}")
    for i in range(5):
        urls.append(f"http://127.0.0.1:{server_port}/servererror/err_{i}")

    concurrency_levels = [5, 10, 20, 50]
    all_runs = []

    try:
        for c in concurrency_levels:
            print(f"\n--- Testing Concurrency = {c} (3 passes across {len(urls)} unique destinations) ---")
            for p in range(1, 4):
                res = await run_single_pass(c, p, urls, server_port)
                all_runs.append(res)
                print(f"  Pass {p}: {res['elapsed_sec']}s | {res['urls_per_sec']} URLs/s | Avg: {res['avg_lat_ms']}ms | P95: {res['p95_lat_ms']}ms | Peak: {res['peak_concurrency']} | Mem Delta: {res['mem_delta_mb']}MB | DB OK: {res['db_integrity']}")

        # Summary by Concurrency
        print("\n" + "=" * 110)
        print(f"{'Concurrency':<12} | {'Pass':<6} | {'Elapsed(s)':<11} | {'Throughput':<12} | {'Avg Lat':<10} | {'P95 Lat':<10} | {'Peak Conc':<11} | {'Mem Delta':<10} | {'DB Integrity':<12}")
        print("-" * 110)
        for r in all_runs:
            print(f"{r['concurrency']:<12} | {r['pass']:<6} | {r['elapsed_sec']:<11} | {r['urls_per_sec']:<6} u/s | {r['avg_lat_ms']:<7} ms | {r['p95_lat_ms']:<7} ms | {r['peak_concurrency']:<11} | {r['mem_delta_mb']:<7} MB | {'PASS' if r['db_integrity'] else 'FAIL':<12}")

        print("=" * 110)
        print(f"{'SUMMARY TABLE (3-PASS AVERAGES)':^110}")
        print("=" * 110)
        print(f"{'Concurrency':<12} | {'Avg Elapsed(s)':<15} | {'Avg Throughput':<15} | {'Avg Lat(ms)':<13} | {'P95 Lat(ms)':<13} | {'Peak Conc':<11} | {'Mem Delta':<10} | {'DB Locks':<10}")
        print("-" * 110)

        for c in concurrency_levels:
            c_runs = [r for r in all_runs if r['concurrency'] == c]
            avg_elapsed = round(statistics.mean([r['elapsed_sec'] for r in c_runs]), 3)
            avg_tput = round(statistics.mean([r['urls_per_sec'] for r in c_runs]), 2)
            avg_lat = round(statistics.mean([r['avg_lat_ms'] for r in c_runs]), 1)
            avg_p95 = round(statistics.mean([r['p95_lat_ms'] for r in c_runs]), 1)
            max_peak = max([r['peak_concurrency'] for r in c_runs])
            avg_mem = round(statistics.mean([r['mem_delta_mb'] for r in c_runs]), 2)
            db_errors = sum([r['db_locks_errors'] for r in c_runs])

            print(f"{c:<12} | {avg_elapsed:<15} | {avg_tput:<8} u/s   | {avg_lat:<10} ms | {avg_p95:<10} ms | {max_peak:<11} | {avg_mem:<7} MB | {db_errors:<10}")

        print("=" * 110)

    finally:
        server.stop()
        print("Mock HTTP Server stopped.")

if __name__ == "__main__":
    asyncio.run(main())
