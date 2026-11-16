import os
import sys
import time
import asyncio
import uuid
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from db import Database
from job_manager import JobManager
from browser import BrowserPool
from api_observer import ApiResponseObserver
from daemon import BatchWorkerDaemon

# Local mock server on port 8994
async def endpoint_item_page(request):
    item_id = request.path_params.get("item_id", "0")
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><title>Product {item_id}</title></head>
    <body>
        <h1>Product #{item_id}</h1>
        <p>Verified item in inventory.</p>
        <script>
            fetch('/api/item/{item_id}').then(r => r.json());
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def endpoint_item_api(request):
    item_id = request.path_params.get("item_id", "0")
    return JSONResponse({"item_id": item_id, "status": "active", "stock": 10})

routes = [
    Route("/page/{item_id}", endpoint_item_page),
    Route("/api/item/{item_id}", endpoint_item_api),
]

app = Starlette(routes=routes)

async def run_crash_recovery_test():
    print("=" * 65)
    print("PHASE 11.1.1: LIVE CRASH RECOVERY & IDEMPOTENCY VERIFICATION")
    print("=" * 65)

    test_db_file = f"test_crash_{uuid.uuid4().hex[:8]}.db"

    # Start mock server
    config = uvicorn.Config(app, host="127.0.0.1", port=8994, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)
    base = "http://127.0.0.1:8994"

    db = Database(test_db_file)
    await db.connect()
    jm = JobManager(db)

    # 1. Create a 20-page Audit Job
    job_id = f"crash-test-job-{uuid.uuid4()}"
    urls = [f"{base}/page/{i}" for i in range(1, 21)]
    
    # Insert upload & job
    upload_id = str(uuid.uuid4())
    await db.conn.execute("INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, 'COMPLETED', CURRENT_TIMESTAMP)",
                         (upload_id, "crash_test.csv", "api_response_audit"))
    await db.conn.execute("INSERT INTO jobs (id, upload_id, status) VALUES (?, ?, 'QUEUED')", (job_id, upload_id))
    
    # Create single batch of 20 URLs
    import json
    batch_id = str(uuid.uuid4())
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    input_file = f"batches/{batch_id}_input.json"
    os.makedirs("batches", exist_ok=True)
    with open(input_file, "w") as f:
        json.dump(urls, f)
    await db.conn.execute(
        "INSERT INTO batches (id, job_id, batch_index, input_file, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, job_id, 0, input_file, len(urls), 'QUEUED', now, now)
    )
    await db.conn.commit()
    print(f"Created audit job {job_id} with 20 pages in batch {batch_id}")

    # 2. Start Worker 1 and Simulate Sudden Termination after 5 Pages
    print("\n--- Phase A: Processing Initial Pages and Simulating Mid-Batch Crash ---")
    bp1 = BrowserPool()
    await bp1.start()
    observer1 = ApiResponseObserver(bp1)
    
    # Mark batch RUNNING
    await db.conn.execute("UPDATE batches SET status = 'RUNNING' WHERE id = ?", (batch_id,))
    await db.conn.execute("UPDATE jobs SET status = 'RUNNING' WHERE id = ?", (job_id,))
    await db.conn.commit()

    # Process first 5 pages manually
    for i in range(5):
        u = urls[i]
        p_res, apis = await observer1.audit_page(job_id, u, batch_id, allow_local=True)
        await db.batch_insert_page_audit_results([p_res.to_db_dict()])
        if apis:
            await db.batch_insert_api_responses([a.to_db_dict() for a in apis])
        await jm.update_batch_progress(batch_id, i + 1)
        print(f"  Processed page {i+1}/20: {u}")

    print("  SIMULATING SUDDEN WORKER CRASH / KILL EVENT NOW!")
    # Abruptly stop worker 1 without updating batch status
    await bp1.stop()

    # Verify state after crash
    audited_after_crash = await db.get_audited_page_urls(job_id)
    print(f"  Pages in DB immediately post-crash: {len(audited_after_crash)} (Expected: 5)")
    assert len(audited_after_crash) == 5, f"Expected 5 audited pages, found {len(audited_after_crash)}"

    # 3. Simulate Daemon Startup Recovery
    print("\n--- Phase B: Daemon Startup Recovery & Batch Resumption ---")
    # Daemon startup hook resets RUNNING batches and jobs to QUEUED
    await db.reset_running_batches()
    
    async with db.conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)) as cur:
        b_row = await cur.fetchone()
        assert b_row[0] == "QUEUED", f"Expected batch status QUEUED, got {b_row[0]}"
        print("  [PASS] Startup hook successfully recovered crashed batch back to QUEUED")

    async with db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
        j_row = await cur.fetchone()
        assert j_row[0] == "QUEUED", f"Expected job status QUEUED, got {j_row[0]}"
        print("  [PASS] Startup hook successfully recovered crashed job back to QUEUED")

    # 4. Start Worker 2 (Resumed Daemon)
    bp2 = BrowserPool()
    await bp2.start()
    daemon2 = BatchWorkerDaemon()
    daemon2.db = db
    daemon2.jm = jm
    daemon2.browser_pool = bp2
    daemon2.api_observer = ApiResponseObserver(bp2)
    # Enable allow_local for mock test
    orig_audit = daemon2.api_observer.audit_page
    daemon2.api_observer.audit_page = lambda j, u, b: orig_audit(j, u, b, allow_local=True)

    print("  Resumed daemon processing batch...")
    batch_dict = await jm.get_next_queued_batch()
    assert batch_dict is not None, "Batch claiming failed"
    await daemon2._process_batch(batch_dict)

    # 5. Verify Full Completion & Invariants
    print("\n--- Phase C: Invariance & Idempotency Assertions ---")
    audited_final = await db.get_page_audit_results(job_id)
    apis_final = await db.get_page_api_responses(job_id)
    
    print(f"  Final Total Page Audit Records: {len(audited_final)} / 20")
    print(f"  Final Total Observed APIs: {len(apis_final)} / 20")
    assert len(audited_final) == 20, f"Expected exactly 20 pages, found {len(audited_final)}"
    assert len(apis_final) == 20, f"Expected exactly 20 APIs, found {len(apis_final)}"

    # Check distinct URLs
    distinct_urls = {p['page_url'] for p in audited_final}
    assert len(distinct_urls) == 20, f"Duplicate page entries detected: {len(distinct_urls)}"
    print("  [PASS] Exactly 20 distinct pages recorded without duplicates")

    # 6. Idempotent Retry Test: Re-run the same batch
    print("\n--- Phase D: Idempotent Double-Execution Test ---")
    await daemon2._process_batch(batch_dict)
    
    audited_after_rerun = await db.get_page_audit_results(job_id)
    apis_after_rerun = await db.get_page_api_responses(job_id)
    assert len(audited_after_rerun) == 20, f"Re-run generated duplicate pages: {len(audited_after_rerun)}"
    assert len(apis_after_rerun) == 20, f"Re-run generated duplicate APIs: {len(apis_after_rerun)}"
    print("  [PASS] Re-running batch is 100% idempotent with zero duplicate rows")

    await bp2.stop()
    await db.close()
    server.should_exit = True
    await asyncio.sleep(0.5)

    print("\n" + "=" * 65)
    print("ALL LIVE CRASH RECOVERY & IDEMPOTENCY TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 65)

if __name__ == "__main__":
    asyncio.run(run_crash_recovery_test())
