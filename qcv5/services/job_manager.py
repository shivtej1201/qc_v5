import os
import uuid
import json
from itertools import chain, islice
import pandas as pd
import openpyxl
from datetime import datetime, timedelta
from qcv5.core.config import settings
from qcv5.core.db import Database
from qcv5.crawler.url_normalizer import URLNormalizer

class JobManager:
    def __init__(self, db: Database):
        self.db = db
        os.makedirs("batches", exist_ok=True)

    async def create_upload(self, upload_id: str, filename: str, mode: str):
        now = datetime.utcnow().isoformat()
        await self.db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, filename, mode, "PROCESSING", now)
        )
        await self.db.conn.commit()

    def _find_url_col(self, df):
        candidates = ["page_url", "page url", "page", "url", "website", "link", "destination_url", "destination"]
        for c in df.columns:
            if str(c).strip().lower() in candidates:
                return c
        for c in df.columns:
            if "url" in str(c).strip().lower() or "link" in str(c).strip().lower():
                return c
        # No recognisable header: pick the first column whose sample values look like URLs
        for col in df.columns:
            sample = df[col].dropna().astype(str).str.strip().head(5)
            if any(v.lower().startswith(("http://", "https://")) for v in sample):
                return col
        return None

    @staticmethod
    def _csv_first_line_is_url(filepath: str) -> bool:
        try:
            with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
                first = f.readline()
        except OSError:
            return False
        return any(cell.strip().strip('"').lower().startswith(("http://", "https://")) for cell in first.split(","))

    async def parse_sheets(self, filepath: str):
        workbook = None
        sheet_names = ["CSV Data"] if filepath.lower().endswith(".csv") else None
        if sheet_names is None:
            workbook = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
            sheet_names = workbook.sheetnames

        sheets = []
        try:
            for sheet_name in sheet_names:
                count = sum(len(chunk) for chunk in self._iter_url_chunks(filepath, sheet_name))
                if count:
                    sheets.append({"name": sheet_name, "count": count})
        except Exception as e:
            print(f"Error reading upload {filepath}: {e}")
            return []
        finally:
            if workbook is not None:
                workbook.close()
        return sheets

    def _iter_url_chunks(self, filepath: str, sheet_name: str):
        chunk_size = max(1, settings.BATCH_SIZE)
        if filepath.lower().endswith(".csv"):
            # A file whose first line is already a URL has no header: read it headerless so the
            # first URL is not swallowed as a column name. Read everything as text and keep literal
            # "NA"/"null" cells (they are recorded as INVALID_URL instead of silently vanishing).
            reader = pd.read_csv(
                filepath, chunksize=chunk_size, dtype=str, keep_default_na=False, na_filter=False,
                header=None if self._csv_first_line_is_url(filepath) else "infer",
            )
            url_col = None
            for frame in reader:
                if url_col is None:
                    # Detect the URL column ONCE (header names, else the first chunk's sample) and keep it:
                    # re-detecting per chunk silently ended the file at the first chunk that happened to
                    # start with non-URL cells, dropping every remaining row.
                    url_col = self._find_url_col(frame)
                    if url_col is None:
                        return
                urls = [str(value).strip() for value in frame[url_col] if pd.notna(value) and str(value).strip()]
                for start in range(0, len(urls), chunk_size):
                    yield urls[start:start + chunk_size]
            return

        workbook = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        try:
            worksheet = workbook[sheet_name]
            rows = worksheet.iter_rows(values_only=True)
            headers = next(rows, ())
            sample_rows = list(islice(rows, 5))
            leading_rows = []
            if any(isinstance(h, str) and h.strip().lower().startswith(("http://", "https://")) for h in headers):
                # headerless sheet: the first row is data
                leading_rows = [headers]
                headers = tuple(f"col_{i}" for i in range(len(headers)))
            columns = pd.Index(headers)
            sample = pd.DataFrame(leading_rows + sample_rows, columns=columns) if len(columns) else pd.DataFrame()
            url_col = self._find_url_col(sample)
            if url_col is None:
                return
            column_index = columns.get_loc(url_col)
            batch = []
            for row in chain(leading_rows, sample_rows, rows):
                if column_index < len(row):
                    value = row[column_index]
                    if value is not None and str(value).strip():
                        batch.append(str(value).strip())
                        if len(batch) >= chunk_size:
                            yield batch
                            batch = []
            if batch:
                yield batch
        finally:
            workbook.close()

    async def create_jobs_for_sheets(self, upload_id: str, filepath: str, mode: str, selected_sheets: list, force_recheck: bool = False):
        existing = await self.get_jobs_for_upload(upload_id)
        if existing:
            return existing
        if filepath.lower().endswith(".csv"):
            sheet_names = [selected_sheets[0] if selected_sheets else "CSV Data"]
        else:
            workbook = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
            try:
                available_sheets = workbook.sheetnames
            finally:
                workbook.close()
            sheet_names = [name for name in (selected_sheets or available_sheets) if name in available_sheets]

        for sheet_name in sheet_names:
            await self._ingest_job(upload_id, sheet_name, mode, force_recheck,
                                   self._iter_url_chunks(filepath, sheet_name))
        await self.db.conn.commit()
        return await self.get_jobs_for_upload(upload_id)

    async def _ingest_job(self, upload_id: str, sheet_name: str, mode: str, force_recheck: bool, url_chunks) -> str:
        """
        Stream URL chunks into batches (and, for url_status, into the per-row ledger) without
        holding the whole sheet in memory. The job stays in INGESTING (not claimable by workers)
        until every row is stored and duplicates are marked; it then becomes QUEUED.
        Every submitted row gets a ledger row: invalid entries are recorded as INVALID_URL
        immediately instead of being dropped.
        """
        job_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"
        ledger = mode == "url_status"
        await self.db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, status, created_at, updated_at, force_recheck) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, sheet_name, 0, 'INGESTING', now, now, force_recheck)
        )
        os.makedirs("batches", exist_ok=True)
        batch_index = 0
        total_records = 0
        for batch_urls in url_chunks:
            if not batch_urls:
                continue
            batch_id = str(uuid.uuid4())
            input_file = f"batches/{batch_id}_input.json"
            with open(input_file, "w") as f:
                json.dump(batch_urls, f)
            await self.db.conn.execute(
                "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, job_id, batch_index, input_file, len(batch_urls), 'QUEUED', now, now)
            )
            if ledger:
                rows = []
                for offset, raw in enumerate(batch_urls, start=1):
                    url = str(raw).strip()
                    row_no = total_records + offset
                    if URLNormalizer.is_valid_http_url(url):
                        rows.append((job_id, row_no, batch_id, url, URLNormalizer.normalize(url), 'PENDING',
                                     'NEW_CHECK', None, None, None, None, None, None))
                    else:
                        rows.append((job_id, row_no, batch_id, url, None, 'DONE', 'NOT_VALIDATED',
                                     'INVALID_URL', 0, 'Invalid URL', 'Invalid URL',
                                     'Not a valid absolute http(s) URL; not requested', now))
                await self.db.insert_job_url_items(rows)
            total_records += len(batch_urls)
            batch_index += 1
        if not total_records:
            await self.db.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return job_id
        processed = 0
        if ledger:
            await self.db.mark_job_url_duplicates(job_id)
            async with self.db.conn.execute(
                "SELECT COUNT(*) FROM job_url_items WHERE job_id = ? AND item_state = 'DONE'", (job_id,)
            ) as cur:
                processed = (await cur.fetchone())[0]
        await self.db.conn.execute(
            "UPDATE jobs SET total_records = ?, processed_records = ?, status = 'QUEUED', updated_at = ? WHERE id = ?",
            (total_records, processed, datetime.utcnow().isoformat() + "Z", job_id),
        )
        return job_id

    async def create_job_from_sitemap(
        self,
        sitemap_import_id: str,
        mode: str = "link_audit",
        force_recheck: bool = False,
        selected_only: bool = True
    ) -> str:
        """
        Creates an audit job and worker batches directly from sitemap extracted URLs.
        Integrates seamlessly into the existing JobManager and BatchWorkerDaemon pipeline.
        """
        if selected_only:
            urls = await self.db.get_selected_urls_for_crawl(sitemap_import_id)
        else:
            # page through ALL extracted URLs (a single 100,000-row page silently dropped the rest)
            urls, page = [], 1
            while True:
                res = await self.db.get_extracted_urls(sitemap_import_id, page=page, page_size=50000)
                items = res.get("items") or []
                urls.extend(u["normalized_url"] for u in items if u.get("status") == "VALID")
                if len(items) < 50000:
                    break
                page += 1

        if not urls:
            raise ValueError("No eligible URLs selected for crawl.")

        max_records = getattr(settings, "LINK_AUDIT_MAX_URL_RECORDS", 20000) if mode == "link_audit" else settings.MAX_URLS_PER_JOB
        if len(urls) > max_records:
            # Never truncate silently: the caller (HTTP 400) tells the user exactly what was rejected.
            raise ValueError(
                f"{len(urls)} URLs selected, which exceeds the limit of {max_records} URLs per {mode} job. "
                f"Select fewer URLs or raise the limit."
            )

        upload_id = str(uuid.uuid4())
        filename = f"sitemap_{sitemap_import_id[:8]}.csv"

        # 1. Create upload record
        await self.create_upload(upload_id, filename, mode)

        # 2+3. Job, batches (and the per-URL ledger for url_status)
        batch_size = max(1, settings.BATCH_SIZE)
        chunks = (urls[i:i + batch_size] for i in range(0, len(urls), batch_size))
        job_id = await self._ingest_job(upload_id, "Sitemap URLs", mode, force_recheck, chunks)

        # 4. Link job to sitemap import
        await self.db.update_sitemap_import(sitemap_import_id, crawl_job_id=job_id)
        await self.db.conn.commit()

        return job_id

    async def get_jobs_for_upload(self, upload_id: str):
        async with self.db.conn.execute("SELECT * FROM jobs WHERE upload_id = ?", (upload_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def get_batches_for_job(self, job_id: str):
        async with self.db.conn.execute("SELECT * FROM batches WHERE job_id = ? ORDER BY batch_index", (job_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def get_all_jobs(self):
        async with self.db.conn.execute("SELECT j.*, u.filename, u.mode FROM jobs j JOIN uploads u ON j.upload_id = u.id ORDER BY j.created_at DESC") as cursor:
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def get_next_queued_batch(self, worker_id: str = None):
        # Claim a queued or expired-lease batch belonging to an active job
        now = datetime.utcnow().isoformat() + "Z"
        lease_duration = getattr(settings, 'BATCH_LEASE_DURATION_SEC', 60)
        lease_expires = (datetime.utcnow() + timedelta(seconds=lease_duration)).isoformat() + "Z"

        sql = """
            SELECT b.*, u.mode, j.force_recheck FROM batches b
            JOIN jobs j ON b.job_id = j.id
            JOIN uploads u ON j.upload_id = u.id
            WHERE (b.status = 'QUEUED' OR (b.status = 'RUNNING' AND (b.lease_expires_at IS NULL OR b.lease_expires_at < ?)))
              AND j.status IN ('QUEUED', 'RUNNING')
            ORDER BY b.created_at ASC LIMIT 25
        """
        async with self.db.conn.execute(sql, (now,)) as cursor:
            rows = await cursor.fetchall()
            if not rows:
                return None
            cols = [desc[0] for desc in cursor.description]
            candidates = [dict(zip(cols, row)) for row in rows]

        for batch in candidates:
            # Atomic claim check
            claim_sql = """
                UPDATE batches 
                SET status = 'RUNNING', worker_id = ?, lease_expires_at = ?, updated_at = ? 
                WHERE id = ? AND (status = 'QUEUED' OR (status = 'RUNNING' AND (lease_expires_at IS NULL OR lease_expires_at < ?)))
            """
            cur = await self.db.conn.execute(claim_sql, (worker_id, lease_expires, now, batch['id'], now))
            if cur.rowcount > 0:
                batch['worker_id'] = worker_id
                batch['lease_expires_at'] = lease_expires
                batch['status'] = 'RUNNING'

                # Also mark job as RUNNING if it was QUEUED
                await self.db.conn.execute("UPDATE jobs SET status = 'RUNNING', updated_at = ? WHERE id = ? AND status = 'QUEUED'", (now, batch['job_id']))
                await self.db.conn.commit()
                return batch

        return None

    async def update_batch_progress(self, batch_id: str, processed: int, status: str = None, error: str = None):
        now = datetime.utcnow().isoformat() + "Z"
        lease_duration = getattr(settings, 'BATCH_LEASE_DURATION_SEC', 60)
        lease_expires = (datetime.utcnow() + timedelta(seconds=lease_duration)).isoformat() + "Z" if not status or status == 'RUNNING' else None

        import asyncio
        for attempt in range(5):
            try:
                if status:
                    if status in ('COMPLETED', 'FAILED'):
                        await self.db.conn.execute(
                            "UPDATE batches SET processed_records = ?, status = ?, error = ?, updated_at = ?, lease_expires_at = NULL WHERE id = ?", 
                            (processed, status, error, now, batch_id)
                        )
                    else:
                        await self.db.conn.execute(
                            "UPDATE batches SET processed_records = ?, status = ?, error = ?, updated_at = ?, lease_expires_at = ? WHERE id = ?", 
                            (processed, status, error, now, lease_expires, batch_id)
                        )
                else:
                    await self.db.conn.execute(
                        "UPDATE batches SET processed_records = ?, updated_at = ?, lease_expires_at = ? WHERE id = ?", 
                        (processed, now, lease_expires, batch_id)
                    )
                await self.db.conn.commit()
                break
            except Exception as e:
                if attempt == 4: raise
                await asyncio.sleep(0.5)

        # Update Job totals
        await self._sync_job_status(batch_id)

    async def _sync_job_status(self, batch_id: str):
        # Get Job ID
        async with self.db.conn.execute("SELECT job_id FROM batches WHERE id = ?", (batch_id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return
            job_id = row[0]

        # Calculate totals
        async with self.db.conn.execute("SELECT SUM(processed_records), COUNT(*), SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) FROM batches WHERE job_id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return
            total_processed, total_batches, completed_batches, failed_batches = row
            
        now = datetime.utcnow().isoformat() + "Z"
        
        # Determine job status and mode
        async with self.db.conn.execute("SELECT j.status, u.mode FROM jobs j JOIN uploads u ON j.upload_id = u.id WHERE j.id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return
            job_status, mode = row[0], row[1]

        pending_items = 0
        if mode == "url_status" and await self.db.job_has_url_items(job_id):
            # Ledger is the source of truth: processed = rows with a final result or explicit reason
            done_items, pending_items = (await self.db.conn.execute_fetchall(
                "SELECT SUM(CASE WHEN item_state = 'DONE' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN item_state = 'PENDING' THEN 1 ELSE 0 END) FROM job_url_items WHERE job_id = ?",
                (job_id,),
            ))[0]
            total_processed = done_items or 0
            pending_items = pending_items or 0
            if pending_items and completed_batches + failed_batches == total_batches:
                # all batches finished but rows still pending: heal stranded duplicates before judging
                healed = await self.db.heal_pending_duplicates(job_id)
                if healed:
                    total_processed += healed
                    pending_items -= healed

        if job_status not in ['PAUSED', 'CANCELLED']:
            if completed_batches + failed_batches == total_batches:
                if failed_batches > 0 or pending_items > 0:
                    # never report COMPLETED while any submitted URL lacks a result
                    new_status = 'PARTIAL'
                else:
                    # Link Audit goes to VALIDATING. Others go to COMPLETED.
                    if mode == "link_audit":
                        new_status = 'VALIDATING'
                    else:
                        new_status = 'COMPLETED'
                await self.db.conn.execute("UPDATE jobs SET status = ?, processed_records = ?, updated_at = ? WHERE id = ?", (new_status, total_processed, now, job_id))
            else:
                await self.db.conn.execute("UPDATE jobs SET status = CASE WHEN status = 'QUEUED' THEN 'RUNNING' ELSE status END, processed_records = ?, updated_at = ? WHERE id = ?", (total_processed, now, job_id))
        
        await self.db.conn.commit()

    async def save_extracted_links(self, links: list):
        if not links: return
        sql = """
            INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
            VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
        """
        await self.db.conn.executemany(sql, links)
        await self.db.conn.commit()

    async def get_jobs_in_advanced_stages(self):
        sql = "SELECT jobs.id, jobs.status, uploads.mode, jobs.force_recheck FROM jobs JOIN uploads ON jobs.upload_id = uploads.id WHERE jobs.status IN ('CLASSIFYING', 'VALIDATING', 'FINALIZING')"
        async with self.db.conn.execute(sql) as cursor:
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def claim_next_advanced_job(self, worker_id: str = None):
        now = datetime.utcnow().isoformat() + "Z"
        lease_duration = getattr(settings, 'BATCH_LEASE_DURATION_SEC', 60)
        lease_expires = (datetime.utcnow() + timedelta(seconds=lease_duration)).isoformat() + "Z"

        sql = """
            SELECT jobs.id, jobs.status, uploads.mode, jobs.force_recheck 
            FROM jobs 
            JOIN uploads ON jobs.upload_id = uploads.id 
            WHERE jobs.status IN ('CLASSIFYING', 'VALIDATING', 'FINALIZING')
              AND (jobs.worker_id IS NULL OR jobs.worker_id = ? OR jobs.lease_expires_at IS NULL OR jobs.lease_expires_at < ?)
            ORDER BY jobs.created_at ASC LIMIT 25
        """
        async with self.db.conn.execute(sql, (worker_id, now)) as cursor:
            rows = await cursor.fetchall()
            if not rows:
                return None
            cols = [desc[0] for desc in cursor.description]
            candidates = [dict(zip(cols, row)) for row in rows]

        for job in candidates:
            claim_sql = """
                UPDATE jobs 
                SET worker_id = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                  AND (worker_id IS NULL OR worker_id = ? OR lease_expires_at IS NULL OR lease_expires_at < ?)
            """
            cur = await self.db.conn.execute(claim_sql, (worker_id, lease_expires, now, job['id'], job['status'], worker_id, now))
            if cur.rowcount > 0:
                job['worker_id'] = worker_id
                job['lease_expires_at'] = lease_expires
                await self.db.conn.commit()
                return job

        return None

    async def update_job_status(self, job_id: str, status: str):
        now = datetime.utcnow().isoformat() + "Z"
        import asyncio
        for attempt in range(5):
            try:
                if status in ('COMPLETED', 'FAILED', 'CANCELLED', 'PAUSED'):
                    await self.db.conn.execute(
                        "UPDATE jobs SET status = ?, worker_id = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                        (status, now, job_id)
                    )
                    if status == 'CANCELLED':
                        await self.db.conn.execute(
                            "UPDATE batches SET status = 'CANCELLED', updated_at = ?, lease_expires_at = NULL WHERE job_id = ? AND status IN ('QUEUED', 'RUNNING')",
                            (now, job_id)
                        )
                    elif status == 'PAUSED':
                        await self.db.conn.execute(
                            "UPDATE batches SET status = 'PAUSED', updated_at = ?, lease_expires_at = NULL WHERE job_id = ? AND status IN ('QUEUED', 'RUNNING')",
                            (now, job_id)
                        )
                elif status == 'QUEUED':
                    # Resuming paused job
                    await self.db.conn.execute(
                        "UPDATE jobs SET status = 'QUEUED', worker_id = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                        (now, job_id)
                    )
                    await self.db.conn.execute(
                        "UPDATE batches SET status = 'QUEUED', worker_id = NULL, lease_expires_at = NULL, updated_at = ? WHERE job_id = ? AND status = 'PAUSED'",
                        (now, job_id)
                    )
                    # URL-status jobs: resuming also retries FAILED batches. Safe because only rows still
                    # PENDING in the ledger are ever (re)validated; finished rows are never redone.
                    await self.db.conn.execute(
                        """UPDATE batches SET status = 'QUEUED', error = NULL, worker_id = NULL, lease_expires_at = NULL, updated_at = ?
                           WHERE job_id = ? AND status = 'FAILED'
                             AND EXISTS (SELECT 1 FROM job_url_items i WHERE i.job_id = batches.job_id)""",
                        (now, job_id)
                    )
                else:
                    await self.db.conn.execute(
                        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                        (status, now, job_id)
                    )
                await self.db.conn.commit()
                break
            except Exception as e:
                if attempt == 4: raise
                await asyncio.sleep(0.5)
