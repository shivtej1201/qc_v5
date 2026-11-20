import asyncio
import io
import json
import os
import unittest
import uuid
from datetime import datetime, timezone, timedelta
import openpyxl
import pandas as pd
from unittest.mock import patch, MagicMock

from qcv5.core.config import settings
from qcv5.core.db import Database
from qcv5.services.job_manager import JobManager
from qcv5.reports.report_generator import ReportGenerator, FILL_2XX, FILL_3XX, FILL_4XX, FILL_5XX, FILL_BLOCKED, PRIORITY_COLORS, SEVERITY_COLORS, STATUS_COLORS
from qcv5.services.daemon import BatchWorkerDaemon
from qcv5.core.ai_provider import sanitize_cell_for_export

class TestCorporateExcelAndParallelWorkers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = f"test_corp_workers_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.test_db_path)
        await self.db.connect()
        self.jm = JobManager(self.db)
        self.report_gen = ReportGenerator(self.db)
        self.temp_files = []

    async def asyncTearDown(self):
        await self.db.close()
        for f in self.temp_files:
            if os.path.exists(f):
                try: os.remove(f)
                except OSError: pass
        if os.path.exists(self.test_db_path):
            try: os.remove(self.test_db_path)
            except OSError: pass
        for extra in [f"{self.test_db_path}-wal", f"{self.test_db_path}-shm"]:
            if os.path.exists(extra):
                try: os.remove(extra)
                except OSError: pass

    async def _create_test_job(self, job_id: str, mode: str = "link_audit", status: str = "QUEUED") -> str:
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self.db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, "test_input.xlsx", mode, "PROCESSING", now)
        )
        await self.db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", 10, status, now, now)
        )
        await self.db.conn.commit()
        return upload_id

    async def test_01_configured_worker_count_and_daemon_init(self):
        """Worker count is configurable via WORKER_COUNT, overrides MAX_CONCURRENT_JOBS, and starts scalable pools."""
        # Default initialization
        daemon_default = BatchWorkerDaemon()
        self.assertEqual(daemon_default.worker_count, settings.WORKER_COUNT)

        # Custom explicit worker count (e.g. 8 workers)
        daemon_8 = BatchWorkerDaemon(worker_count=8, db=self.db, jm=self.jm)
        self.assertEqual(daemon_8.worker_count, 8)
        self.assertEqual(len(daemon_8.worker_tasks), 0)

    async def test_02_parallel_worker_execution_barrier_8_jobs(self):
        """Prove that 8 independent workers concurrently process 8 jobs simultaneously (> 3 limit)."""
        worker_count = 8
        daemon = BatchWorkerDaemon(worker_count=worker_count, db=self.db, jm=self.jm)

        # Create 8 jobs with 1 batch each
        job_ids = [f"job_par_{i}_{uuid.uuid4().hex[:6]}" for i in range(worker_count)]
        batch_ids = []
        now = datetime.now(timezone.utc).isoformat()

        for i, jid in enumerate(job_ids):
            uid = await self._create_test_job(jid, mode="url_status", status="QUEUED")
            bid = f"batch_{i}_{uuid.uuid4().hex[:6]}"
            batch_ids.append(bid)
            # Create a mock input file
            inp_file = f"inp_{i}_{uuid.uuid4().hex[:6]}.json"
            self.temp_files.append(inp_file)
            with open(inp_file, "w") as f:
                json.dump([f"https://example.com/page{i}"], f)

            await self.db.conn.execute(
                "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bid, jid, 0, inp_file, 1, "QUEUED", now, now)
            )
        await self.db.conn.commit()

        # Barrier to verify concurrency: all 8 workers must enter the active processing block concurrently
        concurrency_lock = asyncio.Lock()
        active_concurrent_workers = 0
        peak_concurrent_workers = 0
        barrier_event = asyncio.Event()

        async def mock_process_batch(batch, sub_worker_id=None):
            nonlocal active_concurrent_workers, peak_concurrent_workers
            async with concurrency_lock:
                active_concurrent_workers += 1
                if active_concurrent_workers > peak_concurrent_workers:
                    peak_concurrent_workers = active_concurrent_workers
                if active_concurrent_workers == worker_count:
                    barrier_event.set()

            # Wait until barrier is triggered (proving all 8 are executing simultaneously)
            try:
                await asyncio.wait_for(barrier_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

            # Mark batch completed
            await self.jm.update_batch_progress(batch['id'], processed=1, status="COMPLETED")

            async with concurrency_lock:
                active_concurrent_workers -= 1

        # Patch daemon's _process_batch_task
        daemon._process_batch_task = mock_process_batch
        from unittest.mock import AsyncMock
        daemon.browser_pool.start = AsyncMock()
        daemon.browser_pool.stop = AsyncMock()

        # Run daemon in background task
        daemon_task = asyncio.create_task(daemon.start())

        # Wait until all 8 batches are completed or timeout
        start_wait = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_wait < 10.0:
            async with self.db.conn.execute("SELECT COUNT(*) FROM batches WHERE status = 'COMPLETED'") as cur:
                row = await cur.fetchone()
                if row[0] == worker_count:
                    break
            await asyncio.sleep(0.1)

        daemon.stop()
        await asyncio.sleep(0.2)
        daemon_task.cancel()
        try:
            await daemon_task
        except asyncio.CancelledError:
            pass

        # Concurrency verification:
        self.assertEqual(peak_concurrent_workers, 8, f"Expected 8 concurrent workers, reached peak of {peak_concurrent_workers}")
        self.assertGreater(peak_concurrent_workers, 3, "Failed to remove the hardcoded 3-worker limit!")

        # Verify all 8 jobs and batches completed cleanly
        async with self.db.conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'COMPLETED'") as cur:
            row = await cur.fetchone()
            self.assertEqual(row[0], 8)

    async def test_03_zero_duplicate_claims_race_condition_test(self):
        """10 workers simultaneously attempting to claim 10 queued batches must result in 0 duplicate claims."""
        num_batches = 10
        job_id = f"job_race_{uuid.uuid4().hex[:6]}"
        await self._create_test_job(job_id, status="RUNNING")
        now = datetime.now(timezone.utc).isoformat()

        batch_ids = []
        for i in range(num_batches):
            bid = f"b_race_{i}_{uuid.uuid4().hex[:6]}"
            batch_ids.append(bid)
            await self.db.conn.execute(
                "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bid, job_id, i, f"dummy_{i}.json", 1, "QUEUED", now, now)
            )
        await self.db.conn.commit()

        # 10 workers simultaneously attempt atomic claims
        async def claim_batch(worker_id):
            return await self.jm.get_next_queued_batch(worker_id)

        workers = [f"worker_sim_{i}" for i in range(num_batches)]
        results = await asyncio.gather(*(claim_batch(w) for w in workers))

        claimed_batch_ids = [r['id'] for r in results if r is not None]
        claimed_workers = [r['worker_id'] for r in results if r is not None]

        # Every batch was claimed
        self.assertEqual(len(claimed_batch_ids), num_batches)
        # Exactly 0 duplicate batch claims
        self.assertEqual(len(set(claimed_batch_ids)), num_batches)
        # Exactly 10 distinct workers claimed
        self.assertEqual(len(set(claimed_workers)), num_batches)

    async def test_04_advanced_job_leasing_and_concurrency(self):
        """Atomic leasing on advanced stages (CLASSIFYING, VALIDATING, FINALIZING) prevents race conditions."""
        now = datetime.now(timezone.utc).isoformat()
        job_ids = [f"job_adv_{i}_{uuid.uuid4().hex[:6]}" for i in range(5)]
        for jid in job_ids:
            await self._create_test_job(jid, status="VALIDATING")

        # 5 workers claim simultaneously
        workers = [f"worker_adv_{i}" for i in range(5)]
        results = await asyncio.gather(*(self.jm.claim_next_advanced_job(w) for w in workers))

        claimed_jids = [r['id'] for r in results if r is not None]
        self.assertEqual(len(claimed_jids), 5)
        self.assertEqual(len(set(claimed_jids)), 5)

        # Confirm lease expiration and reset
        expired_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        await self.db.conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            (expired_time, job_ids[0])
        )
        await self.db.conn.commit()

        await self.db.reset_running_batches("worker_reclaimer")
        reclaimed = await self.jm.claim_next_advanced_job("worker_reclaimer")
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed['id'], job_ids[0])

    async def test_05_link_audit_corporate_excel_formatting(self):
        """Link Audit Excel export contains professional styling, clickable URLs, soft fills, Section G legend, and print setup."""
        job_id = f"job_excel_link_{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc).isoformat()
        await self._create_test_job(job_id, mode="link_audit", status="COMPLETED")

        # Insert extracted links
        links = [
            ("https://example.com/source1", "https://example.com/dest-200", 200, "Working", "PLP", "Loans"),
            ("https://example.com/source1", "https://example.com/dest-301", 301, "Redirect", "PLP", "Loans"),
            ("https://example.com/source2", "https://example.com/dest-404", 404, "Broken", "PDP", "Cards"),
            ("https://example.com/source3", "https://example.com/dest-500", 500, "Server Error", "PDP", "Cards"),
            ("https://example.com/source4", "https://example.com/dest-timeout", "TIMEOUT", "Timeout", "Home", "General"),
            ("https://example.com/source5", "http://169.254.169.254/meta", "BLOCKED", "Blocked", "Home", "General"),
        ]

        for s_url, d_url, code, stat, pt, biz in links:
            await self.db.conn.execute("""
                INSERT INTO extracted_links (job_id, source_url, normalized_url, original_url, component_name, anchor_text)
                VALUES (?, ?, ?, ?, 'HeaderNav', 'Click Here')
            """, (job_id, s_url, d_url, d_url))
            await self.db.conn.execute("""
                INSERT INTO job_destinations (job_id, normalized_url, final_status_code, status, response_time_ms, checked_at)
                VALUES (?, ?, ?, ?, 150, ?)
            """, (job_id, d_url, code if isinstance(code, int) else None, stat, now))
            await self.db.update_source_classification(s_url, pt, biz, "META_TAG")
        await self.db.conn.commit()

        # Generate report
        rep_file = f"test_link_rep_{uuid.uuid4().hex[:6]}.xlsx"
        self.temp_files.append(rep_file)
        await self.report_gen.generate_link_audit_report(job_id, rep_file)

        self.assertTrue(os.path.exists(rep_file))
        wb = openpyxl.load_workbook(rep_file)

        # Verify sheets
        expected_sheets = ["Pivot Summary", "Non-200 Responses", "Link Audit", "Remediations"]
        self.assertEqual(wb.sheetnames, expected_sheets)

        # 1. Inspect Pivot Summary
        ws_p = wb["Pivot Summary"]
        self.assertEqual(ws_p.freeze_panes, "A4")
        self.assertTrue(ws_p.views.sheetView[0].showGridLines)
        self.assertEqual(ws_p.page_setup.orientation, ws_p.ORIENTATION_LANDSCAPE)
        self.assertEqual(ws_p.page_setup.fitToWidth, 1)

        # Check Section G exists in Pivot Summary
        cell_values = [str(cell.value) for row in ws_p.iter_rows(values_only=False) for cell in row if cell.value is not None]
        self.assertTrue(any("SECTION G: STATUS & CATEGORY COLOR CODING LEGEND" in v for v in cell_values))

        # Check Section F has clickable hyperlinks
        found_f_link = False
        for row in ws_p.iter_rows(min_row=4, max_col=8):
            for cell in row:
                if cell.hyperlink and cell.hyperlink.target:
                    found_f_link = True
                    self.assertEqual(cell.font.color.rgb, "000563C1")
                    self.assertEqual(cell.font.underline, "single")
        self.assertTrue(found_f_link, "Expected clickable hyperlinks in Pivot Summary Section F")

        # 2. Inspect Non-200 Responses
        ws_non200 = wb["Non-200 Responses"]
        self.assertEqual(ws_non200.freeze_panes, "A2")
        self.assertTrue(ws_non200.auto_filter.ref.startswith("A1:"))
        self.assertEqual(ws_non200["A1"].font.bold, True)
        self.assertEqual(ws_non200["A1"].fill.start_color.rgb, "004F81BD")
        self.assertEqual(ws_non200.page_setup.orientation, ws_non200.ORIENTATION_LANDSCAPE)
        self.assertIn(ws_non200.print_title_rows, ("1:1", "$1:$1"))

        # Verify hyperlinks and status soft fills in Non-200 Responses
        dest_cell = ws_non200["B2"] # destination_url
        self.assertIsNotNone(dest_cell.hyperlink)
        self.assertEqual(dest_cell.font.color.rgb, "000563C1")

        status_cell = ws_non200["C2"] # http_status_code
        self.assertIsNotNone(status_cell.fill.start_color.rgb)

        # 3. Inspect Remediations sheet
        ws_rem = wb["Remediations"]
        self.assertEqual(ws_rem.freeze_panes, "A2")
        self.assertTrue(ws_rem.auto_filter.ref.startswith("A1:"))
        self.assertEqual(ws_rem.page_setup.orientation, ws_rem.ORIENTATION_LANDSCAPE)
        self.assertIn(ws_rem.print_title_rows, ("1:1", "$1:$1"))

        # Remediations target URL hyperlink
        rem_target_cell = ws_rem["G2"]
        if rem_target_cell.value and str(rem_target_cell.value).startswith("http"):
            self.assertIsNotNone(rem_target_cell.hyperlink)

    async def test_06_image_audit_corporate_excel_formatting(self):
        """Image Audit report features corporate headers, freeze panes, auto filters, clickable image URLs, and KB formatting."""
        job_id = f"job_img_{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc).isoformat()
        await self._create_test_job(job_id, mode="image_audit", status="COMPLETED")

        # Insert extracted image
        img_url = "https://example.com/assets/hero.png"
        src_url = "https://example.com/products"
        await self.db.conn.execute("""
            INSERT INTO extracted_links (job_id, source_url, normalized_url, original_url, image_name, loading_type)
            VALUES (?, ?, ?, ?, 'Hero Banner', 'eager')
        """, (job_id, src_url, img_url, img_url))
        await self.db.batch_insert_image_results([{
            "normalized_url": img_url,
            "initial_status_code": 200,
            "final_status_code": 200,
            "status": "Valid",
            "image_format": "PNG",
            "image_size_kb": 245.5,
            "is_dam_asset": 1,
            "source_platform": "AEM",
            "error_type": None,
            "error_message": None,
            "checked_at": now
        }])
        await self.db.conn.commit()

        rep_file = f"test_img_rep_{uuid.uuid4().hex[:6]}.xlsx"
        self.temp_files.append(rep_file)
        await self.report_gen.generate_report(job_id, "image_audit", rep_file)

        self.assertTrue(os.path.exists(rep_file))
        wb = openpyxl.load_workbook(rep_file)
        self.assertEqual(wb.sheetnames, ["Image Audit"])

        ws = wb["Image Audit"]
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertTrue(ws.auto_filter.ref.startswith("A1:"))
        self.assertEqual(ws["A1"].font.bold, True)
        self.assertEqual(ws["A1"].fill.start_color.rgb, "004F81BD")
        self.assertEqual(ws.page_setup.orientation, ws.ORIENTATION_LANDSCAPE)
        self.assertIn(ws.print_title_rows, ("1:1", "$1:$1"))

        # Hyperlinks on source_page_url and image_url
        src_cell = ws["A2"]
        self.assertIsNotNone(src_cell.hyperlink)
        self.assertEqual(src_cell.font.color.rgb, "000563C1")

        img_cell = ws["D2"]
        self.assertIsNotNone(img_cell.hyperlink)

        # Status code soft fill
        status_cell = ws["J2"] # http_status_code
        self.assertEqual(status_cell.fill.start_color.rgb, "00E2EFDA") # 200 soft green

    async def test_07_page_api_audit_corporate_excel_formatting(self):
        """Page & API Audit report features executive Summary, 4 formatted sheets, clickable URLs, and soft outcome badges."""
        job_id = f"job_api_{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc).isoformat()
        await self._create_test_job(job_id, mode="api_response_audit", status="COMPLETED")

        # Insert page & API audit records
        p_url = "https://example.com/checkout"
        api_url = "https://api.example.com/v1/cart"
        await self.db.batch_insert_page_audit_results([{
            "job_id": job_id,
            "batch_id": "b_test_01",
            "page_url": p_url,
            "final_url": p_url,
            "page_status": "FOUND",
            "http_status": 200,
            "response_time_ms": 320,
            "api_count": 1,
            "failed_api_count": 0,
            "summary_reason": "Page loaded successfully",
            "screenshot_path": None,
            "error_message": None,
            "evaluated_at": now
        }])
        await self.db.batch_insert_api_responses([{
            "job_id": job_id,
            "page_url": p_url,
            "request_url": api_url,
            "method": "POST",
            "resource_type": "fetch",
            "http_status": 200,
            "api_status": "FOUND",
            "response_time_ms": 110,
            "content_type": "application/json",
            "error_message": None,
            "response_snippet": '{"status":"ok"}',
            "observed_at": now
        }])

        rep_file = f"test_api_rep_{uuid.uuid4().hex[:6]}.xlsx"
        self.temp_files.append(rep_file)
        await self.report_gen.generate_api_audit_report(job_id, rep_file)

        self.assertTrue(os.path.exists(rep_file))
        wb = openpyxl.load_workbook(rep_file)
        self.assertEqual(wb.sheetnames, ["Summary", "Page Results", "API Responses", "Diagnostics"])

        # Check all 4 sheets have freeze panes and print setup
        for sname in wb.sheetnames:
            ws = wb[sname]
            self.assertEqual(ws.freeze_panes, "A2")
            self.assertTrue(ws.auto_filter.ref.startswith("A1:"))
            self.assertEqual(ws["A1"].font.bold, True)
            self.assertEqual(ws["A1"].fill.start_color.rgb, "004F81BD")
            self.assertEqual(ws.page_setup.orientation, ws.ORIENTATION_LANDSCAPE)
            self.assertIn(ws.print_title_rows, ("1:1", "$1:$1"))

        # Clickable URLs in Page Results
        ws_pg = wb["Page Results"]
        pg_url_cell = ws_pg["B2"] # page_url
        self.assertIsNotNone(pg_url_cell.hyperlink)
        self.assertEqual(pg_url_cell.font.color.rgb, "000563C1")

        # Soft badge for page_status = FOUND
        pg_stat_cell = ws_pg["D2"]
        self.assertEqual(pg_stat_cell.fill.start_color.rgb, "00E2EFDA")

    async def test_08_remediations_standalone_export(self):
        """Standalone Remediations export formats priority badges, severity fills, and clickable URLs."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Remediations"

        rems = [
            {
                "remediation_id": "REM-001",
                "priority": "P1",
                "priority_score": 95,
                "severity": "CRITICAL",
                "category": "BROKEN_LINK",
                "status": "OPEN",
                "target_url": "https://example.com/broken",
                "source_count": 12,
                "title": "Fix Broken Navigation Link",
                "recommended_action": "Update href to /active-page",
                "impact": "12 pages affected",
                "priority_rationale": "High traffic page",
                "review_notes": "Assigned to team"
            },
            {
                "remediation_id": "REM-002",
                "priority": "P2",
                "priority_score": 75,
                "severity": "HIGH",
                "category": "REDIRECT_CHAIN",
                "status": "RESOLVED",
                "target_url": "https://example.com/redirect",
                "source_count": 3,
                "title": "Shorten Redirect Chain",
                "recommended_action": "Point to canonical",
                "impact": "3 pages affected",
                "priority_rationale": "SEO latency",
                "review_notes": "Verified in staging"
            }
        ]

        self.report_gen._write_remediations_sheet(ws, rems)

        # Priority P1 soft red badge
        self.assertEqual(ws["B2"].fill.start_color.rgb, "00FADBD8")
        # Severity CRITICAL soft red badge
        self.assertEqual(ws["D2"].fill.start_color.rgb, "00FADBD8")
        # Target URL clickable hyperlink
        self.assertIsNotNone(ws["G2"].hyperlink)
        self.assertEqual(ws["G2"].font.color.rgb, "000563C1")

        # Priority P2 soft orange badge
        self.assertEqual(ws["B3"].fill.start_color.rgb, "00FCE4D6")
        # Status RESOLVED soft green badge
        self.assertEqual(ws["F3"].fill.start_color.rgb, "00E2EFDA")

        # Freeze panes & print setup
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertEqual(ws.page_setup.orientation, ws.ORIENTATION_LANDSCAPE)
        self.assertIn(ws.print_title_rows, ("1:1", "$1:$1"))

    async def test_09_formula_injection_defense_across_all_sheets(self):
        """Formula injection attempts in all string fields are safely escaped with single quote (')."""
        dangerous_inputs = ["=SUM(1,2)", "+cmd|' /C calc'!A0", "@evil()", "-1+1", "\t=EXEC()", "%0A=1+1"]

        for val in dangerous_inputs:
            sanitized = sanitize_cell_for_export(val)
            self.assertTrue(str(sanitized).startswith("'"), f"Failed to escape formula trigger: {val}")

        # Ensure normal URLs are preserved intact without prepending quote
        safe_urls = ["https://example.com/test-path?arg=1-2", "http://example.com/api/v1", "/relative-path/item-1"]
        for u in safe_urls:
            self.assertEqual(sanitize_cell_for_export(u), u)

if __name__ == "__main__":
    unittest.main()
