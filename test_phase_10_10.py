import asyncio
import os
import sys
import uuid
import unittest
from datetime import datetime, timezone
import httpx

from server import app, db, job_manager
from config import settings
from url_normalizer import URLNormalizer

class TestPhase1010ProfessionalUIAndMonitoring(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not db.conn:
            await db.connect()
        
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

        # Test auth headers if needed
        self.auth_headers = {
            getattr(settings, "PROXY_SECRET_HEADER", "X-Proxy-Secret"): "test-secret",
            getattr(settings, "USER_HEADER", "X-Remote-User"): "test_admin@example.com"
        }

    async def asyncTearDown(self):
        await self.client.aclose()
        from server import daemon
        if daemon:
            daemon.active = False

    async def test_01_static_frontend_serves_new_enterprise_ui(self):
        """Verify GET / returns the redesigned enterprise UI with screen-awake mode and sidebar."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("URL Auditor", html)
        self.assertIn("ScreenWakeLockService", html)
        self.assertIn("wakelock-toggle", html)
        self.assertIn("Overview Dashboard", html)
        self.assertIn("Link Audit (20K Records)", html)
        self.assertIn("Job History & Details", html)
        self.assertIn("System Health & DB", html)

    async def test_02_overview_endpoint_aggregates_kpis(self):
        """Verify GET /api/overview returns aggregated active jobs, counts, and mode distributions."""
        resp = await self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("total_jobs", data)
        self.assertIn("running_jobs", data)
        self.assertIn("completed_jobs", data)
        self.assertIn("total_submitted_urls", data)
        self.assertIn("total_destinations_validated", data)
        self.assertIn("timestamp", data)

    async def test_03_paginated_records_endpoint_and_filters(self):
        """Verify GET /api/jobs/{job_id}/records supports pagination, search query, and status filters."""
        # Create a test upload and job
        upload_id = str(uuid.uuid4())
        job_id = f"job_p1010_test_{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, "pagination_test.csv", "link_audit", "COMPLETED", now)
        )
        await db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", 10, 10, "COMPLETED", now, now)
        )

        # Insert 15 extracted links with 5 unique destinations
        extracted = []
        for i in range(15):
            dest = f"https://example.com/dest_{i % 5}"
            norm = URLNormalizer.normalize(dest)
            extracted.append((
                job_id, f"batch_0", f"https://example.com/src_{i}", dest, norm, f"Anchor {i}", "Grid", now
            ))
        await db.conn.executemany("""
            INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, extracted)

        # Upsert destinations with various status codes: 200, 404, 500, 301
        dest_records = [
            {"job_id": job_id, "normalized_url": "https://example.com/dest_0", "final_status_code": 200, "status": "200 OK"},
            {"job_id": job_id, "normalized_url": "https://example.com/dest_1", "final_status_code": 404, "status": "404 Not Found", "error_type": "HTTPError"},
            {"job_id": job_id, "normalized_url": "https://example.com/dest_2", "final_status_code": 500, "status": "500 Server Error", "error_type": "ServerError"},
            {"job_id": job_id, "normalized_url": "https://example.com/dest_3", "final_status_code": 301, "status": "301 Redirect", "final_url": "https://example.com/dest_target"},
            {"job_id": job_id, "normalized_url": "https://example.com/dest_4", "final_status_code": 200, "status": "200 OK"},
        ]
        await db.batch_upsert_job_destinations(dest_records)
        await db.conn.commit()

        # 1. Test pagination page 1 (size 5)
        resp1 = await self.client.get(f"/api/jobs/{job_id}/records?page=1&page_size=5")
        self.assertEqual(resp1.status_code, 200)
        d1 = resp1.json()
        self.assertEqual(d1["total_records"], 15)
        self.assertEqual(d1["total_pages"], 3)
        self.assertEqual(len(d1["records"]), 5)

        # 2. Test status filter = 200 (should match records with dest_0 and dest_4)
        resp_200 = await self.client.get(f"/api/jobs/{job_id}/records?status=200")
        self.assertEqual(resp_200.status_code, 200)
        d_200 = resp_200.json()
        self.assertTrue(all(r["final_status_code"] == 200 for r in d_200["records"]))

        # 3. Test status filter = NON_200 (should match dest_1, dest_2, dest_3)
        resp_non200 = await self.client.get(f"/api/jobs/{job_id}/records?status=NON_200")
        self.assertEqual(resp_non200.status_code, 200)
        d_non200 = resp_non200.json()
        self.assertTrue(all(r["final_status_code"] != 200 for r in d_non200["records"]))

        # 4. Test query search (e.g., search for "src_3")
        resp_q = await self.client.get(f"/api/jobs/{job_id}/records?q=src_3")
        self.assertEqual(resp_q.status_code, 200)
        d_q = resp_q.json()
        self.assertEqual(d_q["total_records"], 1)
        self.assertEqual(d_q["records"][0]["source_url"], "https://example.com/src_3")

        # 5. Invalid job ID validation
        resp_invalid = await self.client.get("/api/jobs/invalid..job/records")
        self.assertEqual(resp_invalid.status_code, 400)

    async def test_04_accurate_progress_denominators(self):
        """Verify extraction progress uses total_records and validation progress uses total_unique."""
        job_id = f"job_p1010_denoms_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, "denom_test.xlsx", "link_audit", "VALIDATING", now)
        )
        # 100 source records submitted, 50 processed so far
        await db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", 100, 50, "VALIDATING", now, now)
        )

        # 80 extracted links, but only 20 unique destinations
        ext = []
        for i in range(80):
            u = f"https://example.com/u_{i % 20}"
            ext.append((job_id, "b0", f"https://example.com/s_{i}", u, u, "anch", "grid", now))
        await db.conn.executemany("""
            INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ext)

        # Validate 15 out of the 20 unique destinations
        dests = [{"job_id": job_id, "normalized_url": f"https://example.com/u_{i}", "final_status_code": 200, "status": "200 OK"} for i in range(15)]
        await db.batch_upsert_job_destinations(dests)
        await db.conn.commit()

        # Query stats
        stats = await db.get_link_audit_stats(job_id)
        
        # Check that extraction denominator is source-based
        self.assertEqual(stats["total_extracted"], 80)
        self.assertEqual(stats["total_unique"], 20)
        self.assertEqual(stats["unique_validated"], 15)
        self.assertEqual(stats["pending"], 5)
        self.assertEqual(stats["duplicate_avoided"], 60)
        self.assertEqual(stats["dedup_ratio"], 75.0)

        # Fetch job record to verify source progress denominator
        async with db.conn.execute("SELECT total_records, processed_records FROM jobs WHERE id = ?", (job_id,)) as cur:
            j_row = await cur.fetchone()
            self.assertEqual(j_row[0], 100) # Source denominator
            self.assertEqual(j_row[1], 50)  # Source processed

    async def test_05_job_lifecycle_controls_pause_resume_cancel(self):
        """Verify job pause, resume, and cancel transitions work reliably."""
        job_id = f"job_p1010_lifecycle_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, "lifecycle.csv", "link_audit", "RUNNING", now)
        )
        await db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", 10, 5, "RUNNING", now, now)
        )
        await db.conn.commit()

        # 1. Pause
        r_pause = await self.client.post(f"/api/jobs/{job_id}/pause")
        self.assertEqual(r_pause.status_code, 200)
        async with db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            self.assertEqual((await cur.fetchone())[0], "PAUSED")

        # 2. Resume
        r_resume = await self.client.post(f"/api/jobs/{job_id}/resume")
        self.assertEqual(r_resume.status_code, 200)
        async with db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            self.assertEqual((await cur.fetchone())[0], "QUEUED")

        # 3. Cancel
        r_cancel = await self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(r_cancel.status_code, 200)
        async with db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            self.assertEqual((await cur.fetchone())[0], "CANCELLED")

    async def test_06_reverse_proxy_auth_enforced_on_new_endpoints(self):
        """Ensure /api/overview and /api/jobs/{id}/records require authentication when AUTH_REQUIRED=True."""
        try:
            settings.AUTH_REQUIRED = True
            settings.PROXY_SECRET_VALUE = "auth-p1010-secret"
            settings.PROXY_SECRET_HEADER = "X-Proxy-Secret"
            settings.USER_HEADER = "X-Remote-User"

            # Without auth headers -> 401 or 403
            resp_no_auth = await self.client.get("/api/overview")
            self.assertIn(resp_no_auth.status_code, [401, 403])

            resp_rec_no_auth = await self.client.get("/api/jobs/job_123/records")
            self.assertIn(resp_rec_no_auth.status_code, [401, 403])

            # With valid auth headers -> 200
            valid_headers = {
                "X-Proxy-Secret": "auth-p1010-secret",
                "X-Remote-User": "admin@example.com"
            }
            resp_authed = await self.client.get("/api/overview", headers=valid_headers)
            self.assertEqual(resp_authed.status_code, 200)
        finally:
            settings.AUTH_REQUIRED = False
            settings.PROXY_SECRET_VALUE = ""

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPhase1010ProfessionalUIAndMonitoring)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    os._exit(0 if res.wasSuccessful() else 1)
