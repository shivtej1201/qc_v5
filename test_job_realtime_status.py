import asyncio
import os
import sys
import uuid
import unittest
from datetime import datetime, timezone
import httpx

from server import app, db, job_manager
from config import settings

class TestJobRealTimeStatus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not db.conn:
            await db.connect()
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        self.created_job_ids = []
        self.created_upload_ids = []

    async def asyncTearDown(self):
        await self.client.aclose()
        if db.conn and self.created_job_ids:
            placeholders = ",".join("?" for _ in self.created_job_ids)
            await db.conn.execute(f"DELETE FROM batches WHERE job_id IN ({placeholders})", self.created_job_ids)
            await db.conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", self.created_job_ids)
            await db.conn.commit()
        if db.conn and self.created_upload_ids:
            placeholders = ",".join("?" for _ in self.created_upload_ids)
            await db.conn.execute(f"DELETE FROM uploads WHERE id IN ({placeholders})", self.created_upload_ids)
            await db.conn.commit()

    async def _get_job(self, job_id: str):
        async with db.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            cols = [d[0] for d in cur.description]
            row = await cur.fetchone()
            return dict(zip(cols, row)) if row else None

    async def _create_test_job(self, mode="link_audit", total_records=100, sheet_name="Sheet1"):
        now = datetime.now(timezone.utc).isoformat()
        u_id = f"test_up_{uuid.uuid4().hex[:8]}"
        self.created_upload_ids.append(u_id)
        await db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (u_id, "test.xlsx", mode, "RUNNING", now)
        )

        job_id = f"test_job_{uuid.uuid4().hex[:8]}"
        self.created_job_ids.append(job_id)
        await db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, u_id, sheet_name, total_records, 0, "QUEUED", now, now)
        )

        b_id = f"test_batch_{uuid.uuid4().hex[:8]}"
        await db.conn.execute(
            "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, processed_records, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b_id, job_id, 0, "test_input.json", total_records, 0, "QUEUED", now, now)
        )
        await db.conn.commit()
        return job_id, b_id, u_id

    async def test_01_job_creation_initial_queued_status(self):
        """Verify new job starts in QUEUED and is immediately visible across API endpoints."""
        job_id, b_id, _ = await self._create_test_job(total_records=50)

        # 1. Direct DB check
        job = await self._get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "QUEUED")
        self.assertEqual(job["processed_records"], 0)

        # 2. GET /api/jobs/{job_id}/stats API check
        resp = await self.client.get(f"/api/jobs/{job_id}/stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "QUEUED")
        self.assertEqual(data["job_id"], job_id)

        # 3. GET /api/jobs listing check
        resp_list = await self.client.get("/api/jobs")
        self.assertEqual(resp_list.status_code, 200)
        jobs = resp_list.json()["jobs"]
        matched = [j for j in jobs if j["id"] == job_id]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["status"], "QUEUED")

    async def test_02_worker_claim_transitions_job_to_running(self):
        """Verify worker claiming batch transitions job status from QUEUED to RUNNING in real time."""
        job_id, b_id, _ = await self._create_test_job(total_records=80)

        # Claim batch with worker
        worker_id = f"worker_{uuid.uuid4().hex[:6]}"
        claimed = await job_manager.get_next_queued_batch(worker_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], b_id)
        self.assertEqual(claimed["worker_id"], worker_id)
        self.assertEqual(claimed["status"], "RUNNING")

        # Verify parent job status is RUNNING
        job = await self._get_job(job_id)
        self.assertEqual(job["status"], "RUNNING")

        # Verify API reports RUNNING immediately
        resp = await self.client.get(f"/api/jobs/{job_id}/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "RUNNING")

    async def test_03_incremental_progress_updates(self):
        """Verify incremental progress updates update processed_records and maintain RUNNING status."""
        job_id, b_id, _ = await self._create_test_job(total_records=100)
        worker_id = f"test_worker_prog_{uuid.uuid4().hex[:6]}"
        await job_manager.get_next_queued_batch(worker_id)

        # Update progress incrementally
        await job_manager.update_batch_progress(b_id, 25)
        job = await self._get_job(job_id)
        self.assertEqual(job["processed_records"], 25)
        self.assertEqual(job["status"], "RUNNING")

        resp = await self.client.get(f"/api/jobs/{job_id}/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "RUNNING")

        # Another incremental update
        await job_manager.update_batch_progress(b_id, 60)
        job = await self._get_job(job_id)
        self.assertEqual(job["processed_records"], 60)
        self.assertEqual(job["status"], "RUNNING")

    async def test_04_batch_completion_transitions_job_to_next_stage(self):
        """Verify completing batch transitions parent job to VALIDATING (link_audit) or COMPLETED."""
        # Test with image_audit mode -> transitions directly to COMPLETED
        job_id, b_id, _ = await self._create_test_job(mode="image_audit", total_records=50)
        worker_id = f"test_worker_comp_{uuid.uuid4().hex[:6]}"
        await job_manager.get_next_queued_batch(worker_id)

        # Complete batch
        await job_manager.update_batch_progress(b_id, 50, status="COMPLETED")

        # Verify DB status
        job = await self._get_job(job_id)
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["processed_records"], 50)

        # Verify API reflects COMPLETED immediately
        resp = await self.client.get(f"/api/jobs/{job_id}/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "COMPLETED")

    async def test_05_cancellation_cascades_to_batches(self):
        """Verify cancelling a job updates job to CANCELLED and cascades to active/queued batches."""
        job_id, b_id, _ = await self._create_test_job(total_records=100)
        worker_id = f"test_worker_canc_{uuid.uuid4().hex[:6]}"
        await job_manager.get_next_queued_batch(worker_id)

        # Cancel job via API endpoint
        resp = await self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(resp.status_code, 200)

        # Verify job and batch status
        job = await self._get_job(job_id)
        self.assertEqual(job["status"], "CANCELLED")

        async with db.conn.execute("SELECT status FROM batches WHERE id = ?", (b_id,)) as cur:
            b_row = await cur.fetchone()
            self.assertEqual(b_row[0], "CANCELLED")

        # Verify stats endpoint returns CANCELLED
        resp = await self.client.get(f"/api/jobs/{job_id}/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "CANCELLED")

    async def test_06_pause_and_resume_lifecycle(self):
        """Verify pause and resume lifecycle operates cleanly across job and batch states."""
        job_id, b_id, _ = await self._create_test_job(total_records=100)
        worker_id = f"test_worker_pr_{uuid.uuid4().hex[:6]}"
        await job_manager.get_next_queued_batch(worker_id)

        # Pause
        resp = await self.client.post(f"/api/jobs/{job_id}/pause")
        self.assertEqual(resp.status_code, 200)
        job = await self._get_job(job_id)
        self.assertEqual(job["status"], "PAUSED")

        async with db.conn.execute("SELECT status FROM batches WHERE id = ?", (b_id,)) as cur:
            b_row = await cur.fetchone()
            self.assertEqual(b_row[0], "PAUSED")

        # Resume
        resp = await self.client.post(f"/api/jobs/{job_id}/resume")
        self.assertEqual(resp.status_code, 200)
        job = await self._get_job(job_id)
        self.assertEqual(job["status"], "QUEUED")

        async with db.conn.execute("SELECT status FROM batches WHERE id = ?", (b_id,)) as cur:
            b_row = await cur.fetchone()
            self.assertEqual(b_row[0], "QUEUED")

    async def test_07_concurrent_jobs_isolation(self):
        """Verify multiple active jobs maintain isolated progress and independent status transitions."""
        job_a, b_a, _ = await self._create_test_job(mode="image_audit", total_records=100, sheet_name="A")
        job_b, b_b, _ = await self._create_test_job(mode="image_audit", total_records=200, sheet_name="B")

        # Claim batch A
        await job_manager.get_next_queued_batch("worker_A")
        # Claim batch B
        await job_manager.get_next_queued_batch("worker_B")

        # Update progress only on A
        await job_manager.update_batch_progress(b_a, 40)

        # Complete A
        await job_manager.update_batch_progress(b_a, 100, status="COMPLETED")

        # Check job A
        resp_a = await self.client.get(f"/api/jobs/{job_a}/stats")
        self.assertEqual(resp_a.json()["status"], "COMPLETED")
        self.assertEqual(resp_a.json()["processed_records"], 100)

        # Check job B - must still be RUNNING with 0 processed
        resp_b = await self.client.get(f"/api/jobs/{job_b}/stats")
        self.assertEqual(resp_b.json()["status"], "RUNNING")
        self.assertEqual(resp_b.json()["processed_records"], 0)

    async def test_08_frontend_dom_resilience_and_session_contract(self):
        """Verify frontend contract fixes: explorer container protection, startData job_ids, and session restore."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # Verify explorer badge container prevents DOM destruction
        self.assertIn('id="explorer-status-badge-container"', html)
        self.assertIn("explorer-status-badge-container", html)

        # Verify selectJobForLinkAudit saves to sessionStorage
        self.assertIn("sessionStorage.setItem('selectedJobIdForExplorer', jobId)", html)

        # Verify startSelectedSheets opens explorer for first created job
        self.assertIn("startData.job_ids", html)
        self.assertIn("selectJobForLinkAudit(startData.job_ids[0])", html)

        # Verify pollSitemapCrawlProgress queries stats endpoint
        self.assertIn("/api/jobs/${currentSitemapCrawlJobId}/stats", html)

        # Verify session state restoration on DOMContentLoaded
        self.assertIn("sessionStorage.getItem('selectedJobIdForExplorer')", html)
        self.assertIn("sessionStorage.getItem('currentSitemapCrawlJobId')", html)
        self.assertIn("sessionStorage.getItem('currentSitemapImportId')", html)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestJobRealTimeStatus)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    os._exit(0 if res.wasSuccessful() else 1)
