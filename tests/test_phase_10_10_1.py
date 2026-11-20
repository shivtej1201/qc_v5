import asyncio
import os
import sys
import uuid
import unittest
from datetime import datetime, timezone, timedelta
import httpx

from qcv5.api.server import app, db, job_manager
from qcv5.core.config import settings
from qcv5.crawler.url_normalizer import URLNormalizer

class TestPhase10101AcceptanceAndWakeLockVerification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not db.conn:
            await db.connect()
        
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        from qcv5.api.server import daemon
        if daemon:
            daemon.active = False
        if db.conn:
            await db.conn.execute("DELETE FROM batches WHERE job_id LIKE 'job_ov_%' OR job_id LIKE 'job_p10101_%'")
            await db.conn.execute("DELETE FROM jobs WHERE id LIKE 'job_ov_%' OR id LIKE 'job_p10101_%'")
            await db.conn.commit()

    async def test_01_overview_endpoint_aggregation_and_mode_distribution(self):
        """Verify GET /api/overview accurately aggregates all active/terminal statuses, denominators, and modes."""
        now = datetime.now(timezone.utc).isoformat()
        u_link = str(uuid.uuid4())
        u_api = str(uuid.uuid4())
        u_img = str(uuid.uuid4())

        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (u_link, "link.xlsx", "link_audit", "RUNNING", now))
        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (u_api, "api.xlsx", "api_response_audit", "COMPLETED", now))
        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (u_img, "img.csv", "image_audit", "COMPLETED", now))

        # Jobs with distinct statuses
        j_run = f"job_ov_run_{uuid.uuid4().hex[:6]}"
        j_que = f"job_ov_que_{uuid.uuid4().hex[:6]}"
        j_com = f"job_ov_com_{uuid.uuid4().hex[:6]}"
        j_pau = f"job_ov_pau_{uuid.uuid4().hex[:6]}"
        j_can = f"job_ov_can_{uuid.uuid4().hex[:6]}"

        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (j_run, u_link, "S1", 100, 40, "VALIDATING", now, now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (j_que, u_link, "S2", 50, 0, "QUEUED", now, now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (j_com, u_api, "S1", 20, 20, "COMPLETED", now, now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (j_pau, u_img, "S1", 30, 10, "PAUSED", now, now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (j_can, u_link, "S3", 10, 0, "CANCELLED", now, now))

        # Add destinations with 200 OK and Non-200
        dests = [
            {"job_id": j_run, "normalized_url": "https://example.com/dest_ok1", "final_status_code": 200, "status": "200 OK"},
            {"job_id": j_run, "normalized_url": "https://example.com/dest_ok2", "final_status_code": 200, "status": "200 OK"},
            {"job_id": j_run, "normalized_url": "https://example.com/dest_err", "final_status_code": 404, "status": "404 Not Found", "error_type": "HTTPError"},
            {"job_id": j_run, "normalized_url": "https://example.com/dest_tout", "final_status_code": None, "status": "Timeout", "error_type": "Timeout"}
        ]
        await db.batch_upsert_job_destinations(dests)
        await db.conn.commit()

        resp = await self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertGreaterEqual(data["total_jobs"], 5)
        self.assertGreaterEqual(data["running_jobs"], 1)
        self.assertGreaterEqual(data["queued_jobs"], 1)
        self.assertGreaterEqual(data["completed_jobs"], 1)
        self.assertGreaterEqual(data["paused_jobs"], 1)
        self.assertGreaterEqual(data["cancelled_jobs"], 1)
        self.assertGreaterEqual(data["total_submitted_urls"], 210)
        self.assertGreaterEqual(data["total_destinations_validated"], 4)
        self.assertGreaterEqual(data["total_200_ok"], 2)
        self.assertGreaterEqual(data["total_non_200"], 2)

    async def test_02_job_records_pagination_boundaries_and_clamping(self):
        """Verify GET /api/jobs/{id}/records handles page clamping, upper boundaries, and size limits."""
        job_id = f"job_p10101_bound_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "boundary.csv", "link_audit", "COMPLETED", now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 12, 12, "COMPLETED", now, now))

        links = []
        for i in range(12):
            dest = f"https://example.com/bound_{i}"
            links.append((job_id, "b0", f"https://example.com/src_{i}", dest, dest, f"Link {i}", "Nav", now))
        await db.conn.executemany("INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", links)
        await db.conn.commit()

        # 1. Negative or zero page clamped to 1
        r_neg = await self.client.get(f"/api/jobs/{job_id}/records?page=-3&page_size=5")
        self.assertEqual(r_neg.status_code, 200)
        d_neg = r_neg.json()
        self.assertEqual(d_neg["page"], 1)
        self.assertEqual(len(d_neg["records"]), 5)

        r_zero = await self.client.get(f"/api/jobs/{job_id}/records?page=0&page_size=5")
        self.assertEqual(r_zero.status_code, 200)
        self.assertEqual(r_zero.json()["page"], 1)

        # 2. Page beyond total pages returns empty records with valid total_pages metadata
        r_beyond = await self.client.get(f"/api/jobs/{job_id}/records?page=999&page_size=5")
        self.assertEqual(r_beyond.status_code, 200)
        d_beyond = r_beyond.json()
        self.assertEqual(d_beyond["total_records"], 12)
        self.assertEqual(d_beyond["total_pages"], 3)
        self.assertEqual(len(d_beyond["records"]), 0)

        # 3. Page size clamped between 1 and 500
        r_size_neg = await self.client.get(f"/api/jobs/{job_id}/records?page=1&page_size=-10")
        self.assertEqual(r_size_neg.status_code, 200)
        self.assertEqual(r_size_neg.json()["page_size"], 1)

        r_size_huge = await self.client.get(f"/api/jobs/{job_id}/records?page=1&page_size=10000")
        self.assertEqual(r_size_huge.status_code, 200)
        self.assertEqual(r_size_huge.json()["page_size"], 500)

    async def test_03_job_records_search_sanitization_and_status_filtering(self):
        """Verify search query parameter binding protects against injection and filters accurately."""
        job_id = f"job_p10101_filter_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "filter.csv", "link_audit", "COMPLETED", now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 6, 6, "COMPLETED", now, now))

        links = [
            (job_id, "b0", "https://example.com/page1", "https://example.com/ok", "https://example.com/ok", "Buy Now", "Hero", now),
            (job_id, "b0", "https://example.com/page2", "https://example.com/redir", "https://example.com/redir", "Contact Us", "Footer", now),
            (job_id, "b0", "https://example.com/page3", "https://example.com/err404", "https://example.com/err404", "Help Page", "Nav", now),
            (job_id, "b0", "https://example.com/page4", "https://example.com/err500", "https://example.com/err500", "Support", "Body", now),
            (job_id, "b0", "https://example.com/page5", "https://example.com/pending", "https://example.com/pending", "About", "Footer", now),
            (job_id, "b0", "https://example.com/page6", "https://example.com/inject'--", "https://example.com/inject'--", "Injection Test", "Nav", now),
        ]
        await db.conn.executemany("INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", links)

        dests = [
            {"job_id": job_id, "normalized_url": "https://example.com/ok", "final_status_code": 200, "status": "200 OK"},
            {"job_id": job_id, "normalized_url": "https://example.com/redir", "final_status_code": 301, "status": "301 Redirect"},
            {"job_id": job_id, "normalized_url": "https://example.com/err404", "final_status_code": 404, "status": "404 Not Found"},
            {"job_id": job_id, "normalized_url": "https://example.com/err500", "final_status_code": 500, "status": "500 Server Error"},
            {"job_id": job_id, "normalized_url": "https://example.com/inject'--", "final_status_code": 200, "status": "200 OK"},
        ]
        await db.batch_upsert_job_destinations(dests)
        await db.conn.commit()

        # Filter: 3XX redirect
        r_redir = await self.client.get(f"/api/jobs/{job_id}/records?status=3XX")
        self.assertEqual(len(r_redir.json()["records"]), 1)
        self.assertEqual(r_redir.json()["records"][0]["final_status_code"], 301)

        # Filter: 4XX client error
        r_4xx = await self.client.get(f"/api/jobs/{job_id}/records?status=4XX")
        self.assertEqual(len(r_4xx.json()["records"]), 1)
        self.assertEqual(r_4xx.json()["records"][0]["final_status_code"], 404)

        # Filter: 5XX server error
        r_5xx = await self.client.get(f"/api/jobs/{job_id}/records?status=5XX")
        self.assertEqual(len(r_5xx.json()["records"]), 1)
        self.assertEqual(r_5xx.json()["records"][0]["final_status_code"], 500)

        # Filter: PENDING
        r_pend = await self.client.get(f"/api/jobs/{job_id}/records?status=PENDING")
        self.assertEqual(len(r_pend.json()["records"]), 1)
        self.assertEqual(r_pend.json()["records"][0]["normalized_url"], "https://example.com/pending")

        # Search injection test
        r_inject = await self.client.get(f"/api/jobs/{job_id}/records?q=inject'--")
        self.assertEqual(r_inject.status_code, 200)
        self.assertEqual(len(r_inject.json()["records"]), 1)

    async def test_04_job_records_security_validation(self):
        """Verify strict regex format check on job_id prevents traversal and injections."""
        # Path traversal safely rejected by ASGI routing (404) or endpoint regex (400)
        r_trav = await self.client.get("/api/jobs/..%2F..%2Fpasswd/records")
        self.assertIn(r_trav.status_code, [400, 404])

        # Invalid characters triggering regex 400
        r_bad = await self.client.get("/api/jobs/job..invalid/records")
        self.assertEqual(r_bad.status_code, 400)

        # Special chars / SQL injection
        r_inj = await self.client.get("/api/jobs/job_123;DROP/records")
        self.assertEqual(r_inj.status_code, 400)

        # Nonexistent job
        r_404 = await self.client.get("/api/jobs/valid_id_that_does_not_exist/records")
        self.assertEqual(r_404.status_code, 404)

    async def test_05_separate_denominators_source_vs_destination(self):
        """Verify source extraction denominator and destination validation denominator remain independent."""
        job_id = f"job_p10101_denoms_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "denom.xlsx", "link_audit", "VALIDATING", now))
        # User uploaded 250 source URLs, 250 extracted
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 250, 250, "VALIDATING", now, now))

        # Extracted links: 1000 total links, but only 50 unique destinations
        ext = []
        for i in range(1000):
            dest = f"https://example.com/target_{i % 50}"
            ext.append((job_id, "b1", f"https://example.com/src_{i % 250}", dest, dest, "anchor", "nav", now))
        await db.conn.executemany("INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ext)

        # Validate 35 out of 50 unique destinations
        dests = [{"job_id": job_id, "normalized_url": f"https://example.com/target_{i}", "final_status_code": 200, "status": "200 OK"} for i in range(35)]
        await db.batch_upsert_job_destinations(dests)
        await db.conn.commit()

        stats = await db.get_link_audit_stats(job_id)
        # Validation denominator & progress
        self.assertEqual(stats["total_extracted"], 1000)
        self.assertEqual(stats["total_unique"], 50)
        self.assertEqual(stats["unique_validated"], 35)
        self.assertEqual(stats["pending"], 15)
        self.assertEqual(stats["duplicate_avoided"], 950)
        self.assertEqual(stats["dedup_ratio"], 95.0)

        # Source extraction denominator & progress
        async with db.conn.execute("SELECT total_records, processed_records FROM jobs WHERE id = ?", (job_id,)) as cur:
            j_row = await cur.fetchone()
            self.assertEqual(j_row[0], 250)
            self.assertEqual(j_row[1], 250)

    async def test_06_job_lifecycle_controls_state_transitions(self):
        """Verify pause, resume, and cancel transitions with idempotent handling."""
        job_id = f"job_p10101_life_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "life.xlsx", "link_audit", "RUNNING", now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 10, 2, "RUNNING", now, now))
        await db.conn.commit()

        # Pause
        r_p = await self.client.post(f"/api/jobs/{job_id}/pause")
        self.assertEqual(r_p.status_code, 200)
        async with db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            self.assertEqual((await cur.fetchone())[0], "PAUSED")

        # Resume
        r_r = await self.client.post(f"/api/jobs/{job_id}/resume")
        self.assertEqual(r_r.status_code, 200)
        async with db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            self.assertEqual((await cur.fetchone())[0], "QUEUED")

        # Cancel
        r_c = await self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(r_c.status_code, 200)
        async with db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            self.assertEqual((await cur.fetchone())[0], "CANCELLED")

    async def test_07_daemon_worker_respects_paused_and_cancelled_jobs(self):
        """Verify job_manager.get_next_queued_batch never claims batches for PAUSED or CANCELLED jobs."""
        job_p = f"job_p10101_jp_{uuid.uuid4().hex[:6]}"
        job_c = f"job_p10101_jc_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "pause_test.xlsx", "link_audit", "PROCESSING", now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_p, upload_id, "S1", 10, 0, "PAUSED", now, now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_c, upload_id, "S2", 10, 0, "CANCELLED", now, now))

        # Batches marked QUEUED
        bp_id = str(uuid.uuid4())
        bc_id = str(uuid.uuid4())
        await db.conn.execute("INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (bp_id, job_p, 0, "batches/dummy.json", 10, "QUEUED", now, now))
        await db.conn.execute("INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (bc_id, job_c, 0, "batches/dummy.json", 10, "QUEUED", now, now))
        await db.conn.commit()

        # Try claiming
        claimed = await job_manager.get_next_queued_batch("worker_test_1")
        # Neither batch from PAUSED or CANCELLED job should be claimed
        if claimed:
            self.assertNotIn(claimed["id"], [bp_id, bc_id])

    async def test_08_batch_lease_recovery_and_multiworker_safety(self):
        """Verify batches with expired worker leases are safely reclaimed without resetting healthy running jobs."""
        await db.conn.execute("UPDATE batches SET status = 'COMPLETED'")
        await db.conn.commit()

        job_id = f"job_p10101_lease_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat() + "Z"
        future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat() + "Z"

        await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "lease.xlsx", "link_audit", "PROCESSING", now))
        await db.conn.execute("INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, upload_id, "S1", 20, 0, "RUNNING", now, now))

        # Batch 1: healthy running batch with future lease
        b1_id = str(uuid.uuid4())
        await db.conn.execute("INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, worker_id, lease_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (b1_id, job_id, 0, "batches/dummy.json", 10, "RUNNING", "worker_alive", future, now, now))

        # Batch 2: abandoned running batch with expired lease
        b2_id = str(uuid.uuid4())
        old_created = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat() + "Z"
        await db.conn.execute("INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, worker_id, lease_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (b2_id, job_id, 1, "batches/dummy.json", 10, "RUNNING", "worker_crashed", past, old_created, now))
        await db.conn.commit()

        # Claim should recover expired batch (b2) and NOT touch active batch (b1)
        claimed = await job_manager.get_next_queued_batch("worker_recovery_1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], b2_id)
        self.assertEqual(claimed["worker_id"], "worker_recovery_1")

        # Verify batch 1 remains owned by worker_alive
        async with db.conn.execute("SELECT worker_id, status FROM batches WHERE id = ?", (b1_id,)) as cur:
            row1 = await cur.fetchone()
            self.assertEqual(row1[0], "worker_alive")
            self.assertEqual(row1[1], "RUNNING")

    async def test_09_screen_wake_lock_frontend_service_contract(self):
        """Verify frontend/index.html includes hardened ScreenWakeLockService with bounded retries and honest badges."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # Verify key methods and state guards are in source
        self.assertIn("hasActiveJobs()", html)
        self.assertIn("handleRelease(sentinel)", html)
        self.assertIn("maxReacquireAttempts: 3", html)
        self.assertIn("Screen Awake: Reacquiring...", html)
        self.assertIn("Screen Awake: Active", html)
        self.assertIn("Screen Awake: Standby", html)
        self.assertIn("Screen Awake: Off", html)
        self.assertIn("Screen Awake: Unsupported", html)
        self.assertIn("visibilitychange", html)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPhase10101AcceptanceAndWakeLockVerification)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    os._exit(0 if res.wasSuccessful() else 1)
