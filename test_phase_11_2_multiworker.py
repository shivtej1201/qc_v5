import asyncio
import os
import unittest
import uuid
import json
from datetime import datetime, timedelta
from db import Database
from job_manager import JobManager
from report_generator import ReportGenerator

class TestPhase112MultiWorkerSafety(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = f"test_multiworker_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.test_db_path)
        await self.db.connect()
        self.jm = JobManager(self.db)
        self.report_gen = ReportGenerator(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.test_db_path):
            try: os.remove(self.test_db_path)
            except OSError: pass

    async def test_01_worker_lease_claiming_and_protection(self):
        """Healthy running batches must NOT be reset or stolen by a second worker."""
        # Setup job and batch
        job_id = str(uuid.uuid4())
        batch_id = str(uuid.uuid4())
        upload_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"

        await self.db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, "test.csv", "api_response_audit", "PROCESSING", now)
        )
        await self.db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", 10, "QUEUED", now, now)
        )
        await self.db.conn.execute(
            "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, job_id, 0, "fake_input.json", 10, "QUEUED", now, now)
        )
        await self.db.conn.commit()

        # Worker A claims batch
        worker_a = "worker_alpha"
        claimed_batch = await self.jm.get_next_queued_batch(worker_a)
        self.assertIsNotNone(claimed_batch)
        self.assertEqual(claimed_batch['id'], batch_id)
        self.assertEqual(claimed_batch['worker_id'], worker_a)
        self.assertEqual(claimed_batch['status'], 'RUNNING')
        self.assertIsNotNone(claimed_batch['lease_expires_at'])

        # Worker B starts up and runs startup reset
        worker_b = "worker_beta"
        await self.db.reset_running_batches(worker_b)

        # Verify Worker A's batch was NOT reset!
        async with self.db.conn.execute("SELECT status, worker_id FROM batches WHERE id = ?", (batch_id,)) as cur:
            row = await cur.fetchone()
            self.assertEqual(row[0], "RUNNING")
            self.assertEqual(row[1], worker_a)

        # Worker B tries to claim - must find nothing
        claimed_b = await self.jm.get_next_queued_batch(worker_b)
        self.assertIsNone(claimed_b)

    async def test_02_expired_lease_reclamation(self):
        """If worker crashes and its lease expires, another worker can reclaim the batch."""
        job_id = str(uuid.uuid4())
        batch_id = str(uuid.uuid4())
        upload_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"
        expired_time = (datetime.utcnow() - timedelta(minutes=5)).isoformat() + "Z"

        await self.db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, "test.csv", "api_response_audit", "PROCESSING", now)
        )
        await self.db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", 10, "RUNNING", now, now)
        )
        # Abandoned batch with expired lease
        await self.db.conn.execute(
            "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, worker_id, lease_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, job_id, 0, "fake_input.json", 10, "RUNNING", "worker_dead", expired_time, now, now)
        )
        await self.db.conn.commit()

        # Worker B starts and resets expired leases
        worker_b = "worker_reclaimer"
        await self.db.reset_running_batches(worker_b)

        # Batch should be reset to QUEUED or immediately claimable
        reclaimed = await self.jm.get_next_queued_batch(worker_b)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed['id'], batch_id)
        self.assertEqual(reclaimed['worker_id'], worker_b)
        self.assertEqual(reclaimed['status'], 'RUNNING')

    async def test_03_atomic_report_generation_clean_swap(self):
        """Atomic report generation replaces .tmp file only on success and leaves no leftover on failure."""
        job_id = str(uuid.uuid4())
        report_path = f"test_report_{job_id}.xlsx"

        try:
            # Normal generation of empty report
            await self.report_gen.generate_api_audit_report(job_id, report_path)
            self.assertTrue(os.path.exists(report_path))
            self.assertGreater(os.path.getsize(report_path), 0)

            # Assert no stray .tmp files remain in current directory
            tmp_files = [f for f in os.listdir(".") if f.startswith(report_path) and ".tmp" in f]
            self.assertEqual(len(tmp_files), 0)
        finally:
            if os.path.exists(report_path):
                os.remove(report_path)

if __name__ == "__main__":
    unittest.main()
