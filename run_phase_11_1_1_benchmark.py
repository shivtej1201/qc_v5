import os
import sys
import time
import asyncio
import uuid
import tracemalloc
import psutil
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from browser import BrowserPool
from api_observer import ApiResponseObserver
from db import Database
from db_metrics import db_metrics
from config import settings

# Balanced Mock Endpoints on port 8995
async def endpoint_found(request):
    idx = request.path_params.get("idx", "0")
    html = f"""<!DOCTYPE html><html><body><h1>Product {idx}</h1>
    <div id="content">Active Product Details</div>
    <script>fetch('/api/found/{idx}').then(r=>r.json());</script></body></html>"""
    return HTMLResponse(html)

async def endpoint_api_found(request):
    idx = request.path_params.get("idx", "0")
    return JSONResponse({"id": idx, "name": f"Item {idx}", "items": [{"id": 1, "sku": "SKU123"}]})

async def endpoint_not_found(request):
    idx = request.path_params.get("idx", "0")
    html = f"""<!DOCTYPE html><html><body><h1>Search Results</h1>
    <div id="res">0 results found</div>
    <script>fetch('/api/empty/{idx}').then(r=>r.json());</script></body></html>"""
    return HTMLResponse(html)

async def endpoint_api_empty(request):
    return JSONResponse({"count": 0, "results": []})

async def endpoint_api_error(request):
    idx = request.path_params.get("idx", "0")
    html = f"""<!DOCTYPE html><html><body><div id="box"></div>
    <script>fetch('/api/fatal/{idx}').catch(e=>console.error(e));</script></body></html>"""
    return HTMLResponse(html)

async def endpoint_api_fatal(request):
    return Response(content="Database connection failed", status_code=500, media_type="text/plain")

async def endpoint_page_error(request):
    return Response(content="500 Internal Server Error", status_code=500, media_type="text/plain")

routes = [
    Route("/found/{idx}", endpoint_found),
    Route("/api/found/{idx}", endpoint_api_found),
    Route("/empty/{idx}", endpoint_not_found),
    Route("/api/empty/{idx}", endpoint_api_empty),
    Route("/api-err/{idx}", endpoint_api_error),
    Route("/api/fatal/{idx}", endpoint_api_fatal),
    Route("/page-err/{idx}", endpoint_page_error),
]

mock_app = Starlette(routes=routes)

def get_process_tree_memory_mb():
    """Calculates total RSS memory of current Python process and all child Chromium processes."""
    current_proc = psutil.Process(os.getpid())
    total_rss = current_proc.memory_info().rss
    python_rss = total_rss
    chromium_rss = 0
    try:
        children = current_proc.children(recursive=True)
        for child in children:
            try:
                rss = child.memory_info().rss
                total_rss += rss
                chromium_rss += rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return round(python_rss / (1024 * 1024), 2), round(chromium_rss / (1024 * 1024), 2), round(total_rss / (1024 * 1024), 2)

