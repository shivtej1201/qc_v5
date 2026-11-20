import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/benchmarks)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import asyncio
import os
import sys
import time
import json
import statistics
import psutil
from benchmark_500k import BenchmarkRunner

async def main():
    concurrency_levels = [5, 10, 20, 30, 40, 50, 75, 100]
    test_urls = 5000 # 5,000 URLs per concurrency level
    results = []

    print("=" * 85)
    print("CONCURRENCY MATRIX BENCHMARK (5, 10, 20, 30, 40, 50, 75, 100)")
    print("=" * 85)

    for c in concurrency_levels:
        db_path = f"bench_conc_{c}.sqlite"
        runner = BenchmarkRunner(target_urls=test_urls, db_path=db_path, concurrency=c)
        res = await runner.run()
        results.append(res)
        for ext in ["", "-wal", "-shm"]:
            fpath = f"{db_path}{ext}"
            if os.path.exists(fpath):
                try: os.remove(fpath)
                except: pass

        print(f"Concurrency {c:>3} -> Throughput: {res['urls_per_sec']:>7.1f} u/s | P50: {res['p50_latency_ms']:>5.2f} ms | P95: {res['p95_latency_ms']:>5.2f} ms | Mem: {res['peak_memory_mb']:>5.1f} MB | CPU: {res['cpu_usage_pct']:>4.1f}%")

    # Output markdown table
    print("\n### CONCURRENCY SELECTION MATRIX")
    headers = ["Concurrency", "Throughput (URLs/s)", "Duration (s)", "P50 (ms)", "P95 (ms)", "Peak Mem (MB)", "CPU (%)", "DB Write (s)"]
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join(["---"] * len(headers)) + "|")
    for r in results:
        print(f"| {r['concurrency']} | {r['urls_per_sec']:.1f} | {r['total_duration_s']:.2f} | {r['p50_latency_ms']:.2f} | {r['p95_latency_ms']:.2f} | {r['peak_memory_mb']:.1f} | {r['cpu_usage_pct']:.1f}% | {r['database_write_time_s']:.2f} |")

    with open("docs/performance/concurrency_matrix.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
