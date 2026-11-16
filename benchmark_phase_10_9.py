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

from db import Database
from link_validator import LinkValidator
from url_normalizer import URLNormalizer

class MockRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # Suppress server logging during benchmark

    def do_GET(self):
        path = self.path
        if path.startswith("/ok/"):
            time.sleep(0.04) # 40ms simulated latency
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
            time.sleep(0.03)
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
        elif path.startswith("/servererror/"):
            time.sleep(0.03)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Server Error")
        elif path.startswith("/rate_limit/"):
            retry = self.headers.get("X-Retry-Attempt")
            if not retry:
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.end_headers()
                self.wfile.write(b"Too Many Requests")
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Cleared")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Default OK")

class BenchmarkMockServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8995):
        self.host = host
        self.port = port
        self.server = ThreadingHTTPServer((self.host, self.port), MockRequestHandler)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

async def run_single_benchmark(concurrency: int, urls: List[str], server_port: int) -> Dict[str, Any]:
    db_file = f"test_bench_{concurrency}_{uuid.uuid4().hex[:6]}.sqlite"
    db = Database(db_file)
    await db.connect()

    job_id = f"job_bench_{concurrency}_{uuid.uuid4().hex[:6]}"
    upload_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Insert upload and job
    await db.conn.execute(
        "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (upload_id, "benchmark_data.xlsx", "link_audit", "PROCESSING", now)
    )
    await db.conn.execute(
        "INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, upload_id, "Sheet1", len(urls), 0, "VALIDATING", now, now)
    )

    # Insert extracted links (including duplicates to test deduplication flow)
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
        # Add 1 duplicate for every even index
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

    # Track process memory before
    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss / (1024 * 1024)

    # Instantiate LinkValidator with specified concurrency and allow_local=True for mock server
    validator = LinkValidator(db=db, concurrency=concurrency, per_host_concurrency=concurrency, allow_local=True)

    t0 = time.time()
    st = await validator.validate_job_destinations(job_id=job_id, force_recheck=True)
    t1 = time.time()
    elapsed = max(0.001, t1 - t0)

    mem_after = process.memory_info().rss / (1024 * 1024)

    # Fetch recorded destination latencies from SQLite
    dests = await db.get_job_destinations(job_id)
    latencies = [d["response_time_ms"] for d in dests if d.get("response_time_ms") and d["response_time_ms"] > 0]
    latencies.sort()
    
    p50 = statistics.median(latencies) if latencies else 0.0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0

    total_validated = len(dests)
    urls_per_min = round((total_validated / elapsed) * 60.0, 1)

    await validator.aclose()
    await db.close()

    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass

    return {
        "concurrency": concurrency,
        "elapsed_sec": round(elapsed, 2),
        "total_unique": total_validated,
        "urls_per_minute": urls_per_min,
        "p50_latency_ms": round(p50, 1),
        "p95_latency_ms": round(p95, 1),
        "peak_in_flight": validator.peak_concurrency,
        "mem_usage_mb": round(mem_after, 1),
        "sqlite_busy_retries": 0
    }

async def main():
    print("=" * 80)
    print("PHASE 10.9 BENCHMARK: CONCURRENT LINK VALIDATION SPEED & LATENCY")
    print("=" * 80)

    server_port = 8995
    server = BenchmarkMockServer(host="127.0.0.1", port=server_port)
    server.start()
    print(f"Mock HTTP Fixture running on http://127.0.0.1:{server_port}")

    # Generate 100 unique destination URLs
    # 70 ok, 15 redirect -> ok, 10 notfound (404), 5 servererror (500)
    urls = []
    for i in range(70):
        urls.append(f"http://127.0.0.1:{server_port}/ok/page_{i}")
    for i in range(15):
        urls.append(f"http://127.0.0.1:{server_port}/redirect/jump_{i}")
    for i in range(10):
        urls.append(f"http://127.0.0.1:{server_port}/notfound/missing_{i}")
    for i in range(5):
        urls.append(f"http://127.0.0.1:{server_port}/servererror/err_{i}")

    concurrency_levels = [1, 5, 10, 20]
    results = []

    try:
        baseline_time = None
        for c in concurrency_levels:
            print(f"Running benchmark with Concurrency = {c} across {len(urls)} unique destinations...")
            res = await run_single_benchmark(concurrency=c, urls=urls, server_port=server_port)
            if baseline_time is None:
                baseline_time = res["elapsed_sec"]
                res["speedup"] = "1.00x (Baseline)"
            else:
                speedup_ratio = round(baseline_time / res["elapsed_sec"], 2)
                res["speedup"] = f"{speedup_ratio}x"
            results.append(res)
            print(f"  -> Concurrency {c}: {res['elapsed_sec']}s ({res['urls_per_minute']} URLs/min, Peak In-Flight: {res['peak_in_flight']}, Speedup: {res['speedup']})")

        print("\n" + "=" * 90)
        print(f"{'Concurrency':<12} | {'Time (s)':<10} | {'Speed (URLs/m)':<15} | {'Speedup':<12} | {'P50 (ms)':<10} | {'P95 (ms)':<10} | {'Peak In-Flight':<14}")
        print("-" * 90)
        for r in results:
            print(f"{r['concurrency']:<12} | {r['elapsed_sec']:<10} | {r['urls_per_minute']:<15} | {r['speedup']:<12} | {r['p50_latency_ms']:<10} | {r['p95_latency_ms']:<10} | {r['peak_in_flight']:<14}")
        print("=" * 90)

    finally:
        server.stop()
        print("Mock HTTP Fixture stopped.")

if __name__ == "__main__":
    asyncio.run(main())
