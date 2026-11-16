import os
import sys
import time
import json
import asyncio
import hashlib
import uvicorn
import openpyxl
import pandas as pd
from datetime import datetime
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, Response, RedirectResponse
from starlette.routing import Route

from config import settings
from db import Database
from browser import BrowserPool
from api_observer import ApiResponseObserver
from report_generator import ReportGenerator
from url_normalizer import URLNormalizer
from job_manager import JobManager

# Mock server handlers
async def handle_home(request):
    return HTMLResponse("<html><body><h1>Home</h1><div id='content'>Content here</div></body></html>")

async def handle_redirect_to_ssrf(request):
    # 302 redirect to metadata IP
    return RedirectResponse(url="http://169.254.169.254/latest/meta-data", status_code=302)

async def handle_delayed_api_page(request):
    html = """
    <html><body>
    <h1>Delayed Products</h1>
    <div id="results">Waiting...</div>
    <script>
        setTimeout(() => {
            fetch('/api/delayed-data')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('results').innerText = 'Loaded ' + data.items.length + ' products';
                });
        }, 1000);
    </script>
    </body></html>
    """
    return HTMLResponse(html)

async def handle_delayed_data(request):
    await asyncio.sleep(0.8)
    return JSONResponse({"items": [{"id": 1, "name": "Laptop"}, {"id": 2, "name": "Phone"}]})

async def handle_nested_empty_page(request):
    html = """
    <html><body>
    <h1>Catalog Search</h1>
    <div id="results"></div>
    <script>
        fetch('/api/nested-empty').then(r => r.json());
    </script>
    </body></html>
    """
    return HTMLResponse(html)

async def handle_nested_empty_api(request):
    # Deeply nested empty JSON: {"response": {"data": {"items": []}, "pagination": {"total": 0}}}
    return JSONResponse({
        "status": "success",
        "response": {
            "pagination": {"total": 0, "page": 1},
            "data": {
                "items": []
            }
        }
    })

async def handle_nested_found_api(request):
    # Deeply nested found JSON
    return JSONResponse({
        "status": "success",
        "response": {
            "pagination": {"total": 5, "page": 1},
            "data": {
                "items": [{"id": 101, "title": "Headphones"}]
            }
        }
    })

async def handle_telemetry_fail_page(request):
    html = """
    <html><body>
    <h1>Product Page</h1>
    <div id="prod">iPhone 16 Pro - In Stock</div>
    <script>
        // Telemetry fails
        fetch('https://www.google-analytics.com/collect', {method: 'POST'}).catch(() => {});
        // Primary API succeeds
        fetch('/api/product-details').then(r => r.json());
    </script>
    </body></html>
    """
    return HTMLResponse(html)

async def handle_product_details_api(request):
    return JSONResponse({"productId": "IPH16", "status": "available", "price": 999})

async def handle_unusable_200_maintenance(request):
    html = """
    <html><head><title>Maintenance</title></head>
    <body>
        <h1>Under Maintenance</h1>
        <p>Our services are temporarily unavailable while we perform updates. Please check back later.</p>
    </body></html>
    """
    return HTMLResponse(html, status_code=200)

async def handle_unusable_200_empty(request):
    return HTMLResponse("<html><body>   \n\t  </body></html>", status_code=200)

