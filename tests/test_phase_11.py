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
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route

from qcv5.core.config import settings
from qcv5.core.db import Database
from qcv5.crawler.browser import BrowserPool
from qcv5.crawler.api_observer import ApiResponseObserver
from qcv5.reports.report_generator import ReportGenerator

# 1. Mock web server providing controlled test fixtures
async def endpoint_home(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Success Page</title></head>
    <body>
        <h1>Product Catalog</h1>
        <div id="results">Loading...</div>
        <script>
            fetch('/api/products')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('results').innerText = 'Found: ' + data.items[0].name;
                });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def endpoint_api_products(request):
    # Sensitive token in response to verify redaction
    return JSONResponse({
        "items": [{"id": 101, "name": "Wireless Noise Cancelling Headphones", "price": 299}],
        "token": "secret_session_token_12345"
    })

async def endpoint_empty_search(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Search Page</title></head>
    <body>
        <h1>Search Results</h1>
        <div id="status">No products found</div>
        <script>
            fetch('/api/search?q=xyz&token=supersecret')
                .then(r => r.json())
                .then(data => {
                    if (data.total === 0) {
                        document.getElementById('status').innerText = '0 results found';
                    }
                });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def endpoint_api_search(request):
    return JSONResponse({"total": 0, "products": []})

async def endpoint_api_error_page(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>API Error Page</title></head>
    <body>
        <div id="container"></div>
        <script>
            fetch('/api/fatal-endpoint').catch(e => console.error(e));
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def endpoint_api_fatal(request):
    return Response(content="Internal Database Exception", status_code=500, media_type="text/plain")

async def endpoint_optional_api_failure(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Optional Failure Page</title></head>
    <body>
        <h1>Premium Smartphone 5G</h1>
        <p>In stock and ready to ship. Excellent battery life and display.</p>
        <script>
            // Non-critical tracking/telemetry API fails with 500
            fetch('/api/telemetry').catch(e => console.log('Optional failed'));
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def endpoint_api_telemetry(request):
    return Response(content="Telemetry server down", status_code=500, media_type="text/plain")

test_routes = [
    Route("/success", endpoint_home),
    Route("/api/products", endpoint_api_products),
    Route("/empty", endpoint_empty_search),
    Route("/api/search", endpoint_api_search),
    Route("/api-error", endpoint_api_error_page),
    Route("/api/fatal-endpoint", endpoint_api_fatal),
    Route("/optional-fail", endpoint_optional_api_failure),
    Route("/api/telemetry", endpoint_api_telemetry),
]

test_app = Starlette(routes=test_routes)

async def run_phase_11_tests():
    print("=" * 60)
    print("STARTING PHASE 11: PAGE & API RESPONSE AUDIT TEST SUITE")
    print("=" * 60)

    # Start local fixture test server
    config = uvicorn.Config(test_app, host="127.0.0.1", port=8991, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)
    base = "http://127.0.0.1:8991"

    # Initialize BrowserPool & DB
    bp = BrowserPool()
    await bp.start()
    
    test_db_file = "test_phase_11_cache.db"
    for f in [test_db_file, f"{test_db_file}-wal", f"{test_db_file}-shm"]:
        try:
            if os.path.exists(f): os.remove(f)
        except: pass

    db = Database(test_db_file)
    await db.connect()

    observer = ApiResponseObserver(bp)
    import uuid
    job_id = f"test-job-phase-11-{uuid.uuid4()}"
    batch_id = f"batch-11-{uuid.uuid4()}"

    test_cases = [
        ("Case 1: Working Page & Working API (FOUND)", f"{base}/success", "FOUND", 1),
        ("Case 2: Empty Result Page & API (NOT_FOUND)", f"{base}/empty", "NOT_FOUND", 1),
        ("Case 3: HTTP 404 Missing Page (NOT_FOUND)", f"{base}/non-existent-page-404", "NOT_FOUND", 0),
        ("Case 4: Critical API Error (API_ERROR)", f"{base}/api-error", "API_ERROR", 1),
        ("Case 5: Optional API Failure with Valid Page (FOUND)", f"{base}/optional-fail", "FOUND", 1),
        ("Case 6: SSRF Blocked URL (SKIPPED)", "http://169.254.169.254/latest/meta-data", "SKIPPED", 0),
    ]

    all_page_results = []
    all_api_responses = []

    for desc, url, expected_status, min_apis in test_cases:
        print(f"\n--- Running {desc} ---")
        is_local_fixture = (expected_status != "SKIPPED")
        p_res, apis = await observer.audit_page(job_id, url, batch_id, allow_local=is_local_fixture)
        
        print(f"  URL: {url}")
        print(f"  Outcome: {p_res.page_status} (Expected: {expected_status})")
        print(f"  HTTP Status: {p_res.http_status}")
        print(f"  APIs observed: {len(apis)} (Min required: {min_apis})")
        print(f"  Summary reason: {p_res.summary_reason}")
        if p_res.screenshot_path:
            print(f"  Screenshot captured: {p_res.screenshot_path} (Exists: {os.path.exists(p_res.screenshot_path)})")

        assert p_res.page_status == expected_status, f"Expected {expected_status}, got {p_res.page_status}"
        assert len(apis) >= min_apis, f"Expected at least {min_apis} APIs, got {len(apis)}"
        
        if expected_status in ["NOT_FOUND", "API_ERROR"]:
            if url != f"{base}/non-existent-page-404" or p_res.screenshot_path:
                if p_res.screenshot_path:
                    assert os.path.exists(p_res.screenshot_path), f"Screenshot file missing: {p_res.screenshot_path}"

        all_page_results.append(p_res.to_db_dict())
        for a in apis:
            all_api_responses.append(a.to_db_dict())

    print("\n[OK] All 6 controlled page & API cases evaluated correctly!")

    # 2. Test Redaction
    print("\n--- Testing Data Redaction ---")
    sensitive_sample = "Bearer eyJhbGciOiJIUzI1NiJ9.test and api_key=super_secret_12345"
    redacted = observer.redact_text(sensitive_sample)
    assert "super_secret_12345" not in redacted
    assert "[REDACTED]" in redacted
    url_with_key = "https://api.example.com/data?token=my_secret_token&user=john"
    redacted_url = observer.redact_url(url_with_key)
    assert "my_secret_token" not in redacted_url
    assert "token=%5BREDACTED%5D" in redacted_url or "token=[REDACTED]" in redacted_url
    print("[OK] Text and URL redaction verified safely!")

    # 3. Test Database Persistence
    print("\n--- Testing SQLite Persistence ---")
    await db.batch_insert_page_audit_results(all_page_results)
    if all_api_responses:
        await db.batch_insert_api_responses(all_api_responses)

    stored_pages = await db.get_page_audit_results(job_id)
    stored_apis = await db.get_page_api_responses(job_id)
    stats = await db.get_api_audit_stats(job_id)

    assert len(stored_pages) == len(test_cases)
    assert stats["total_pages"] == len(test_cases)
    assert stats["found"] == 2
    assert stats["not_found"] == 2
    assert stats["api_error"] == 1
    assert stats["skipped"] == 1
    print(f"[OK] Persisted {len(stored_pages)} pages and {len(stored_apis)} API observations in DB")
    print(f"     Stats: Total={stats['total_pages']}, FOUND={stats['found']}, NOT_FOUND={stats['not_found']}, API_ERROR={stats['api_error']}, SKIPPED={stats['skipped']}")

    # 4. Test Excel Report Generation
    print("\n--- Testing Multi-Sheet Excel Report Generation ---")
    report_file = "test_phase_11_report.xlsx"
    if os.path.exists(report_file):
        os.remove(report_file)

    rg = ReportGenerator(db)
    await rg.generate_api_audit_report(job_id, report_file)
    assert os.path.exists(report_file), "Excel report file was not generated"

    # Inspect Workbook
    wb = openpyxl.load_workbook(report_file)
    expected_sheets = ["Summary", "Page Results", "API Responses", "Diagnostics"]
    assert wb.sheetnames == expected_sheets, f"Expected sheets {expected_sheets}, got {wb.sheetnames}"
    print(f"[OK] Workbook created with sheets: {wb.sheetnames}")

    # Inspect Styles & Freeze Panes
    for sheet_name in expected_sheets:
        ws = wb[sheet_name]
        assert ws.freeze_panes == "A2", f"Sheet {sheet_name} missing freeze_panes A2 (got {ws.freeze_panes})"
        assert ws.auto_filter.ref is not None, f"Sheet {sheet_name} missing auto_filter.ref"
        assert ws["A1"].font.bold is True, f"Sheet {sheet_name} A1 font not bold"
        assert ws["A1"].fill.start_color.rgb == "004F81BD", f"Sheet {sheet_name} header fill mismatch"
        print(f"  [OK] Sheet '{sheet_name}': Freeze Panes A2, Auto-Filter, Pro Header Styling verified")

    # Read back with pandas
    df_sum = pd.read_excel(report_file, sheet_name="Summary")
    df_pg = pd.read_excel(report_file, sheet_name="Page Results")
    df_api = pd.read_excel(report_file, sheet_name="API Responses")
    df_diag = pd.read_excel(report_file, sheet_name="Diagnostics")

    assert len(df_sum) >= 10
    assert len(df_pg) == len(test_cases)
    assert len(df_diag) > 0
    print(f"[OK] Summary rows: {len(df_sum)}, Page rows: {len(df_pg)}, API rows: {len(df_api)}, Diagnostic rows: {len(df_diag)}")

    # 5. Clean up
    await bp.stop()
    await db.close()
    server.should_exit = True
    await server_task

    if os.path.exists(report_file):
        try: os.remove(report_file)
        except: pass

    for f in [test_db_file, f"{test_db_file}-wal", f"{test_db_file}-shm"]:
        try:
            if os.path.exists(f): os.remove(f)
        except: pass

    print("\n" + "=" * 60)
    print("ALL PHASE 11 TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_phase_11_tests())
