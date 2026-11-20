import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/benchmarks)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import asyncio
import os
import sys
import time
import uuid
import psutil
import statistics
import argparse
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

import httpx
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from qcv5.core.db import Database
from qcv5.crawler.html_parser import Parser
from qcv5.crawler.classifier import Classifier
from qcv5.crawler.url_normalizer import URLNormalizer
from qcv5.crawler.status_classifier import StatusClassifier
from qcv5.core.config import settings

# -----------------------------------------------------------------------------
# Controlled Mock Application Server
# -----------------------------------------------------------------------------
def create_mock_app():
    async def page_endpoint(request):
        idx = int(request.path_params.get("idx", "0"))
        # 10% redirects
        if idx % 10 == 0:
            target = f"http://127.0.0.1/ok/{idx + 1}"
            return Response("Moved Permanently", status_code=301, headers={"Location": target})
        # 5% not found
        if idx % 20 == 5:
            return Response("Not Found", status_code=404)
        # 3% server error
        if idx % 33 == 7:
            return Response("Internal Server Error", status_code=500)

        # Standard HTML page with internal links, anchor text, and containers
        next_id = idx + 1
        dup_id = (idx // 10) * 10 + 2 # Creates duplicate destinations across pages
        html = f"""<!DOCTYPE html>
<html>
<head><title>Page {idx}</title></head>
<body>
    <header><nav><a href="/nav_link">Nav</a></nav></header>
    <main id="main-content">
        <h1>Welcome to Page {idx}</h1>
        <p>This is product description content for test URL {idx}.</p>
        <div class="product-grid">
            <a href="/ok/{next_id}" class="btn">Next Product {next_id}</a>
            <a href="/ok/{dup_id}" class="shared-link">Shared Category {dup_id}</a>
            <a href="/about" class="info-link">About Us</a>
        </div>
    </main>
    <footer><p>&copy; 2026 QC V5 Test Corporation</p></footer>
</body>
</html>"""
        return HTMLResponse(html, status_code=200)

    async def static_endpoint(request):
        return HTMLResponse("<html><body>Static Page OK</body></html>", status_code=200)

    routes = [
        Route("/ok/{idx}", page_endpoint),
        Route("/nav_link", static_endpoint),
        Route("/about", static_endpoint),
    ]
    return Starlette(routes=routes)


# -----------------------------------------------------------------------------
# Benchmark Runner
# -----------------------------------------------------------------------------
class BenchmarkRunner:
    def __init__(self, target_urls: int, db_path: str = "benchmark_run.db", concurrency: int = 50):
        self.target_urls = target_urls
        self.db_path = db_path
        self.concurrency = concurrency
        self.app = create_mock_app()
        self.transport = httpx.ASGITransport(app=self.app)
        
        # Telemetry metrics
        self.latencies: List[float] = []
        self.network_wait_times: List[float] = []
        self.html_parse_times: List[float] = []
        self.db_write_times: List[float] = []
        self.mem_samples: List[float] = []
        self.cpu_samples: List[float] = []
        self.queue_depth_samples: List[int] = []
        self.worker_samples: List[int] = []
        self.transaction_counts: List[int] = []
        self.records_per_tx: List[int] = []
        
        # Counts
        self.discovered_urls = 0
        self.scheduled_urls = 0
        self.processed_urls = 0
        self.successful_urls = 0
        self.failed_urls = 0
        self.timeouts = 0
        self.redirects = 0
        self.duplicates = 0
        self.active_workers = 0
        self.peak_workers = 0

        # Sustained window tracking
        self.window_size = 10000 if target_urls >= 10000 else 1000
        self.windows: List[Dict[str, Any]] = []
        self.last_window_time = 0.0
        self.last_window_url = 0
        self.last_window_lat_idx = 0
        self.last_window_db_idx = 0
        self.last_window_worker_idx = 0
        self.last_window_queue_idx = 0
        self.last_window_tx_idx = 0
        self.last_window_rec_idx = 0
        self.last_window_timeouts = 0
        self.last_window_errors = 0
        self.last_window_duplicates = 0
        self.window_lock = None

    def record_window(self, now: float, process: psutil.Process, queue: asyncio.Queue):
        window_start = self.last_window_url
        window_end = self.processed_urls
        completed = window_end - window_start
        if completed <= 0:
            return
        window_duration = max(0.001, now - self.last_window_time)
        urls_sec = completed / window_duration
        urls_min = urls_sec * 60.0

        win_latencies = self.latencies[self.last_window_lat_idx:len(self.latencies)]
        self.last_window_lat_idx = len(self.latencies)
        avg_http_lat = statistics.mean(win_latencies) if win_latencies else 0.0

        win_db_times = self.db_write_times[self.last_window_db_idx:len(self.db_write_times)]
        self.last_window_db_idx = len(self.db_write_times)
        avg_db_lat = (statistics.mean(win_db_times) * 1000.0) if win_db_times else 0.0

        win_workers = self.worker_samples[self.last_window_worker_idx:len(self.worker_samples)]
        self.last_window_worker_idx = len(self.worker_samples)
        non_zero_workers = [w for w in win_workers if w > 0]
        avg_workers = round(statistics.mean(non_zero_workers)) if non_zero_workers else self.concurrency

        win_queue = self.queue_depth_samples[self.last_window_queue_idx:len(self.queue_depth_samples)]
        self.last_window_queue_idx = len(self.queue_depth_samples)
        avg_queue = round(statistics.mean(win_queue)) if win_queue else queue.qsize()

        win_tx_count = len(self.transaction_counts) - self.last_window_tx_idx
        self.last_window_tx_idx = len(self.transaction_counts)

        win_rec = self.records_per_tx[self.last_window_rec_idx:len(self.records_per_tx)]
        self.last_window_rec_idx = len(self.records_per_tx)
        avg_rec_per_tx = round(statistics.mean(win_rec), 1) if win_rec else 0.0

        rss_mem = process.memory_info().rss / (1024 * 1024)
        cpu = psutil.cpu_percent(interval=None)

        win_timeouts = self.timeouts - self.last_window_timeouts
        self.last_window_timeouts = self.timeouts

        win_errors = self.failed_urls - self.last_window_errors
        self.last_window_errors = self.failed_urls

        win_dups = self.duplicates - self.last_window_duplicates
        self.last_window_duplicates = self.duplicates

        db_size_mb = round(os.path.getsize(self.db_path) / (1024 * 1024), 2) if os.path.exists(self.db_path) else 0.0
        wal_path = f"{self.db_path}-wal"
        wal_size_mb = round(os.path.getsize(wal_path) / (1024 * 1024), 2) if os.path.exists(wal_path) else 0.0

        w_data = {
            "window_start_url": window_start,
            "window_end_url": window_end,
            "completed_urls": completed,
            "window_duration_s": round(window_duration, 2),
            "urls_per_sec": round(urls_sec, 1),
            "urls_per_min": round(urls_min, 1),
            "active_workers": avg_workers,
            "queue_depth": avg_queue,
            "cpu_usage_pct": round(cpu, 1),
            "rss_memory_mb": round(rss_mem, 1),
            "db_latency_ms": round(avg_db_lat, 2),
            "http_latency_ms": round(avg_http_lat, 2),
            "transaction_count": win_tx_count,
            "records_per_transaction": avg_rec_per_tx,
            "checkpoint_duration_ms": 0.0,
            "wal_size_mb": wal_size_mb,
            "db_size_mb": db_size_mb,
            "retry_count": 0,
            "timeout_rate": round(win_timeouts / completed, 4),
            "error_rate": round(win_errors / completed, 4),
            "duplicate_count": win_dups
        }
        self.windows.append(w_data)
        self.last_window_time = now
        self.last_window_url = window_end

        print(f"[Window {len(self.windows):>2}] {window_start:>7,} -> {window_end:>7,} | Speed: {urls_sec:>7.1f} u/s | Mem: {rss_mem:>5.1f} MB | CPU: {cpu:>4.1f}% | DB Lat: {avg_db_lat:>5.2f} ms | HTTP: {avg_http_lat:>4.2f} ms | Queue: {avg_queue:>4} | Workers: {avg_workers}")

    async def run(self) -> Dict[str, Any]:
        process = psutil.Process(os.getpid())
        initial_cpu = psutil.cpu_percent(interval=None)
        initial_mem_mb = process.memory_info().rss / (1024 * 1024)
        self.window_lock = asyncio.Lock()
        self.last_window_time = time.time()
        import gc
        gc.set_threshold(100000, 20, 20)

        if os.path.exists(self.db_path):
            try: os.remove(self.db_path)
            except: pass
        if os.path.exists(f"{self.db_path}-wal"):
            try: os.remove(f"{self.db_path}-wal")
            except: pass
        if os.path.exists(f"{self.db_path}-shm"):
            try: os.remove(f"{self.db_path}-shm")
            except: pass

        db = Database(self.db_path)
        await db.connect()

        job_id = f"job_bench_{self.target_urls}_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, f"bench_{self.target_urls}.xlsx", "link_audit", "PROCESSING", now)
        )
        await db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", self.target_urls, 0, "RUNNING", now, now)
        )
        await db.conn.commit()

        classifier = Classifier()
        limits = httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency)
        timeout = httpx.Timeout(5.0)

        # Bounded queues
        queue = asyncio.Queue(maxsize=1000)
        db_write_queue = asyncio.Queue(maxsize=10000)
        seen_urls = set()

        start_time = time.time()
        is_done = False

        # Background monitor for CPU, Memory, Queue Depth, Workers
        async def monitor_telemetry():
            while not is_done:
                try:
                    self.mem_samples.append(process.memory_info().rss / (1024 * 1024))
                    self.cpu_samples.append(psutil.cpu_percent(interval=None))
                    self.queue_depth_samples.append(queue.qsize())
                    self.worker_samples.append(self.active_workers)
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    break

        # DB Writer Task
        async def db_writer():
            buffer_records = []
            buffer_extracted = []

            async def flush_batch():
                if not buffer_records and not buffer_extracted:
                    return
                t0 = time.time()
                await db.batch_insert_results_and_destinations(buffer_records, buffer_extracted)
                dt = time.time() - t0
                self.db_write_times.append(dt)
                self.transaction_counts.append(1)
                self.records_per_tx.append(len(buffer_records) * 2 + len(buffer_extracted))
                buffer_records.clear()
                buffer_extracted.clear()

            while True:
                try:
                    item = await asyncio.wait_for(db_write_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    if is_done and db_write_queue.empty():
                        break
                    await flush_batch()
                    continue

                if item is None:
                    db_write_queue.task_done()
                    break

                kind, res_data, ext_data = item
                buffer_records.append(res_data)
                if ext_data:
                    buffer_extracted.extend(ext_data)

                if len(buffer_records) >= 500:
                    await flush_batch()

                db_write_queue.task_done()

            # Final flush
            await flush_batch()

        db_task = asyncio.create_task(db_writer())

        # Worker Coroutine
        async def crawl_worker(client: httpx.AsyncClient):
            while True:
                try:
                    url = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if is_done and queue.empty():
                        break
                    continue

                if url is None:
                    queue.task_done()
                    break

                self.active_workers += 1
                if self.active_workers > self.peak_workers:
                    self.peak_workers = self.active_workers

                try:
                    # 1. Fetch Request
                    t_net_start = time.time()
                    redirect_chain = []
                    current_url = url
                    final_resp = None
                    is_redirect = False

                    # Manual hop-by-hop redirect following with SSRF check
                    for hop in range(5):
                        resp = await client.get(current_url)
                        if 300 <= resp.status_code <= 399:
                            is_redirect = True
                            redirect_chain.append({"status": resp.status_code, "url": current_url})
                            loc = resp.headers.get("Location")
                            if not loc:
                                final_resp = resp
                                break
                            current_url = URLNormalizer.normalize(loc)
                            if not URLNormalizer.is_safe_url(current_url, allow_local=True):
                                break
                        else:
                            final_resp = resp
                            break

                    req_duration = time.time() - t_net_start
                    self.network_wait_times.append(req_duration)
                    self.latencies.append(req_duration * 1000.0)

                    st_code = final_resp.status_code if final_resp else 0
                    if is_redirect:
                        self.redirects += 1

                    if st_code == 200:
                        self.successful_urls += 1
                    elif st_code in (404, 500, 0):
                        self.failed_urls += 1

                    # 2. HTML Parsing & Link Extraction
                    html = final_resp.text if final_resp and "text/html" in final_resp.headers.get("content-type", "") else ""
                    extracted_rows = []
                    if html:
                        t_parse_start = time.time()
                        parser = Parser(current_url)
                        parser.feed(html)
                        filtered_links = parser.get_filtered_links()
                        self.html_parse_times.append(time.time() - t_parse_start)

                        for el in filtered_links:
                            resolved = parser.resolve_url(el['href'])
                            if resolved and URLNormalizer.is_safe_url(resolved, allow_local=True):
                                norm_link = URLNormalizer.normalize(resolved)
                                self.discovered_urls += 1
                                extracted_rows.append({
                                    "job_id": job_id,
                                    "batch_id": "batch_0",
                                    "source_url": url,
                                    "original_url": resolved,
                                    "normalized_url": norm_link,
                                    "anchor_text": el['text'].strip(),
                                    "component_name": el['component'],
                                    "image_name": "",
                                    "loading_type": "",
                                    "extracted_timestamp": now
                                })

                                # Deduplication
                                if norm_link in seen_urls:
                                    self.duplicates += 1
                                else:
                                    seen_urls.add(norm_link)

                    # 3. Status Classification
                    cat = StatusClassifier.classify(st_code)
                    res_dict = {
                        "job_id": job_id,
                        "normalized_url": url,
                        "original_url": url,
                        "initial_status_code": redirect_chain[0]["status"] if redirect_chain else st_code,
                        "final_status_code": st_code,
                        "status": f"Redirect -> {cat}" if is_redirect else cat,
                        "final_url": current_url,
                        "redirect_count": len(redirect_chain),
                        "redirect_chain": str(redirect_chain),
                        "response_time_ms": round(req_duration * 1000.0, 2),
                        "content_type": final_resp.headers.get("content-type", "") if final_resp else "",
                        "content_length": len(final_resp.content) if final_resp else 0,
                        "error_type": "" if st_code < 400 else f"HTTP_{st_code}",
                        "error_message": "" if st_code < 400 else f"HTTP {st_code}",
                        "check_method": "HTTP",
                        "browser_verified": "NO",
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "retries_count": 0
                    }

                    # Queue for persistence (single atomic item per URL)
                    await db_write_queue.put(("item", res_dict, extracted_rows))

                    self.processed_urls += 1
                    if self.processed_urls % self.window_size == 0 or self.processed_urls == self.target_urls:
                        async with self.window_lock:
                            if self.processed_urls > self.last_window_url:
                                self.record_window(time.time(), process, queue)
                except Exception as ex:
                    self.failed_urls += 1
                    err_str = str(ex).lower()
                    if "timeout" in err_str:
                        self.timeouts += 1
                finally:
                    self.active_workers -= 1
                    queue.task_done()

        # Producer Task: Generates target URLs
        async def producer():
            for i in range(self.target_urls):
                url = f"http://127.0.0.1/ok/{i}"
                norm = URLNormalizer.normalize(url)
                seen_urls.add(norm)
                self.scheduled_urls += 1
                await queue.put(norm)

        # Execute
        async with httpx.AsyncClient(transport=self.transport, limits=limits, timeout=timeout) as client:
            worker_tasks = [asyncio.create_task(crawl_worker(client)) for _ in range(self.concurrency)]
            monitor_task = asyncio.create_task(monitor_telemetry())
            
            # Run producer
            await producer()
            await queue.join()
            is_done = True

            for _ in range(self.concurrency):
                await queue.put(None)
            await asyncio.gather(*worker_tasks)

            await db_write_queue.put(None)
            await db_task

        monitor_task.cancel()
        try: await monitor_task
        except asyncio.CancelledError: pass

        total_duration = time.time() - start_time
        urls_per_sec = round(self.processed_urls / total_duration, 2) if total_duration > 0 else 0.0
        urls_per_min = round(urls_per_sec * 60, 2)

        # Database telemetry
        await db.close()
        db_size_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        wal_path = f"{self.db_path}-wal"
        wal_size_bytes = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0

        # Percentiles
        sorted_lat = sorted(self.latencies) if self.latencies else [0.0]
        n_lat = len(sorted_lat)
        p50 = sorted_lat[int(n_lat * 0.50)]
        p95 = sorted_lat[int(n_lat * 0.95)]
        p99 = sorted_lat[min(int(n_lat * 0.99), n_lat - 1)]
        avg_lat = statistics.mean(sorted_lat) if sorted_lat else 0.0

        avg_cpu = round(statistics.mean(self.cpu_samples), 1) if self.cpu_samples else initial_cpu
        peak_mem = round(max(self.mem_samples), 2) if self.mem_samples else initial_mem_mb
        avg_mem = round(statistics.mean(self.mem_samples), 2) if self.mem_samples else initial_mem_mb
        avg_queue_depth = round(statistics.mean(self.queue_depth_samples), 1) if self.queue_depth_samples else 0.0
        worker_util = round((self.peak_workers / self.concurrency) * 100, 1)

        total_db_write_time = round(sum(self.db_write_times), 3)
        total_html_parse_time = round(sum(self.html_parse_times), 3)
        total_network_wait_time = round(sum(self.network_wait_times), 3)

        if self.processed_urls > self.last_window_url:
            self.record_window(time.time(), process, queue)

        # Sustained throughput analysis
        if len(self.windows) >= 5:
            num_windows_20pct = max(1, int(len(self.windows) * 0.20))
            early_windows = self.windows[:num_windows_20pct]
            late_windows = self.windows[-num_windows_20pct:]
            early_sustained = round(statistics.mean(w["urls_per_sec"] for w in early_windows), 1)
            late_sustained = round(statistics.mean(w["urls_per_sec"] for w in late_windows), 1)
            degradation_pct = round(((early_sustained - late_sustained) / early_sustained) * 100.0, 2) if early_sustained > 0 else 0.0
        elif self.windows:
            early_sustained = round(self.windows[0]["urls_per_sec"], 1)
            late_sustained = round(self.windows[-1]["urls_per_sec"], 1)
            degradation_pct = round(((early_sustained - late_sustained) / early_sustained) * 100.0, 2) if early_sustained > 0 else 0.0
        else:
            early_sustained = urls_per_sec
            late_sustained = urls_per_sec
            degradation_pct = 0.0

        peak_tp = max((w["urls_per_sec"] for w in self.windows), default=urls_per_sec)
        final_tp = self.windows[-1]["urls_per_sec"] if self.windows else urls_per_sec
        peak_ram = max((w["rss_memory_mb"] for w in self.windows), default=peak_mem)
        final_ram = self.windows[-1]["rss_memory_mb"] if self.windows else peak_mem
        initial_ram = self.windows[0]["rss_memory_mb"] if self.windows else initial_mem_mb
        ram_growth = round(final_ram - initial_ram, 1)
        max_q = max((w["queue_depth"] for w in self.windows), default=avg_queue_depth)
        initial_db = self.windows[0]["db_latency_ms"] if self.windows else 0.0
        final_db = self.windows[-1]["db_latency_ms"] if self.windows else 0.0

        result = {
            "total_urls": self.target_urls,
            "discovered_urls": self.discovered_urls,
            "scheduled_urls": self.scheduled_urls,
            "processed_urls": self.processed_urls,
            "successful_urls": self.successful_urls,
            "failed_urls": self.failed_urls,
            "timeouts": self.timeouts,
            "redirects": self.redirects,
            "duplicates": self.duplicates,
            "urls_per_sec": urls_per_sec,
            "urls_per_min": urls_per_min,
            "total_duration_s": round(total_duration, 2),
            "avg_latency_ms": round(avg_lat, 2),
            "p50_latency_ms": round(p50, 2),
            "p95_latency_ms": round(p95, 2),
            "p99_latency_ms": round(p99, 2),
            "cpu_usage_pct": avg_cpu,
            "peak_memory_mb": peak_mem,
            "avg_memory_mb": avg_mem,
            "database_size_mb": round(db_size_bytes / (1024 * 1024), 2),
            "wal_size_mb": round(wal_size_bytes / (1024 * 1024), 2),
            "database_write_time_s": total_db_write_time,
            "html_parse_time_s": total_html_parse_time,
            "network_wait_time_s": total_network_wait_time,
            "avg_queue_depth": avg_queue_depth,
            "worker_utilization_pct": worker_util,
            "concurrency": self.concurrency,
            "windows": self.windows,
            "sustained_analysis": {
                "early_sustained_throughput": early_sustained,
                "late_sustained_throughput": late_sustained,
                "throughput_degradation_pct": degradation_pct,
                "peak_throughput": peak_tp,
                "final_window_throughput": final_tp,
                "peak_ram_mb": peak_ram,
                "final_ram_mb": final_ram,
                "ram_growth_mb": ram_growth,
                "max_queue_depth": max_q,
                "initial_db_latency_ms": initial_db,
                "final_db_latency_ms": final_db
            }
        }
        return result

def print_result_table(res: Dict[str, Any]):
    print("=" * 80)
    print(f"BENCHMARK RESULTS: {res['total_urls']:,} URLs (Concurrency: {res['concurrency']})")
    print("=" * 80)
    print(f"Total URLs:                  {res['total_urls']:,}")
    print(f"Discovered URLs:             {res['discovered_urls']:,}")
    print(f"Scheduled URLs:              {res['scheduled_urls']:,}")
    print(f"Processed URLs:              {res['processed_urls']:,}")
    print(f"Successful URLs:             {res['successful_urls']:,}")
    print(f"Failed URLs:                 {res['failed_urls']:,}")
    print(f"Timeouts:                    {res['timeouts']:,}")
    print(f"Redirects:                   {res['redirects']:,}")
    print(f"Duplicates:                  {res['duplicates']:,}")
    print("-" * 80)
    print(f"Throughput (URLs/sec):       {res['urls_per_sec']:.2f}")
    print(f"Throughput (URLs/min):       {res['urls_per_min']:.2f}")
    print(f"Total Duration:              {res['total_duration_s']:.2f} s")
    print("-" * 80)
    print(f"Average Request Latency:     {res['avg_latency_ms']:.2f} ms")
    print(f"P50 Latency:                 {res['p50_latency_ms']:.2f} ms")
    print(f"P95 Latency:                 {res['p95_latency_ms']:.2f} ms")
    print(f"P99 Latency:                 {res['p99_latency_ms']:.2f} ms")
    print("-" * 80)
    print(f"CPU Usage (Avg):             {res['cpu_usage_pct']:.1f}%")
    print(f"Peak Memory:                 {res['peak_memory_mb']:.2f} MB")
    print(f"Average Memory:              {res['avg_memory_mb']:.2f} MB")
    print(f"Database Size:               {res['database_size_mb']:.2f} MB")
    print(f"WAL Size:                    {res['wal_size_mb']:.2f} MB")
    print("-" * 80)
    print(f"Database Write Time:         {res['database_write_time_s']:.3f} s")
    print(f"HTML Parse Time:             {res['html_parse_time_s']:.3f} s")
    print(f"Network Wait Time:           {res['network_wait_time_s']:.3f} s")
    print(f"Average Queue Depth:         {res['avg_queue_depth']:.1f}")
    print(f"Worker Utilization:          {res['worker_utilization_pct']:.1f}%")
    print("=" * 80)

    if "sustained_analysis" in res:
        sa = res["sustained_analysis"]
        print("\n" + "=" * 80)
        print("SUSTAINED THROUGHPUT ANALYSIS")
        print("=" * 80)
        print(f"Early Sustained Throughput:  {sa['early_sustained_throughput']} u/s")
        print(f"Late Sustained Throughput:   {sa['late_sustained_throughput']} u/s")
        print(f"Throughput Degradation:      {sa['throughput_degradation_pct']}% (Target: <= 10%)")
        print(f"Peak Throughput:             {sa['peak_throughput']} u/s")
        print(f"Final-Window Throughput:     {sa['final_window_throughput']} u/s")
        print(f"Peak RAM:                    {sa['peak_ram_mb']} MB")
        print(f"Final RAM:                   {sa['final_ram_mb']} MB")
        print(f"RAM Growth:                  {sa['ram_growth_mb']} MB")
        print(f"Maximum Queue Depth:         {sa['max_queue_depth']}")
        print(f"Initial DB Latency:          {sa['initial_db_latency_ms']} ms")
        print(f"Final DB Latency:            {sa['final_db_latency_ms']} ms")
        print("=" * 80)

        if res.get("windows"):
            print("\n### SUSTAINED THROUGHPUT WINDOWS")
            print("| Window Range | URLs/sec | RAM (MB) | CPU (%) | DB Latency (ms) | Queue Depth | Errors |")
            print("|---|---:|---:|---:|---:|---:|---:|")
            for w in res["windows"]:
                print(f"| {w['window_start_url']:,}–{w['window_end_url']:,} | {w['urls_per_sec']:.1f} | {w['rss_memory_mb']:.1f} | {w['cpu_usage_pct']:.1f}% | {w['db_latency_ms']:.2f} | {w['queue_depth']} | {int(w['error_rate'] * w['completed_urls'])} |")

async def main():
    parser = argparse.ArgumentParser(description="QC V5 500k URL Benchmark Suite")
    parser.add_argument("--scale", type=int, default=10000, help="Number of URLs to benchmark (e.g. 10000, 50000, 100000, 250000, 500000)")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrency level")
    parser.add_argument("--all", action="store_true", help="Run all benchmarks (10k, 50k, 100k, 250k, 500k)")
    parser.add_argument("--output", type=str, default="", help="JSON output file path")
    args = parser.parse_args()

    scales = [10000, 50000, 100000, 250000, 500000] if args.all else [args.scale]

    all_results = []
    for s in scales:
        print(f"\n>>> Running Benchmark for {s:,} URLs with Concurrency {args.concurrency}...")
        runner = BenchmarkRunner(target_urls=s, db_path=f"bench_{s}.sqlite", concurrency=args.concurrency)
        res = await runner.run()
        print_result_table(res)
        all_results.append(res)
        # Cleanup temporary benchmark database
        for ext in ["", "-wal", "-shm"]:
            fpath = f"bench_{s}.sqlite{ext}"
            if os.path.exists(fpath):
                try: os.remove(fpath)
                except: pass

    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        import json
        with open(args.output, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\n[OK] Results written to {args.output}")

    # Print summary markdown table
    if len(all_results) > 1:
        print("\n\n### SUMMARY BENCHMARK TABLE")
        headers = ["Scale", "Duration (s)", "Throughput (u/s)", "P50 (ms)", "P95 (ms)", "P99 (ms)", "Peak Mem (MB)", "DB Size (MB)", "DB Write (s)", "HTML Parse (s)"]
        print("| " + " | ".join(headers) + " |")
        print("|" + "|".join(["---"] * len(headers)) + "|")
        for r in all_results:
            row = [
                f"{r['total_urls']:,}",
                f"{r['total_duration_s']:.2f}",
                f"{r['urls_per_sec']:.1f}",
                f"{r['p50_latency_ms']:.2f}",
                f"{r['p95_latency_ms']:.2f}",
                f"{r['p99_latency_ms']:.2f}",
                f"{r['peak_memory_mb']:.1f}",
                f"{r['database_size_mb']:.1f}",
                f"{r['database_write_time_s']:.2f}",
                f"{r['html_parse_time_s']:.2f}"
            ]
            print("| " + " | ".join(row) + " |")

if __name__ == "__main__":
    asyncio.run(main())
