import asyncio
import os
import tempfile
from pathlib import Path

import openpyxl

from config import settings
from db import Database
from job_manager import JobManager
from report_generator import ReportGenerator


async def test_large_csv_upload_and_streaming_report():
    assert settings.MAX_URLS_PER_JOB >= 1_000_000
    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as temp_dir:
        os.chdir(temp_dir)
        try:
            source = Path("urls.csv")
            record_count = 25_001
            source.write_text(
                "url\n" + "\n".join(f"https://example.com/{i}" for i in range(record_count)),
                encoding="utf-8",
            )

            db = Database("smoke.db")
            await db.connect()
            try:
                manager = JobManager(db)
                sheets = await manager.parse_sheets(str(source))
                assert sheets == [{"name": "CSV Data", "count": record_count}]

                upload_id = "upload-smoke"
                await manager.create_upload(upload_id, source.name, "url_status")
                await manager.create_jobs_for_sheets(upload_id, str(source), "url_status", ["CSV Data"])
                jobs = await manager.get_jobs_for_upload(upload_id)
                assert len(jobs) == 1
                assert jobs[0]["total_records"] == record_count

                batches = await manager.get_batches_for_job(jobs[0]["id"])
                assert len(batches) == (record_count + settings.BATCH_SIZE - 1) // settings.BATCH_SIZE
                assert max(batch["total_records"] for batch in batches) <= settings.BATCH_SIZE

                await db.conn.execute(
                    "INSERT INTO url_results (normalized_url, final_status_code, status, checked_at) VALUES (?, ?, ?, ?)",
                    ("https://example.com/0", 200, "OK", "2026-01-01T00:00:00Z"),
                )
                output = "status.xlsx"
                await ReportGenerator(db).generate_report(jobs[0]["id"], "url_status", output)
                workbook = openpyxl.load_workbook(output, read_only=True, data_only=True)
                rows = list(workbook["URL Status"].iter_rows(values_only=True))
                workbook.close()
                assert len(rows) == 2
                assert rows[1][0] == "https://example.com/0"
            finally:
                await db.close()
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    asyncio.run(test_large_csv_upload_and_streaming_report())
    print("Large-upload streaming test passed")