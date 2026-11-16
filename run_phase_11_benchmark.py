import os
import sys
import time
import json
import asyncio
import tracemalloc
from datetime import datetime
import openpyxl
import pandas as pd
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route
import uvicorn

from config import settings
from db import Database
from browser import BrowserPool
from api_observer import ApiResponseObserver
from report_generator import ReportGenerator

# Mock server handlers
async def handle_good_page(request):
    html = """
    <html><body>
        <h1>Catalog Item</h1>
        <div id="content">Item loaded</div>
        <script>fetch('/api/products').then(r => r.json());</script>
    </body></html>
    """
    return HTMLResponse(html)

async def handle_good_api(request):
    return JSONResponse({"status": "ok", "items": [{"id": 1, "name": "Item A"}]})

async def handle_empty_page(request):
    html = """
    <html><body>
        <h1>Search Page</h1>
        <div id="status">No items found</div>
        <script>fetch('/api/empty-search').then(r => r.json());</script>
    </body></html>
    """
    return HTMLResponse(html)

async def handle_empty_api(request):
    return JSONResponse({"status": "success", "total": 0, "items": []})

async def handle_api_error_page(request):
    html = """
    <html><body>
        <div id="container"></div>
        <script>fetch('/api/error').catch(() => {});</script>
    </body></html>
    """
    return HTMLResponse(html)

async def handle_error_api(request):
    return Response(content="Database failure", status_code=500)

async def handle_maintenance_page(request):
    return HTMLResponse("<html><body><h1>Under Maintenance</h1></body></html>", status_code=200)

async def handle_delayed_page(request):
    html = """
    <html><body>
        <h1>Delayed Item</h1>
        <div id="status">Loading</div>
        <script>
            setTimeout(() => { fetch('/api/delayed').then(r => r.json()); }, 500);
        </script>
    </body></html>
    """
    return HTMLResponse(html)

async def handle_delayed_api(request):
    await asyncio.sleep(0.3)
    return JSONResponse({"items": [{"id": 99, "name": "Delayed Widget"}]})