async def run_hardening_tests():
    print("=" * 65)
    print("STARTING PHASE 11.1 PRODUCTION HARDENING & ACCURACY TEST SUITE")
    print("=" * 65)

    # 1. Start Mock Server
    app = Starlette(routes=[
        Route("/home", handle_home),
        Route("/redirect-to-ssrf", handle_redirect_to_ssrf),
        Route("/delayed-page", handle_delayed_api_page),
        Route("/api/delayed-data", handle_delayed_data),
        Route("/nested-empty-page", handle_nested_empty_page),
        Route("/api/nested-empty", handle_nested_empty_api),
        Route("/api/nested-found", handle_nested_found_api),
        Route("/telemetry-fail-page", handle_telemetry_fail_page),
        Route("/api/product-details", handle_product_details_api),
        Route("/maintenance", handle_unusable_200_maintenance),
        Route("/empty-dom", handle_unusable_200_empty),
    ])

    config = uvicorn.Config(app, host="127.0.0.1", port=8993, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)
    base_url = "http://127.0.0.1:8993"

    # Init dependencies
    test_db_file = f"test_phase11_1_{int(time.time())}.db"
    if os.path.exists(test_db_file):
        try: os.remove(test_db_file)
        except: pass

    db = Database(test_db_file)
    await db.connect()
    jm = JobManager(db)
    bp = BrowserPool()
    await bp.start()
    observer = ApiResponseObserver(bp)
    job_id = f"job-uat-{int(time.time())}"

    try:
        # TEST 1: SSRF Protection on Direct URLs, DNS, and Redirects
        print("\n--- [1] Testing SSRF Protection: Direct, DNS, and Redirects ---")
        # 1a. Direct Link-Local
        res_dir, _ = await observer.audit_page(job_id, "http://169.254.169.254/latest/meta-data", allow_local=False)
        assert res_dir.page_status == "SKIPPED", f"Direct SSRF expected SKIPPED, got {res_dir.page_status}"
        print("  [OK] Direct 169.254.169.254 blocked -> SKIPPED")

        # 1b. Loopback & Localhost when allow_local=False
        res_loop, _ = await observer.audit_page(job_id, "http://127.0.0.1:8000/internal-admin", allow_local=False)
        assert res_loop.page_status == "SKIPPED", f"Loopback expected SKIPPED, got {res_loop.page_status}"
        print("  [OK] Loopback 127.0.0.1 blocked -> SKIPPED")

        # 1c. Redirect to SSRF IP via browser navigation
        res_redir, _ = await observer.audit_page(job_id, f"{base_url}/redirect-to-ssrf", allow_local=True)
        assert res_redir.page_status == "SKIPPED", f"Redirect to SSRF expected SKIPPED, got {res_redir.page_status}"
        assert "SSRF" in res_redir.summary_reason or "blocked" in res_redir.summary_reason
        print("  [OK] 302 Redirect to SSRF target intercepted and aborted -> SKIPPED")

        # TEST 2: Sensitive Data Redaction
        print("\n--- [2] Testing Sensitive Data Redaction ---")
        raw_url = "https://example.com/checkout?token=xyz998877&auth=secret123&apiKey=ak_test_456&user=alice"
        redacted_url = observer.redact_url(raw_url)
        assert "xyz998877" not in redacted_url, "Token was not redacted from URL!"
        assert "secret123" not in redacted_url, "Auth secret was not redacted from URL!"
        assert "ak_test_456" not in redacted_url, "API key was not redacted from URL!"
        assert "alice" in redacted_url, "Non-sensitive parameter was incorrectly altered!"
        print(f"  [OK] URL Query Redaction: {redacted_url}")

        raw_snippet = '{"accessToken": "eyJhbGciOi...", "client_secret": "sec_8899", "status": "ok", "items": 10}'
        redacted_snippet = observer.redact_text(raw_snippet)
        assert "eyJhbGciOi" not in redacted_snippet
        assert "sec_8899" not in redacted_snippet
        assert '"status": "ok"' in redacted_snippet
        print(f"  [OK] Payload Snippet Redaction: {redacted_snippet}")

        # TEST 3: Classification Accuracy on Complex Page / API Behaviors
        print("\n--- [3] Testing Complex Page / API Classification ---")
        # 3a. Delayed API response
        res_delay, apis_delay = await observer.audit_page(job_id, f"{base_url}/delayed-page", allow_local=True)
        assert res_delay.page_status == "FOUND", f"Delayed API expected FOUND, got {res_delay.page_status}"
        assert len(apis_delay) >= 1, "Expected at least 1 API observed during dynamic settle"
        print(f"  [OK] Delayed API response dynamically settled -> FOUND ({len(apis_delay)} APIs observed)")

        # 3b. Deeply nested empty JSON
        res_nest, apis_nest = await observer.audit_page(job_id, f"{base_url}/nested-empty-page", allow_local=True)
        assert res_nest.page_status == "NOT_FOUND", f"Nested empty expected NOT_FOUND, got {res_nest.page_status}"
        assert "zero" in res_nest.summary_reason.lower() or "empty" in res_nest.summary_reason.lower()
        print(f"  [OK] Deeply nested empty JSON collection -> NOT_FOUND ({res_nest.summary_reason})")

        # 3c. Optional telemetry failure with valid page content
        res_tele, apis_tele = await observer.audit_page(job_id, f"{base_url}/telemetry-fail-page", allow_local=True)
        assert res_tele.page_status == "FOUND", f"Telemetry failure expected FOUND, got {res_tele.page_status}"
        print(f"  [OK] Optional telemetry failure ignored -> FOUND ({res_tele.summary_reason})")

        # 3d. HTTP 200 with Unusable Content: Maintenance page
        res_maint, _ = await observer.audit_page(job_id, f"{base_url}/maintenance", allow_local=True)
        assert res_maint.page_status == "PAGE_ERROR", f"Maintenance page expected PAGE_ERROR, got {res_maint.page_status}"
        assert "maintenance" in res_maint.summary_reason.lower()
        print(f"  [OK] HTTP 200 Maintenance Screen detected -> PAGE_ERROR ({res_maint.summary_reason})")

        # 3e. HTTP 200 with Empty DOM
        res_empty_dom, _ = await observer.audit_page(job_id, f"{base_url}/empty-dom", allow_local=True)
        assert res_empty_dom.page_status == "PAGE_ERROR", f"Empty DOM expected PAGE_ERROR, got {res_empty_dom.page_status}"
        print(f"  [OK] HTTP 200 Blank Body detected -> PAGE_ERROR ({res_empty_dom.summary_reason})")

        # TEST 4: Idempotent Database Writes & Progress Persistence
        print("\n--- [4] Testing Database Idempotency & Persistence ---")
        dict_maint = res_maint.to_db_dict()
        # Insert once
        await db.batch_insert_page_audit_results([dict_maint])
        # Insert second time (simulate retry / restart)
        dict_maint_retry = dict_maint.copy()
        dict_maint_retry["summary_reason"] = "Updated on retry"
        await db.batch_insert_page_audit_results([dict_maint_retry])

        # Verify only 1 row exists and reason was updated
        results = await db.get_page_audit_results(job_id)
        maint_rows = [r for r in results if r["page_url"] == dict_maint["page_url"]]
        assert len(maint_rows) == 1, f"Expected 1 idempotent row, found {len(maint_rows)}"
        assert maint_rows[0]["summary_reason"] == "Updated on retry"
        print("  [OK] page_audit_results upsert is 100% idempotent upon retry")

        # TEST 5: Duplicate Upload Guard
        print("\n--- [5] Testing Duplicate Upload Guard ---")
        upload_id = f"up-dup-{int(time.time())}"
        test_csv = "test_dup.csv"
        pd.DataFrame({"page_url": [f"{base_url}/home"]}).to_csv(test_csv, index=False)
        
        await db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, created_at) VALUES (?, ?, ?, ?)",
            (upload_id, test_csv, "api_response_audit", datetime.utcnow().isoformat() + "Z")
        )
        await db.conn.commit()

        # First call creates jobs
        jobs1 = await jm.create_jobs_for_sheets(upload_id, test_csv, "api_response_audit", ["CSV Data"])
        # Second call returns existing jobs without duplicate creation
        jobs2 = await jm.create_jobs_for_sheets(upload_id, test_csv, "api_response_audit", ["CSV Data"])
        all_jobs_for_upload = await jm.get_jobs_for_upload(upload_id)
        assert len(all_jobs_for_upload) == 1, f"Expected 1 job for upload, found {len(all_jobs_for_upload)}"
        print("  [OK] Repeated upload start call safely returns existing job without duplicates")
        if os.path.exists(test_csv): os.remove(test_csv)

        # TEST 6: Report Generation & Sanitization
        print("\n--- [6] Testing Multi-Sheet Report Generation & Clean References ---")
        report_file = f"reports/test_report_{job_id}.xlsx"
        rg = ReportGenerator(db)
        await rg.generate_api_audit_report(job_id, report_file)
        assert os.path.exists(report_file), "Report file was not generated"

        wb = openpyxl.load_workbook(report_file)
        assert wb.sheetnames == ["Summary", "Page Results", "API Responses", "Diagnostics"]
        ws_pages = wb["Page Results"]
        assert ws_pages["A1"].font.bold is True
        assert ws_pages.freeze_panes == "A2"
        assert ws_pages.auto_filter.ref is not None
        print("  [OK] All 4 sheets verified with Freeze A2, Auto-Filter, and Pro Header styling")

        if os.path.exists(report_file):
            try: os.remove(report_file)
            except: pass

        print("\n" + "=" * 65)
        print("ALL PHASE 11.1 HARDENING AND ACCURACY TESTS PASSED!")
        print("=" * 65)

    finally:
        await bp.stop()
        await db.close()
        server.should_exit = True
        await server_task
        for f in [test_db_file, f"{test_db_file}-wal", f"{test_db_file}-shm"]:
            try:
                if os.path.exists(f): os.remove(f)
            except: pass

if __name__ == "__main__":
    asyncio.run(run_hardening_tests())
