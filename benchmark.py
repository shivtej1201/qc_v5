import asyncio
import time
from db import Database
from job_manager import JobManager
from config import settings
from server import db, job_manager

async def run_benchmark():
    print("Starting Benchmark...")
    await db.connect()
    
    # Check if there are any completed jobs to benchmark
    jobs = await job_manager.get_all_jobs()
    if not jobs:
        print("No jobs found to benchmark. Please upload and run an Excel file first.")
        return
        
    print(f"Total jobs in system: {len(jobs)}")
    
    for job in jobs:
        job_id = job['id']
        mode = job['mode']
        
        async with db.conn.execute("SELECT created_at, updated_at FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            if not row: continue
            
        # SQLite returns string dates, let's parse them
        from dateutil.parser import isoparse
        try:
            start = isoparse(row[0])
            end = isoparse(row[1])
            duration = (end - start).total_seconds()
        except:
            duration = 1.0 # fallback
            
        if duration <= 0: duration = 1.0
            
        table = "image_results" if mode == "image_audit" else "url_results"
        sql = f"""
            SELECT 
                COUNT(*) as total_extracted,
                COUNT(DISTINCT e.normalized_url) as total_unique,
                SUM(CASE WHEN r.final_status_code IS NOT NULL THEN 1 ELSE 0 END) as total_checked
            FROM extracted_links e
            LEFT JOIN {table} r ON e.normalized_url = r.normalized_url
            WHERE e.job_id = ?
        """
        async with db.conn.execute(sql, (job_id,)) as cur:
            stats = dict(zip([d[0] for d in cur.description], await cur.fetchone()))
            
        print(f"\n--- Job: {job_id} ({mode}) ---")
        print(f"Total Source Pages / Records: {job['total_records']}")
        print(f"Total Extracted Links/Images: {stats['total_extracted']}")
        print(f"Total Unique Destinations: {stats['total_unique']}")
        print(f"Total Validated: {stats['total_checked']}")
        print(f"Total Runtime: {duration:.2f} seconds")
        ext_count = stats['total_extracted'] or 0
        chk_count = stats['total_checked'] or 0
        print(f"-> Extraction Throughput: {ext_count / duration:.2f} records/sec")
        print(f"-> Validation Throughput: {chk_count / duration:.2f} urls/sec")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
