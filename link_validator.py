import asyncio
import httpx
import json
import math
import random
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set

from config import settings
from db import Database
from models import UrlResult
from status_classifier import StatusClassifier
from url_normalizer import URLNormalizer

class LinkValidator:
    """
    High-performance, bounded concurrent validation pool for Link Audit unique destinations.
    Implements:
    - Bounded worker concurrency (range 1-32).
    - Per-host concurrency rate limiting.
    - Long-lived HTTP client with connection pooling and explicit timeouts.
    - Per-hop SSRF validation during redirect chains.
    - Bounded exponential backoff with jitter for transient failures (429, 502, 503, 504, timeout).
    - Incremental batch persistence to SQLite.
    - Atomic live metrics (in-flight, queued, completed, URLs/min, estimated time).
    """

    def __init__(self, db: Optional[Database] = None, concurrency: Optional[int] = None, per_host_concurrency: Optional[int] = None, allow_local: bool = False, max_retries: Optional[int] = None):
        self.db = db or Database()
        self.allow_local = allow_local
        
        # Bounded concurrency: strictly 1 to 50
        raw_concurrency = concurrency if concurrency is not None else getattr(settings, 'LINK_AUDIT_CONCURRENCY', 10)
        self.concurrency = max(1, min(int(raw_concurrency), 50))
        
        # Per-host concurrency
        raw_per_host = per_host_concurrency if per_host_concurrency is not None else getattr(settings, 'LINK_AUDIT_PER_HOST_CONCURRENCY', 5)
        self.per_host_concurrency = max(1, min(int(raw_per_host), self.concurrency))
        
        self.connect_timeout = getattr(settings, 'LINK_AUDIT_CONNECT_TIMEOUT', 5.0)
        self.read_timeout = getattr(settings, 'LINK_AUDIT_READ_TIMEOUT', 15.0)
        self.total_timeout = getattr(settings, 'LINK_AUDIT_TOTAL_TIMEOUT', 20.0)
        self.max_retries = max_retries if max_retries is not None else getattr(settings, 'LINK_AUDIT_MAX_RETRIES', 2)
        self.queue_maxsize = getattr(settings, 'LINK_AUDIT_MAX_QUEUE_SIZE', 1000)

        # Long-lived HTTP client with connection pooling
        limits = httpx.Limits(
            max_connections=self.concurrency,
            max_keepalive_connections=self.concurrency,
            keepalive_expiry=60.0
        )
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=10.0,
            pool=5.0
        )
        self.http_client = httpx.AsyncClient(
            verify=False, # preserves current environment cert behavior while maintaining security controls
            timeout=timeout,
            limits=limits,
            follow_redirects=False, # manual hop-by-hop redirects with SSRF check
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )

        # Per-host semaphores
        self._host_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._sem_lock = asyncio.Lock()

        # In-memory live tracking per job
        self._live_stats: Dict[str, Dict[str, Any]] = {}
        self._stats_lock = asyncio.Lock()

        # Concurrency monitoring
        self.active_requests = 0
        self.peak_concurrency = 0

    async def aclose(self):
        if self.http_client:
            await self.http_client.aclose()

    async def _get_host_semaphore(self, host: str) -> asyncio.Semaphore:
        sem = self._host_semaphores.get(host)
        if sem is not None:
            return sem
        async with self._sem_lock:
            if host not in self._host_semaphores:
                self._host_semaphores[host] = asyncio.Semaphore(self.per_host_concurrency)
            return self._host_semaphores[host]

    def _get_host(self, url: str) -> str:
        try:
            return urllib.parse.urlsplit(url).netloc.lower() or "unknown_host"
        except Exception:
            return "unknown_host"

    def get_live_stats(self, job_id: str) -> Optional[Dict[str, Any]]:
        st = self._live_stats.get(job_id)
        if not st:
            return None
        now = time.time()
        elapsed = round(max(0.0, now - st["start_time"]), 2)
        completed = st["completed_unique"]
        total = st["total_unique"]
        
        # Calculate rolling rate and ETA
        rate = round((completed / (elapsed / 60.0)), 1) if elapsed > 1.0 else 0.0
        remaining = max(0, total - completed)
        eta_sec = round((remaining / (rate / 60.0)), 1) if rate > 0 and remaining > 0 else 0.0

        return {
            "job_id": job_id,
            "total_extracted": st["total_extracted"],
            "total_unique": total,
            "queued_destinations": st["queued_destinations"],
            "in_flight_validations": st["in_flight_validations"],
            "completed_unique": completed,
            "failed_validations": st["failed_validations"],
            "working_200": st["working_200"],
            "redirects_3xx": st["redirects_3xx"],
            "retry_attempts": st["retry_attempts"],
            "duplicate_avoided": st["duplicate_avoided"],
            "dedup_ratio": st["dedup_ratio"],
            "current_concurrency": self.concurrency,
            "peak_concurrency_observed": self.peak_concurrency,
            "urls_per_minute": rate,
            "elapsed_time": elapsed,
            "estimated_remaining_time": f"~{int(eta_sec)}s" if eta_sec > 0 else "0s"
        }

    async def check_destination(self, url: str, job_id: str = "") -> Dict[str, Any]:
        """
        Validates a single destination URL with:
        - Manual redirect hops (SSRF check on each target).
        - Per-host concurrency control.
        - Transient failure retries with backoff and jitter.
        - Respecting Retry-After header for 429/503.
        """
        # 1. Initial SSRF check on base URL
        if not URLNormalizer.is_safe_url(url, allow_local=self.allow_local):
            return {
                "job_id": job_id,
                "normalized_url": url,
                "original_url": url,
                "initial_status_code": None,
                "final_status_code": None,
                "status": "Blocked (SSRF)",
                "final_url": url,
                "redirect_count": 0,
                "redirect_chain": "[]",
                "response_time_ms": 0.0,
                "content_type": "",
                "content_length": 0,
                "error_type": "Blocked (SSRF)",
                "error_message": "Destination blocked by security policy / SSRF",
                "check_method": "SSRF_FILTER",
                "browser_verified": "NO",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "retries_count": 0
            }

        start_total = time.time()
        retries_done = 0
        final_record = None

        while retries_done <= self.max_retries:
            host = self._get_host(url)
            host_sem = await self._get_host_semaphore(host)

            is_transient_failure = False
            retry_after_delay = None
            start_attempt = time.time()

            try:
                # Track in-flight concurrency
                self.active_requests += 1
                if self.active_requests > self.peak_concurrency:
                    self.peak_concurrency = self.active_requests

                async with host_sem:
                    current_url = url
                    redirect_chain = []
                    max_hops = 10
                    final_resp = None
                    is_ssrf_blocked_in_redirect = False

                    for hop in range(max_hops):
                        # Verify SSRF on every redirect hop
                        if not URLNormalizer.is_safe_url(current_url, allow_local=self.allow_local):
                            is_ssrf_blocked_in_redirect = True
                            break

                        resp = await self.http_client.get(current_url)
                        status_code = resp.status_code

                        if status_code in [301, 302, 303, 307, 308]:
                            redirect_chain.append({"url": current_url, "status": status_code})
                            location = resp.headers.get("Location")
                            if not location:
                                final_resp = resp
                                break
                            # Resolve target location
                            next_url = urllib.parse.urljoin(current_url, location)
                            current_url = URLNormalizer.normalize(next_url)
                        else:
                            final_resp = resp
                            break

                    elapsed_attempt_ms = round((time.time() - start_attempt) * 1000, 2)

                    if is_ssrf_blocked_in_redirect:
                        final_record = {
                            "job_id": job_id,
                            "normalized_url": url,
                            "original_url": url,
                            "initial_status_code": redirect_chain[0]["status"] if redirect_chain else None,
                            "final_status_code": None,
                            "status": "Blocked (SSRF)",
                            "final_url": current_url,
                            "redirect_count": len(redirect_chain),
                            "redirect_chain": json.dumps(redirect_chain),
                            "response_time_ms": elapsed_attempt_ms,
                            "content_type": "",
                            "content_length": 0,
                            "error_type": "Blocked (SSRF)",
                            "error_message": f"Redirect target blocked by security policy: {current_url}",
                            "check_method": "SSRF_FILTER",
                            "browser_verified": "NO",
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                            "retries_count": retries_done
                        }
                        break

                    # Successful response received
                    st_code = final_resp.status_code
                    content_type = final_resp.headers.get("content-type", "")
                    content_length = int(final_resp.headers.get("content-length", 0)) if (final_resp.headers.get("content-length") or "").isdigit() else 0
                    init_code = redirect_chain[0]["status"] if redirect_chain else st_code

                    # Classification
                    cat = StatusClassifier.classify(st_code)
                    if init_code != st_code and 300 <= init_code <= 399:
                        status_str = f"Redirect -> {cat}"
                    else:
                        status_str = cat

                    final_record = {
                        "job_id": job_id,
                        "normalized_url": url,
                        "original_url": url,
                        "initial_status_code": init_code,
                        "final_status_code": st_code,
                        "status": status_str,
                        "final_url": current_url,
                        "redirect_count": len(redirect_chain),
                        "redirect_chain": json.dumps(redirect_chain),
                        "response_time_ms": elapsed_attempt_ms,
                        "content_type": content_type,
                        "content_length": content_length,
                        "error_type": "" if st_code < 400 else StatusClassifier.classify(st_code),
                        "error_message": "" if st_code < 400 else f"HTTP {st_code}",
                        "check_method": "HTTP",
                        "browser_verified": "NO",
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "retries_count": retries_done
                    }

                    # Check for transient retryable HTTP statuses (429, 502, 503, 504)
                    if st_code == 429:
                        is_transient_failure = True
                        ra = final_resp.headers.get("Retry-After")
                        if ra and ra.isdigit():
                            retry_after_delay = min(float(ra), 5.0)
                    elif st_code in [502, 503, 504]:
                        is_transient_failure = True

            except Exception as e:
                elapsed_attempt_ms = round((time.time() - start_attempt) * 1000, 2)
                err_text = str(e).lower()

                if "timeout" in err_text or isinstance(e, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
                    err_type = "Timeout"
                    is_transient_failure = True
                elif "dns" in err_text or "name_not_resolved" in err_text or "resolve" in err_text:
                    err_type = "DNS Error"
                    # DNS failure is generally non-transient, but may retry once for network jitter
                    is_transient_failure = (retries_done == 0)
                elif "connect" in err_text or isinstance(e, (httpx.ConnectError, ConnectionRefusedError)):
                    err_type = "Connection Error"
                    is_transient_failure = True
                elif "ssl" in err_text or "cert" in err_text:
                    err_type = "SSL Error"
                    is_transient_failure = False # SSL issues don't resolve upon instant retry
                else:
                    err_type = "Network Error"
                    is_transient_failure = True

                final_record = {
                    "job_id": job_id,
                    "normalized_url": url,
                    "original_url": url,
                    "initial_status_code": None,
                    "final_status_code": None,
                    "status": err_type,
                    "final_url": url,
                    "redirect_count": 0,
                    "redirect_chain": "[]",
                    "response_time_ms": elapsed_attempt_ms,
                    "content_type": "",
                    "content_length": 0,
                    "error_type": err_type,
                    "error_message": str(e)[:250],
                    "check_method": "HTTP",
                    "browser_verified": "NO",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "retries_count": retries_done
                }

            finally:
                self.active_requests = max(0, self.active_requests - 1)

            # Retry handling: if transient and within retry limit
            if is_transient_failure and retries_done < self.max_retries:
                retries_done += 1
                if job_id and job_id in self._live_stats:
                    self._live_stats[job_id]["retry_attempts"] += 1

                # Calculate backoff: respects Retry-After if provided, else bounded exponential backoff with jitter
                if retry_after_delay:
                    sleep_sec = retry_after_delay + random.uniform(0.05, 0.2)
                else:
                    sleep_sec = min(5.0, 0.4 * (1.6 ** retries_done) + random.uniform(0.05, 0.25))

                # Note: sleep occurs OUTSIDE host_sem and active_requests so other hosts proceed!
                await asyncio.sleep(sleep_sec)
            else:
                break

        return final_record

    async def validate_job_destinations(self, job_id: str, force_recheck: bool = False, on_progress: Optional[Any] = None) -> Dict[str, Any]:
        """
        Executes bounded concurrent validation for all unique destinations in a Link Audit job.
        """
        # 1. Fetch unique destinations and extracted stats
        unique_urls = await self.db.get_unique_destinations_for_job(job_id)
        stats_db = await self.db.get_link_audit_stats(job_id)
        
        total_unique = len(unique_urls)
        total_extracted = stats_db.get("total_extracted", total_unique)
        duplicate_avoided = max(0, total_extracted - total_unique)
        dedup_ratio = round((duplicate_avoided / total_extracted) * 100, 2) if total_extracted > 0 else 0.0

        # 2. Initialize in-memory live tracking
        start_time = time.time()
        st = {
            "start_time": start_time,
            "total_extracted": total_extracted,
            "total_unique": total_unique,
            "duplicate_avoided": duplicate_avoided,
            "dedup_ratio": dedup_ratio,
            "queued_destinations": total_unique,
            "in_flight_validations": 0,
            "completed_unique": 0,
            "failed_validations": 0,
            "working_200": 0,
            "redirects_3xx": 0,
            "retry_attempts": 0
        }
        self._live_stats[job_id] = st

        if total_unique == 0:
            return st

        # 3. Filter cache hits & direct SSRF blocks
        worklist = []
        cached_batch = []
        blocked_batch = []

        for u in unique_urls:
            # Direct SSRF block
            if not URLNormalizer.is_safe_url(u, allow_local=self.allow_local):
                blocked_batch.append({
                    "job_id": job_id,
                    "normalized_url": u,
                    "original_url": u,
                    "initial_status_code": None,
                    "final_status_code": None,
                    "status": "Blocked (SSRF)",
                    "final_url": u,
                    "redirect_count": 0,
                    "redirect_chain": "[]",
                    "response_time_ms": 0.0,
                    "content_type": "",
                    "content_length": 0,
                    "error_type": "Blocked (SSRF)",
                    "error_message": "Destination blocked by security policy / SSRF",
                    "check_method": "SSRF_FILTER",
                    "browser_verified": "NO",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "retries_count": 0
                })
                continue

            # Cache lookup
            if not force_recheck:
                cached = await self.db.get_cached_result(u)
                if cached:
                    c_dict = dict(cached)
                    c_dict["job_id"] = job_id
                    cached_batch.append(c_dict)
                    continue

            worklist.append(u)

        # Save initial cached and blocked records
        if blocked_batch:
            await self.db.batch_upsert_job_destinations(blocked_batch)
            await self.db.batch_insert_results(blocked_batch)
            st["completed_unique"] += len(blocked_batch)
            st["failed_validations"] += len(blocked_batch)
            st["queued_destinations"] -= len(blocked_batch)

        if cached_batch:
            await self.db.batch_upsert_job_destinations(cached_batch)
            for c in cached_batch:
                st["completed_unique"] += 1
                st["queued_destinations"] -= 1
                if c.get("final_status_code") == 200:
                    st["working_200"] += 1
                elif c.get("final_status_code") and 300 <= c["final_status_code"] <= 399:
                    st["redirects_3xx"] += 1
                else:
                    st["failed_validations"] += 1

        # Destination tracking: do NOT overwrite jobs.total_records or jobs.processed_records (which track source batch crawl)
        # Unique destination totals are tracked in live stats and destination tables

        # 4. Bounded Queue & Parallel Workers
        queue = asyncio.Queue(maxsize=self.queue_maxsize)
        results_buffer: List[Dict[str, Any]] = []
        flush_lock = asyncio.Lock()
        last_flush_time = time.time()
        is_aborted = False
        last_status_check = 0.0

        async def check_abort_needed() -> bool:
            nonlocal is_aborted, last_status_check
            now = time.time()
            if now - last_status_check >= 0.8:
                last_status_check = now
                try:
                    async with self.db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
                        row = await cur.fetchone()
                        if row and row[0] in ("PAUSED", "CANCELLED"):
                            is_aborted = True
                except Exception:
                    pass
            return is_aborted

        async def flush_results_buffer(force: bool = False):
            nonlocal last_flush_time
            async with flush_lock:
                now = time.time()
                batch_threshold = max(50, getattr(settings, 'DB_BATCH_SIZE', 500))
                if results_buffer and (force or len(results_buffer) >= batch_threshold or (now - last_flush_time) >= 0.5):
                    to_flush = list(results_buffer)
                    results_buffer.clear()
                    try:
                        await self.db.batch_upsert_job_destinations(to_flush)
                        await self.db.batch_insert_results(to_flush)
                        # Incrementally update jobs timestamp in database
                        await self.db.conn.execute(
                            "UPDATE jobs SET updated_at = ? WHERE id = ?",
                            (datetime.now(timezone.utc).isoformat(), job_id)
                        )
                        await self.db.conn.commit()
                    except Exception as e:
                        print(f"[LinkValidator] Batch upsert error: {e}")
                    last_flush_time = now

        async def worker():
            while True:
                if await check_abort_needed():
                    break
                try:
                    url = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if await check_abort_needed():
                        break
                    continue
                except (asyncio.CancelledError, GeneratorExit):
                    break

                if url is None:
                    queue.task_done()
                    break

                st["in_flight_validations"] += 1
                try:
                    record = await self.check_destination(url, job_id=job_id)
                    results_buffer.append(record)

                    # Update stats
                    st["completed_unique"] += 1
                    st["queued_destinations"] = max(0, st["queued_destinations"] - 1)
                    if record.get("final_status_code") == 200:
                        st["working_200"] += 1
                    elif record.get("final_status_code") and 300 <= record["final_status_code"] <= 399:
                        st["redirects_3xx"] += 1
                    else:
                        st["failed_validations"] += 1

                    await flush_results_buffer(force=False)
                    if on_progress:
                        try: on_progress(st)
                        except Exception: pass
                except Exception as worker_err:
                    print(f"[LinkValidator] Worker error for {url}: {worker_err}")
                finally:
                    st["in_flight_validations"] = max(0, st["in_flight_validations"] - 1)
                    queue.task_done()

        # Spawn bounded pool of workers
        worker_tasks = [asyncio.create_task(worker()) for _ in range(self.concurrency)]

        # Enqueue URLs into bounded queue
        for u in worklist:
            if is_aborted:
                break
            await queue.put(u)

        # Wait for queue completion unless aborted
        if not is_aborted:
            await queue.join()

        # Stop workers cleanly
        for _ in range(self.concurrency):
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        # Final flush
        await flush_results_buffer(force=True)

        # Update final timestamp in jobs table
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ?",
            (now_iso, job_id)
        )
        await self.db.conn.commit()

        return st