async def run_benchmark():
    print("=" * 70)
    print("PHASE 11.1.1: 100-URL HIGH-FIDELITY BENCHMARK & ACCURACY MATRIX")
    print("=" * 70)

    # 1. Start Server on port 8995
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=8995, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)
    base = "http://127.0.0.1:8995"

    test_db_file = f"test_benchmark_{uuid.uuid4().hex[:8]}.db"
    db = Database(test_db_file)
    await db.connect()

    bp = BrowserPool()
    await bp.start()
    observer = ApiResponseObserver(bp)
    job_id = f"bench-job-{uuid.uuid4()}"

    # 2. Build 100-URL dataset with known ground truth
    # 25 FOUND, 25 NOT_FOUND, 25 API_ERROR, 15 PAGE_ERROR, 10 SKIPPED (SSRF)
    urls = []
    ground_truth = {}
    for i in range(25):
        u = f"{base}/found/{i}"
        urls.append(u)
        ground_truth[u] = "FOUND"
    for i in range(25):
        u = f"{base}/empty/{i}"
        urls.append(u)
        ground_truth[u] = "NOT_FOUND"
    for i in range(25):
        u = f"{base}/api-err/{i}"
        urls.append(u)
        ground_truth[u] = "API_ERROR"
    for i in range(15):
        u = f"{base}/page-err/{i}"
        urls.append(u)
        ground_truth[u] = "PAGE_ERROR"
    for i in range(10):
        # SSRF blocked URLs
        u = f"http://169.254.169.254/meta-data-{i}"
        urls.append(u)
        ground_truth[u] = "SKIPPED"

    assert len(urls) == 100, f"Expected 100 URLs, got {len(urls)}"

    tracemalloc.start()
    py_start_mb, chrom_start_mb, total_start_mb = get_process_tree_memory_mb()
    print(f"\nInitial Memory: Python={py_start_mb} MB | Chromium Tree={chrom_start_mb} MB | Total={total_start_mb} MB")

    sem = asyncio.Semaphore(settings.BROWSER_CONCURRENCY)
    results = {}
    db_metrics.reset()

    start_time = time.time()

    async def audit_worker(url):
        async with sem:
            is_local = not url.startswith("http://169.254.")
            p_res, apis = await observer.audit_page(job_id, url, "batch-bench", allow_local=is_local)
            await db.batch_insert_page_audit_results([p_res.to_db_dict()])
            if apis:
                await db.batch_insert_api_responses([a.to_db_dict() for a in apis])
            results[url] = p_res.page_status

    print(f"Executing 100 pages with concurrency {settings.BROWSER_CONCURRENCY}...")
    tasks = [asyncio.create_task(audit_worker(u)) for u in urls]
    await asyncio.gather(*tasks)

    elapsed = time.time() - start_time
    current_traced, peak_traced = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    py_end_mb, chrom_end_mb, total_end_mb = get_process_tree_memory_mb()

    # Calculate metrics
    pages_per_min = round((100 / elapsed) * 60, 2)
    categories = ["FOUND", "NOT_FOUND", "API_ERROR", "PAGE_ERROR", "SKIPPED"]
    
    # Build Confusion Matrix: True x Predicted
    confusion_matrix = {t: {p: 0 for p in categories} for t in categories}
    correct_count = 0
    for u, true_label in ground_truth.items():
        pred_label = results.get(u, "INCONCLUSIVE")
        if pred_label in confusion_matrix[true_label]:
            confusion_matrix[true_label][pred_label] += 1
        if true_label == pred_label:
            correct_count += 1

    accuracy = round((correct_count / 100.0) * 100, 2)

    print("\n" + "=" * 70)
    print("BENCHMARK RESULTS SUMMARY (100 URLs)")
    print("=" * 70)
    print(f"Total Execution Time:        {elapsed:.2f} seconds")
    print(f"Measured Throughput:         {pages_per_min:.2f} pages/min")
    print(f"Overall Accuracy:            {accuracy:.1f}% ({correct_count}/100 correct)")
    print(f"\n--- Resource Telemetry ---")
    print(f"Python Heap Peak (tracemalloc): {round(peak_traced / (1024*1024), 2)} MB")
    print(f"Python RSS Memory:              {py_end_mb} MB (Delta: +{round(py_end_mb - py_start_mb, 2)} MB)")
    print(f"Chromium Process-Tree RSS:      {chrom_end_mb} MB (Delta: +{round(chrom_end_mb - chrom_start_mb, 2)} MB)")
    print(f"Total Combined RSS Footprint:   {total_end_mb} MB")
    db_stats = db_metrics.get_stats()
    print(f"\n--- SQLite Performance Metrics ---")
    print(f"Total Persisted Records:        {db_stats['total_persisted']}")
    print(f"Median Write Latency:           {db_stats['median_latency_ms']:.2f} ms")
    print(f"P95 Write Latency:              {db_stats['p95_latency_ms']:.2f} ms")
    print(f"Database Lock Errors:           {db_stats['lock_errors']}")
    print(f"Exhausted Retries:              {db_stats['exhausted_retries']}")

    print(f"\n--- Category-by-Category Confusion Matrix ---")
    headers = f"{'True \\ Pred':<14} | " + " | ".join(f"{c:<10}" for c in categories)
    print(headers)
    print("-" * len(headers))
    for t in categories:
        row = f"{t:<14} | " + " | ".join(f"{confusion_matrix[t][p]:<10}" for p in categories)
        print(row)

    print(f"\n--- Per-Class Precision / Recall / F1-Score ---")
    print(f"{'Category':<14} | {'Precision':<10} | {'Recall':<10} | {'F1-Score':<10}")
    print("-" * 52)
    for c in categories:
        tp = confusion_matrix[c][c]
        fp = sum(confusion_matrix[other][c] for other in categories if other != c)
        fn = sum(confusion_matrix[c][other] for other in categories if other != c)
        precision = round((tp / (tp + fp)) * 100, 1) if (tp + fp) > 0 else 0.0
        recall = round((tp / (tp + fn)) * 100, 1) if (tp + fn) > 0 else 0.0
        f1 = round((2 * precision * recall / (precision + recall)), 1) if (precision + recall) > 0 else 0.0
        print(f"{c:<14} | {precision:>9.1f}% | {recall:>9.1f}% | {f1:>9.1f}%")

    await bp.stop()
    await db.close()
    server.should_exit = True
    await asyncio.sleep(0.5)

    assert accuracy == 100.0, f"Benchmark accuracy fell below 100%: {accuracy}%"
    print("\n[OK] 100-URL High-Fidelity Benchmark Completed with 100% Accuracy!")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
