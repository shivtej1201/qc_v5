import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/benchmarks)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import asyncio
import os
import psutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qcv5.core.db import Database
from qcv5.crawler.link_validator import LinkValidator

MOCK_PORT = 8995
NUM_URLS = 100

class BenchmarkMockServer:
    def __init__(self, host: str = "127.0.0.1", port: int = MOCK_PORT):
        self.host = host
        self.port = port
        self.server = None
        self.thread = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                # Simulate realistic remote server latency (30ms)
                time.sleep(0.03)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Test Page</h1></body></html>")

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

async def run_concurrency_benchmark():
    print("=" * 80)
    print(f"PHASE 10.9.2: BOUNDED WORKER CONCURRENCY BENCHMARK ({NUM_URLS} UNIQUE DESTINATIONS)")
    print("Testing concurrency levels: 1, 5, 10, 20, 50")
    print("=" * 80)

    server = BenchmarkMockServer()
    server.start()

    process = psutil.Process(os.getpid())
    db = Database("benchmark_phase_10_9_2.db")
    await db.connect()

    concurrency_levels = [1, 5, 10, 20, 50]
    benchmark_results = []

    try:
        for c in concurrency_levels:
            job_id = f"bench_job_{c}_{uuid.uuid4().hex[:6]}"
            urls = [f"http://127.0.0.1:{MOCK_PORT}/dest-{i}" for i in range(NUM_URLS)]

            rows = [(job_id, f"https://example.com/source-{i}", u, u) for i, u in enumerate(urls)]
            await db.conn.executemany(
                "INSERT INTO extracted_links (job_id, source_url, original_url, normalized_url) VALUES (?, ?, ?, ?)",
                rows
            )
            await db.conn.commit()

            mem_before_mb = process.memory_info().rss / (1024 * 1024)
            validator = LinkValidator(db=db, concurrency=c, per_host_concurrency=c, allow_local=True)
            
            t0 = time.perf_counter()
            validated_results = await validator.validate_job_destinations(job_id, force_recheck=True)
            t1 = time.perf_counter()
            await validator.aclose()
            
            mem_after_mb = process.memory_info().rss / (1024 * 1024)
            elapsed_sec = t1 - t0
            urls_per_sec = NUM_URLS / elapsed_sec if elapsed_sec > 0 else 0
            speedup = benchmark_results[0]["elapsed_sec"] / elapsed_sec if benchmark_results else 1.0

            result_entry = {
                "concurrency": c,
                "elapsed_sec": round(elapsed_sec, 3),
                "urls_per_sec": round(urls_per_sec, 2),
                "speedup": round(speedup, 2),
                "mem_delta_mb": round(mem_after_mb - mem_before_mb, 2),
                "success_count": len(validated_results)
            }
            benchmark_results.append(result_entry)

            print(f"Concurrency {c:2d} | Elapsed: {elapsed_sec:6.2f}s | Throughput: {urls_per_sec:6.1f} URLs/s | Speedup: {speedup:4.2f}x | Mem Delta: {result_entry['mem_delta_mb']:5.2f} MB")

    finally:
        server.stop()
        await db.close()
        if os.path.exists("benchmark_phase_10_9_2.db"):
            try: os.remove("benchmark_phase_10_9_2.db")
            except OSError: pass

    print("=" * 80)
    print("BENCHMARK SUMMARY TABLE")
    print(f"{'Concurrency':<12} | {'Elapsed (s)':<12} | {'Throughput (URLs/s)':<22} | {'Speedup':<10} | {'Status':<10}")
    print("-" * 75)
    for r in benchmark_results:
        print(f"{r['concurrency']:<12} | {r['elapsed_sec']:<12} | {r['urls_per_sec']:<22} | {r['speedup']:<8}x | PASS")
    print("=" * 80)

    # Verify that concurrency 10 is significantly faster than concurrency 1
    assert benchmark_results[2]["elapsed_sec"] < benchmark_results[0]["elapsed_sec"] / 3, "Concurrency 10 should be at least 3x faster than concurrency 1"
    print("All benchmark thresholds successfully met!")

if __name__ == "__main__":
    asyncio.run(run_concurrency_benchmark())
