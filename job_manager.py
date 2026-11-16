import os
import uuid
import json
from itertools import chain, islice
import pandas as pd
import openpyxl
from datetime import datetime, timedelta
from config import settings
from db import Database

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
        if len(df.columns) > 0:
            first_col = df.columns[0]
            sample = df[first_col].dropna().astype(str).head(5)
            if any(s.startswith("http://") or s.startswith("https://") for s in sample):
                return first_col
        return None

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
            reader = pd.read_csv(filepath, chunksize=chunk_size)
            for frame in reader:
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
            columns = pd.Index(headers)
            sample = pd.DataFrame(sample_rows, columns=columns) if len(columns) else pd.DataFrame()
            url_col = self._find_url_col(sample)
            if url_col is None:
                return
            column_index = columns.get_loc(url_col)
            batch = []
            for row in chain(sample_rows, rows):
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
            job_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat() + "Z"
            await self.db.conn.execute(
                "INSERT INTO jobs (id, upload_id, sheet_name, total_records, status, created_at, updated_at, force_recheck) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, upload_id, sheet_name, 0, 'QUEUED', now, now, force_recheck)
            )

            batch_index = 0
            total_records = 0
            for batch_urls in self._iter_url_chunks(filepath, sheet_name):
                batch_id = str(uuid.uuid4())
                input_file = f"batches/{batch_id}_input.json"
                with open(input_file, "w") as f:
                    json.dump(batch_urls, f)
                await self.db.conn.execute(
                    "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, job_id, batch_index, input_file, len(batch_urls), 'QUEUED', now, now)
                )
                total_records += len(batch_urls)
                batch_index += 1
            if total_records:
                await self.db.conn.execute("UPDATE jobs SET total_records = ? WHERE id = ?", (total_records, job_id))
            else:
                await self.db.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self.db.conn.commit()

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
            res = await self.db.get_extracted_urls(sitemap_import_id, page=1, page_size=100000)
            urls = [u["normalized_url"] for u in res["items"] if u.get("status") == "VALID"]

        if not urls:
            raise ValueError("No eligible URLs selected for crawl.")

        max_records = getattr(settings, "LINK_AUDIT_MAX_URL_RECORDS", 20000) if mode == "link_audit" else settings.MAX_URLS_PER_JOB
        if len(urls) > max_records:
            urls = urls[:max_records]

        upload_id = str(uuid.uuid4())
        filename = f"sitemap_{sitemap_import_id[:8]}.csv"
        now = datetime.utcnow().isoformat() + "Z"

        # 1. Create upload record
        await self.create_upload(upload_id, filename, mode)

        # 2. Create job record
        job_id = str(uuid.uuid4())
        sheet_name = "Sitemap URLs"
        await self.db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, status, created_at, updated_at, force_recheck) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, sheet_name, len(urls), 'QUEUED', now, now, force_recheck)
        )

        # 3. Create batch chunks in batches/ folder
        os.makedirs("batches", exist_ok=True)
        batch_size = settings.BATCH_SIZE
        for i in range(0, len(urls), batch_size):
            batch_urls = urls[i:i + batch_size]
            batch_id = str(uuid.uuid4())
            batch_index = i // batch_size
            
            input_file = f"batches/{batch_id}_input.json"
            with open(input_file, "w") as f:
                json.dump(batch_urls, f)
            
            await self.db.conn.execute(
                "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, job_id, batch_index, input_file, len(batch_urls), 'QUEUED', now, now)
            )
            
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

        if job_status not in ['PAUSED', 'CANCELLED']:
            if completed_batches + failed_batches == total_batches:
                if failed_batches > 0:
                    new_status = 'PARTIAL'
                else:
                    # Link Audit goes to VALIDATING. Others go to COMPLETED.
                    if mode == "link_audit":
                        new_status = 'VALIDATING'
                    else:
                        new_status = 'COMPLETED'
                await self.db.conn.execute("UPDATE jobs SET status = ?, processed_records = ?, updated_at = ? WHERE id = ?", (new_status, total_processed, now, job_id))
            else:
                await self.db.conn.execute("UPDATE jobs SET processed_records = ?, updated_at = ? WHERE id = ?", (total_processed, now, job_id))
        
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
