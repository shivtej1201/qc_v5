import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import List
from config import settings
from db import Database
from job_manager import JobManager
from checker import URLChecker
from browser import BrowserPool
from url_normalizer import URLNormalizer
from extractor import LinkExtractor
from classifier import Classifier
from api_observer import ApiResponseObserver
from link_validator import LinkValidator
from diagnostics_403 import Diagnostics403
from status_classifier import classify_outcome

class BatchWorkerDaemon:
    def __init__(self, worker_id: str = None, worker_count: int = None, db: Database = None, jm: JobManager = None):
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.worker_count = worker_count if worker_count is not None else getattr(settings, 'WORKER_COUNT', getattr(settings, 'MAX_CONCURRENT_JOBS', 4))
        self._owns_db = db is None
        self.db = db if db is not None else Database()
        self.jm = jm if jm is not None else (JobManager(self.db) if db is not None else None)
        self.browser_pool = BrowserPool()
        self.checker = None
        self.extractor = None
        self.classifier = Classifier()
        self.api_observer = None
        self.link_validator = None
        
        # We process unique destination URLs found in the batch
        self.http_queue = asyncio.Queue(maxsize=10000)
        self.db_queue = asyncio.Queue(maxsize=10000)
        
        self.active = True
        self.active_jobs = 0
        self.active_advanced_jobs = set()
        self.worker_tasks = []

    def stop(self):
        self.active = False

    async def start(self):
        if not self.db.conn:
            await self.db.connect()
        if not self.jm:
            self.jm = JobManager(self.db)
        
        # Reset any batches and jobs that were previously held by this worker or whose lease expired
        await self.db.reset_running_batches(self.worker_id)
        
        await self.browser_pool.start()
        self.api_observer = ApiResponseObserver(self.browser_pool)
        self.checker = URLChecker(self.browser_pool)
        self.extractor = LinkExtractor(self.browser_pool)
        self.link_validator = LinkValidator(db=self.db, concurrency=settings.LINK_AUDIT_CONCURRENCY)
        
        # Start permanent HTTP workers
        # Spawn a large pool so the Tuner can scale up to its max_concurrency
        self.http_workers = [
            asyncio.create_task(self._http_worker()) 
            for _ in range(self.checker.tuner.max_concurrency + 50)
        ]
        
        # Start DB writer task
        db_worker_task = asyncio.create_task(self._db_writer())
        
        # Start concurrent worker loops
        self.worker_tasks = [
            asyncio.create_task(self._worker_loop(i))
            for i in range(self.worker_count)
        ]
        print(f"Worker daemon started with {self.worker_count} concurrent worker loops...")
        
        try:
            await asyncio.gather(*self.worker_tasks)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Daemon main loop crashed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.active = False
            for t in self.worker_tasks:
                if not t.done():
                    t.cancel()
            for w in getattr(self, 'http_workers', []): 
                if not w.done():
                    w.cancel()
            await self.db_queue.put(None)
            await db_worker_task
            if self.link_validator:
                try:
                    await self.link_validator.aclose()
                except Exception:
                    pass
            if self.checker: 
                try:
                    await self.checker.stop()
                except Exception:
                    pass
            try:
                await self.browser_pool.stop()
            except Exception:
                pass
            if self._owns_db:
                await self.db.close()

    async def _worker_loop(self, worker_idx: int):
        sub_worker_id = f"{self.worker_id}_w{worker_idx}"
        while self.active:
            try:
                # 1. Process jobs in advanced stages
                adv_job = await self.jm.claim_next_advanced_job(sub_worker_id)
                if adv_job:
                    self.active_jobs += 1
                    try:
                        await self._process_advanced_job(adv_job, sub_worker_id)
                    finally:
                        self.active_jobs = max(0, self.active_jobs - 1)
                    continue

                # 2. Process regular queued batches
                batch = await self.jm.get_next_queued_batch(sub_worker_id)
                if batch:
                    self.active_jobs += 1
                    try:
                        await self._process_batch_task(batch)
                    finally:
                        self.active_jobs = max(0, self.active_jobs - 1)
                    continue

                # 3. Idle: brief sleep before re-checking
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Worker loop {sub_worker_id} crashed: {e}")
                await asyncio.sleep(0.5)

    async def _process_batch_task(self, batch):
        try:
            if batch['mode'] == "url_status" and await self.db.job_has_url_items(batch['job_id']):
                done = await self._process_url_status_batch(batch)
                await self.jm.update_batch_progress(batch['id'], done, "COMPLETED")
                return
            await self._process_batch(batch)
            if batch['mode'] in ["link_audit", "image_audit"]:
                await self.http_queue.join()
            await self.jm.update_batch_progress(batch['id'], batch['total_records'], "COMPLETED")
        except Exception as e:
            print(f"Batch failed: {e}")
            await self.jm.update_batch_progress(batch['id'], 0, "FAILED", str(e))

    # ------------------------------------------------------------------------------------------
    # URL-status audit (ledger-backed): every submitted URL ends with a result or explicit reason
    # ------------------------------------------------------------------------------------------
    URL_STATUS_FLUSH_EVERY = 100

    @staticmethod
    def _ledger_record(row_no: int, norm: str, source: str, r: dict) -> dict:
        outcome, validated = classify_outcome(
            r.get("final_status_code"), r.get("initial_status_code"), r.get("redirect_count") or 0,
            r.get("error_type") or "", r.get("browser_verified") or "NO",
        )
        return {
            "row_no": row_no, "normalized_url": norm, "validation_source": source,
            "outcome": outcome, "validated": validated, "status": r.get("status"),
            "initial_status_code": r.get("initial_status_code"), "final_status_code": r.get("final_status_code"),
            "final_url": r.get("final_url"), "redirect_count": r.get("redirect_count") or 0,
            "redirect_chain": r.get("redirect_chain") or "[]", "response_time_ms": r.get("response_time_ms"),
            "content_type": r.get("content_type") or "", "error_type": r.get("error_type") or "",
            "error_message": (r.get("error_message") or "")[:500], "check_method": r.get("check_method") or "",
            "attempts": r.get("attempts") or 0, "checked_at": r.get("checked_at") or datetime.utcnow().isoformat() + "Z",
        }

    @staticmethod
    def _unvalidated_result(norm: str, error_type: str, message: str, method: str) -> dict:
        return {"normalized_url": norm, "status": error_type, "error_type": error_type, "error_message": message,
                "check_method": method, "final_status_code": None, "attempts": 0,
                "checked_at": datetime.utcnow().isoformat() + "Z"}

    async def _process_url_status_batch(self, batch) -> int:
        job_id, batch_id = batch['job_id'], batch['id']
        force_recheck = bool(batch.get('force_recheck'))
        allow_local = getattr(settings, "ALLOW_LOCAL_TESTING", False)
        healed = await self.db.heal_pending_duplicates(job_id)   # repair duplicates stranded by a crash
        if healed:
            print(f"[url_status] job {job_id}: copied results onto {healed} stranded duplicate rows")
        items = await self.db.get_pending_url_items_for_batch(job_id, batch_id)   # resume: PENDING rows only
        reusable = {} if force_recheck else await self.db.get_reusable_results_bulk(
            list({n for _, _, n in items if n}))

        buf_items: list = []
        buf_cache: list = []
        flush_lock = asyncio.Lock()

        async def flush():
            nonlocal buf_items, buf_cache
            async with flush_lock:
                if not buf_items:
                    return
                its, cache = buf_items, buf_cache
                buf_items, buf_cache = [], []
                await self.db.record_url_item_results(job_id, its, cache)
                done = await self.db.count_url_items_done_in_batch(job_id, batch_id)
                await self.jm.update_batch_progress(batch_id, done)   # also renews the batch lease

        async def validate_one(row_no: int, original: str, norm: str):
            try:
                cached = reusable.get(norm)
                if cached is not None:
                    rec = self._ledger_record(row_no, norm, "CACHED", cached)
                    cache_row = None
                else:
                    safety = await asyncio.to_thread(URLNormalizer.classify_url_safety, norm, allow_local)
                    if safety == "INVALID_URL":
                        r = self._unvalidated_result(norm, "Invalid URL", "Not a valid absolute http(s) URL; not requested", "SYNTAX_CHECK")
                        rec, cache_row = self._ledger_record(row_no, norm, "NOT_VALIDATED", r), None
                    elif safety == "DNS_ERROR":
                        r = self._unvalidated_result(norm, "DNS Error", "Hostname could not be resolved (DNS failure); not requested", "DNS_PREFLIGHT")
                        rec, cache_row = self._ledger_record(row_no, norm, "NEW_CHECK", r), None
                    elif safety == "BLOCKED_SSRF":
                        r = self._unvalidated_result(norm, "Blocked (SSRF)", "Destination resolves to a prohibited address; not requested", "SSRF_FILTER")
                        rec, cache_row = self._ledger_record(row_no, norm, "NOT_VALIDATED", r), None
                    else:
                        res = await self.checker.check(norm)
                        d = res.to_db_dict()
                        rec = self._ledger_record(row_no, norm, "NEW_CHECK", d)
                        cache_row = {k: v for k, v in d.items() if k != "attempts"}
            except Exception as e:
                # Failure isolation: one URL's unexpected error is recorded, never propagated
                r = self._unvalidated_result(norm, "Validation Error", f"{e.__class__.__name__}: {e}"[:500], "HTTP")
                rec, cache_row = self._ledger_record(row_no, norm, "NEW_CHECK", r), None
            buf_items.append(rec)
            if cache_row is not None:
                buf_cache.append(cache_row)
            if len(buf_items) >= self.URL_STATUS_FLUSH_EVERY:
                await flush()

        sem = asyncio.Semaphore(max(1, settings.HTTP_CONCURRENCY))

        async def bounded(item):
            async with sem:
                await validate_one(*item)

        async def heartbeat():
            # keep the batch lease alive while slow URLs (timeouts/retries) are in flight, so no
            # other worker re-claims this batch and validates the same URLs a second time
            interval = max(5.0, getattr(settings, 'BATCH_LEASE_DURATION_SEC', 60) / 3.0)
            while True:
                await asyncio.sleep(interval)
                try:
                    done = await self.db.count_url_items_done_in_batch(job_id, batch_id)
                    await self.jm.update_batch_progress(batch_id, done)
                except Exception as hb_err:
                    print(f"[url_status] lease heartbeat failed for batch {batch_id}: {hb_err}")

        hb = asyncio.create_task(heartbeat())
        tasks = [asyncio.create_task(bounded(it)) for it in items]
        try:
            if tasks:
                done_t, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for t in done_t:
                    if t.exception() is not None:
                        raise t.exception()   # e.g. persistent DB failure -> batch FAILED, rows stay PENDING
            await flush()
        finally:
            hb.cancel()
            for t in tasks:
                if not t.done():
                    t.cancel()

        # Batch is complete only when none of its unique rows is still PENDING (no silent loss)
        left = await self.db.get_pending_url_items_for_batch(job_id, batch_id)
        if left:
            raise RuntimeError(f"{len(left)} URLs in batch {batch_id} still have no result")
        return await self.db.count_url_items_done_in_batch(job_id, batch_id)

    async def _process_batch(self, batch):
        mode = batch['mode']
        input_file = batch['input_file']
        with open(input_file, 'r') as f:
            urls = json.load(f)
            
        unique_targets = set()
        force_recheck = batch.get('force_recheck', False)
        processed_sources = 0

        # Push to HTTP Queue helper
        async def queue_validation(url_norm):
            if url_norm not in unique_targets:
                unique_targets.add(url_norm)
                if not force_recheck:
                    res = await (self.db.get_cached_image_result(url_norm) if mode == "image_audit" else self.db.get_cached_result(url_norm))
                    if res: return
                await self.http_queue.put({"mode": mode, "url": url_norm})

        if mode == "api_response_audit":
            completed_urls = set(await self.db.get_audited_page_urls(batch['job_id']))
            urls_to_audit = [u for u in urls if u not in completed_urls]
            processed_sources = len(completed_urls)
            if processed_sources > 0:
                await self.jm.update_batch_progress(batch['id'], processed_sources)

            sem = asyncio.Semaphore(settings.BROWSER_CONCURRENCY)
            async def audit_single_page(url):
                async with sem:
                    try:
                        page_res, api_resps = await self.api_observer.audit_page(batch['job_id'], url, batch['id'])
                        await self.db.batch_insert_page_audit_results([page_res.to_db_dict()])
                        if api_resps:
                            await self.db.batch_insert_api_responses([a.to_db_dict() for a in api_resps])
                    except Exception as page_err:
                        err_str = str(page_err)
                        fallback_res = {
                            "job_id": batch['job_id'],
                            "batch_id": batch['id'],
                            "page_url": url,
                            "final_url": url,
                            "page_status": "PAGE_ERROR",
                            "http_status": None,
                            "response_time_ms": 0,
                            "error_message": err_str[:200],
                            "screenshot_path": "",
                            "api_count": 0,
                            "failed_api_count": 0,
                            "summary_reason": f"Audit execution exception: {err_str[:120]}",
                            "evaluated_at": datetime.utcnow().isoformat() + "Z"
                        }
                        await self.db.batch_insert_page_audit_results([fallback_res])

            tasks = [asyncio.create_task(audit_single_page(u)) for u in urls_to_audit]
            for coro in asyncio.as_completed(tasks):
                try:
                    await coro
                except Exception:
                    pass
                processed_sources += 1
                await self.jm.update_batch_progress(batch['id'], processed_sources)

        elif mode in ["link_audit", "image_audit"]:
            sem = asyncio.Semaphore(settings.BROWSER_CONCURRENCY)
            async def extract(url):
                async with sem:
                    if mode == "link_audit":
                        items, html, final_url = await self.extractor.extract_links(url)
                        res = self.classifier.classify_from_html(url, final_url, html)
                        await self.db.update_source_classification(
                            url, res.page_type, res.business, res.method,
                            res.tag_1, res.tag_2, res.diagnostic_notes
                        )
                    else:
                        items = await self.extractor.extract_images(url)
                        
                    extracted_rows = []
                    for item in items:
                        norm = item.get("normalized_url")
                        extracted_rows.append({
                            "job_id": batch['job_id'],
                            "batch_id": batch['id'],
                            "source_url": item.get("source_url"),
                            "original_url": item.get("original_url") or item.get("original_href"),
                            "normalized_url": norm,
                            "anchor_text": item.get("anchor_text", ""),
                            "component_name": item.get("component_name", ""),
                            "image_name": item.get("image_name", ""),
                            "loading_type": item.get("loading_type", ""),
                            "extracted_timestamp": datetime.utcnow().isoformat() + "Z"
                        })
                        # ONLY validate immediately if it's image_audit
                        if mode == "image_audit":
                            await queue_validation(norm)
                    
                    if extracted_rows:
                        await self.jm.save_extracted_links(extracted_rows)
            
            tasks = [asyncio.create_task(extract(u)) for u in urls]
            for coro in asyncio.as_completed(tasks):
                await coro
                processed_sources += 1
                await self.jm.update_batch_progress(batch['id'], processed_sources)
        else:
            for u in urls:
                if URLNormalizer.is_safe_url(u):
                    await queue_validation(URLNormalizer.normalize(u))
            await self.jm.update_batch_progress(batch['id'], len(urls))

    async def _http_worker(self):
        try:
            while True:
                item = await self.http_queue.get()
                mode = item["mode"]
                url = item["url"]
                job_id = item.get("job_id")
                try:
                    res_obj = await self.checker.check_image(url) if mode == "image_audit" else await self.checker.check(url)
                    res_dict = res_obj if isinstance(res_obj, dict) else res_obj.to_db_dict()
                    if job_id:
                        res_dict["job_id"] = job_id
                        status_code = res_dict.get("final_status_code")
                        err_type = res_dict.get("error_type", "")
                        if status_code == 403 or err_type == "BROWSER_CHALLENGE_BLOCKED":
                            diag = Diagnostics403.classify(
                                status_code=status_code or 403,
                                headers={},
                                body_snippet="",
                                stage="page_crawl",
                                robots_allowed=True,
                                error_type=err_type
                            )
                            asyncio.create_task(self.db.record_request_diagnostic(
                                url=url,
                                final_url=res_dict.get("final_url", url),
                                stage="page_crawl",
                                classification=diag.get("classification", "HTTP_403_UNCLASSIFIED"),
                                evidence_type=diag.get("evidence_type", "VERIFIED_EVIDENCE"),
                                what_happened=diag.get("what_happened", "Access was forbidden (HTTP 403)."),
                                where_it_happened=diag.get("where_it_happened", "During page crawl."),
                                what_verified=diag.get("what_verified", "Verified HTTP 403 status."),
                                recommended_action=diag.get("recommended_action", "Check access control policy on origin server or edge firewall."),
                                http_status=status_code or 403,
                                job_id=job_id,
                                retry_appropriate=diag.get("retry_appropriate", False)
                            ))
                    await self.db_queue.put({"mode": mode, "data": res_dict, "queued_at": time.time(), "job_id": job_id})
                except Exception as e:
                    print(f"HTTP Worker error: {e}")
                finally:
                    self.http_queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _db_writer(self):
        url_batch = []
        img_batch = []
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self.db_queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        if url_batch:
                            await self.db.batch_insert_results(url_batch)
                            jd_items = [d for d in url_batch if d.get("job_id")]
                            if jd_items:
                                await self.db.batch_upsert_job_destinations(jd_items)
                            url_batch.clear()
                        if img_batch:
                            await self.db.batch_insert_image_results(img_batch)
                            img_batch.clear()
                    except Exception as e:
                        print(f"DB Writer timeout flush error: {e}")
                    continue
                    
                if item is None:
                    try:
                        if url_batch:
                            await self.db.batch_insert_results(url_batch)
                            jd_items = [d for d in url_batch if d.get("job_id")]
                            if jd_items:
                                await self.db.batch_upsert_job_destinations(jd_items)
                        if img_batch:
                            await self.db.batch_insert_image_results(img_batch)
                    except Exception as e:
                        print(f"DB Writer final flush error: {e}")
                    self.db_queue.task_done()
                    break
                    
                if item["mode"] == "image_audit":
                    img_batch.append(item["data"])
                    if len(img_batch) >= settings.DB_BATCH_SIZE:
                        try:
                            await self.db.batch_insert_image_results(img_batch)
                        except Exception as e:
                            print(f"DB Writer image flush error: {e}")
                        img_batch.clear()
                else:
                    url_batch.append(item["data"])
                    if len(url_batch) >= settings.DB_BATCH_SIZE:
                        try:
                            await self.db.batch_insert_results(url_batch)
                            jd_items = [d for d in url_batch if d.get("job_id")]
                            if jd_items:
                                await self.db.batch_upsert_job_destinations(jd_items)
                        except Exception as e:
                            print(f"DB Writer url flush error: {e}")
                        url_batch.clear()
                        
                self.db_queue.task_done()
        except asyncio.CancelledError:
            try:
                if url_batch:
                    await self.db.batch_insert_results(url_batch)
                    jd_items = [d for d in url_batch if d.get("job_id")]
                    if jd_items:
                        await self.db.batch_upsert_job_destinations(jd_items)
                if img_batch:
                    await self.db.batch_insert_image_results(img_batch)
            except: pass
        except Exception as e:
            print(f"DB Writer crashed: {e}")

    async def _process_advanced_job(self, job, worker_id: str = None):
        job_id = job['id']
        status = job['status']
        mode = job['mode']
        force_recheck = job.get('force_recheck', False)
        try:
            if status == "VALIDATING":
                if mode == "link_audit":
                    if not self.link_validator:
                        self.link_validator = LinkValidator(db=self.db, concurrency=settings.LINK_AUDIT_CONCURRENCY)
                    await self.link_validator.validate_job_destinations(job_id, force_recheck=force_recheck)
                    await self.jm.update_job_status(job_id, "FINALIZING")
                else:
                    # Build unique validation worklist for this job
                    urls = await self.db.get_unique_destinations_for_job(job_id)
                    queued_count = 0
                    cached_items = []
                    blocked_items = []

                    for url in urls:
                        # 1. SSRF check
                        if not URLNormalizer.is_safe_url(url):
                            blocked_items.append({
                                "job_id": job_id,
                                "normalized_url": url,
                                "initial_status_code": None,
                                "final_status_code": None,
                                "status": "Blocked (SSRF)",
                                "final_url": url,
                                "error_type": "Blocked (SSRF)",
                                "error_message": "Destination blocked by SSRF policy",
                                "check_method": "SSRF_FILTER",
                                "browser_verified": "NO",
                                "checked_at": datetime.utcnow().isoformat() + "Z"
                            })
                            continue

                        # 2. Check cache if force_recheck is False
                        if not force_recheck:
                            cached = await self.db.get_cached_result(url)
                            if cached:
                                c_dict = dict(cached)
                                c_dict["job_id"] = job_id
                                cached_items.append(c_dict)
                                continue

                        # 3. Queue unique destination for HTTP validation
                        await self.http_queue.put({"mode": mode, "url": url, "job_id": job_id})
                        queued_count += 1

                    if blocked_items:
                        await self.db.batch_upsert_job_destinations(blocked_items)
                        await self.db.batch_insert_results(blocked_items)

                    if cached_items:
                        await self.db.batch_upsert_job_destinations(cached_items)

                    if queued_count > 0:
                        await self.http_queue.join()
                        
                    # Short yield to allow DB writer to commit any pending in-flight items
                    await asyncio.sleep(0.5)

                    # Move to FINALIZING
                    await self.jm.update_job_status(job_id, "FINALIZING")
                
            elif status == "FINALIZING":
                await self.jm.update_job_status(job_id, "COMPLETED")
                
        except Exception as e:
            print(f"Error processing advanced job {job_id} in state {status}: {e}")
        finally:
            if job_id in self.active_advanced_jobs:
                self.active_advanced_jobs.remove(job_id)