async def run_benchmark(num_urls: int = 100):
    print("=" * 65)
    print(f"STARTING PHASE 11.1 BENCHMARK RUN: {num_urls} URLs")
    print("=" * 65)

    # 1. Start Mock Server
    app = Starlette(routes=[
        Route("/page/good", handle_good_page),
        Route("/api/products", handle_good_api),
        Route("/page/empty", handle_empty_page),
        Route("/api/empty-search", handle_empty_api),
        Route("/page/api-error", handle_api_error_page),
        Route("/api/error", handle_error_api),
        Route("/page/maint", handle_maintenance_page),
        Route("/page/delayed", handle_delayed_page),
        Route("/api/delayed", handle_delayed_api),
    ])

    config = uvicorn.Config(app, host="127.0.0.1", port=8998, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)
    base_url = "http://127.0.0.1:8998"

    # 2. Build repeatable dataset with known ground truth
    dataset = []
    # Distribution across 100 URLs:
    # 35 Good (FOUND)
    # 20 Empty (NOT_FOUND)
    # 15 HTTP 404 (NOT_FOUND)
    # 10 API Error (API_ERROR)
    # 10 Maintenance (PAGE_ERROR)
    # 5 Delayed (FOUND)
    # 5 SSRF Blocked (SKIPPED)
    for i in range(num_urls):
        mod = i % 20
        if mod < 7:
            dataset.append({"url": f"{base_url}/page/good?id={i}", "expected": "FOUND"})
        elif mod < 11:
            dataset.append({"url": f"{base_url}/page/empty?id={i}", "expected": "NOT_FOUND"})
        elif mod < 14:
            dataset.append({"url": f"{base_url}/page/non-existent-404-{i}", "expected": "NOT_FOUND"})
        elif mod < 16:
            dataset.append({"url": f"{base_url}/page/api-error?id={i}", "expected": "API_ERROR"})
        elif mod < 18:
            dataset.append({"url": f"{base_url}/page/maint?id={i}", "expected": "PAGE_ERROR"})
        elif mod < 19:
            dataset.append({"url": f"{base_url}/page/delayed?id={i}", "expected": "FOUND"})
        else:
            dataset.append({"url": f"http://169.254.169.254/latest/meta-data/{i}", "expected": "SKIPPED"})

    test_db_file = f"benchmark_p11_{int(time.time())}.db"
    db = Database(test_db_file)
    await db.connect()
    bp = BrowserPool()
    await bp.start()
    observer = ApiResponseObserver(bp)
    job_id = f"bench-job-{int(time.time())}"

    # Start Memory & Performance Profiling
    tracemalloc.start()
    t_start = time.time()
    db_write_times = []
    lock_errors = 0

    sem = asyncio.Semaphore(settings.BROWSER_CONCURRENCY)
    actual_results = {}
    total_apis_observed = 0

    async def audit_worker(item):
        nonlocal total_apis_observed, lock_errors
        url = item["url"]
        async with sem:
            p_res, apis = await observer.audit_page(job_id, url, allow_local=True)
            actual_results[url] = p_res.page_status
            total_apis_observed += len(apis)

            t_write_s = time.time()
            try:
                await db.batch_insert_page_audit_results([p_res.to_db_dict()])
                if apis:
                    await db.batch_insert_api_responses([a.to_db_dict() for a in apis])
                db_write_times.append(time.time() - t_write_s)
            except Exception as e:
                if "locked" in str(e).lower():
                    lock_errors += 1

    tasks = [asyncio.create_task(audit_worker(item)) for item in dataset]
    await asyncio.gather(*tasks)

    t_end = time.time()
    total_duration = t_end - t_start
    pages_per_min = round((num_urls / total_duration) * 60, 2)
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Measure outcome accuracy against ground truth
    correct_count = sum(1 for item in dataset if actual_results.get(item["url"]) == item["expected"])
    accuracy_pct = round((correct_count / num_urls) * 100, 2)

    # Measure screenshots & disk usage
    screenshots_dir = os.path.join(settings.ARTIFACTS_DIR, job_id, "screenshots")
    screenshot_files = os.listdir(screenshots_dir) if os.path.exists(screenshots_dir) else []
    total_disk_bytes = sum(os.path.getsize(os.path.join(screenshots_dir, f)) for f in screenshot_files)

    # Measure report generation duration
    report_file = f"reports/benchmark_report_{job_id}.xlsx"
    rg = ReportGenerator(db)
    t_rep_s = time.time()
    await rg.generate_api_audit_report(job_id, report_file)
    report_gen_duration_s = round(time.time() - t_rep_s, 3)

    # Retrieve stats from DB
    stats = await db.get_api_audit_stats(job_id)

    # Clean up
    await bp.stop()
    await db.close()
    server.should_exit = True
    await server_task

    for f in [test_db_file, f"{test_db_file}-wal", f"{test_db_file}-shm"]:
        try:
            if os.path.exists(f): os.remove(f)
        except: pass

    # Print Formatted Benchmark Table
    print("\n" + "=" * 65)
    print("PHASE 11.1 BENCHMARK RESULTS SUMMARY (100 URLs)")
    print("=" * 65)
    print(f"Total Pages Processed:        {num_urls}")
    print(f"Total Runtime:                {total_duration:.2f} seconds")
    print(f"Throughput:                   {pages_per_min:.2f} pages / minute")
    print(f"Browser Concurrency:          {settings.BROWSER_CONCURRENCY} workers")
    print(f"Peak Memory Usage:            {peak_mem / (1024 * 1024):.2f} MB")
    print(f"API Requests Observed:        {total_apis_observed} requests")
    print(f"Outcome Accuracy:             {accuracy_pct}% ({correct_count}/{num_urls} correct)")
    print(f"Screenshots Captured:         {len(screenshot_files)} images ({total_disk_bytes / 1024:.2f} KB)")
    print(f"Avg DB Write Latency:         {sum(db_write_times) / len(db_write_times) * 1000:.2f} ms")
    print(f"DB Lock Errors:               {lock_errors}")
    print(f"Report Generation Duration:   {report_gen_duration_s:.3f} seconds")
    print("-" * 65)
    print("Outcome Breakdown:")
    print(f"  FOUND:        {stats.get('found', 0)}")
    print(f"  NOT_FOUND:    {stats.get('not_found', 0)}")
    print(f"  API_ERROR:    {stats.get('api_error', 0)}")
    print(f"  PAGE_ERROR:   {stats.get('page_error', 0)}")
    print(f"  TIMEOUT:      {stats.get('timeout', 0)}")
    print(f"  SKIPPED:      {stats.get('skipped', 0)}")
    print(f"  INCONCLUSIVE: {stats.get('inconclusive', 0)}")
    print("=" * 65)

if __name__ == "__main__":
    asyncio.run(run_benchmark(100))
