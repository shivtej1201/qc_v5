import os
import sys
import time
import json
import urllib.request
import urllib.parse
import openpyxl
import pandas as pd

API_BASE = "http://127.0.0.1:8002"

def main():
    print("=" * 60)
    print("RUNNING LIVE END-TO-END PIPELINE AUDIT (PHASE 11)")
    print("=" * 60)

    # 1. Create a CSV file with test URLs
    csv_filename = "test_pages_audit_input.csv"
    urls = [
        "https://crawler-test.com/",
        "https://crawler-test.com/status_codes/status_404",
        "https://crawler-test.com/status_codes/status_500",
        "http://169.254.169.254/metadata-ssrf"
    ]
    df = pd.DataFrame({"page_url": urls})
    df.to_csv(csv_filename, index=False)
    print(f"[1] Created test CSV input: {csv_filename} ({len(urls)} URLs)")

    # 2. Upload file via multipart/form-data
    boundary = "----WebKitFormBoundary" + hex(int(time.time() * 1000))[2:]
    body = []
    body.append(f"--{boundary}".encode())
    body.append(f'Content-Disposition: form-data; name="mode"'.encode())
    body.append(b"")
    body.append(b"api_response_audit")
    
    body.append(f"--{boundary}".encode())
    body.append(f'Content-Disposition: form-data; name="file"; filename="{csv_filename}"'.encode())
    body.append(b"Content-Type: text/csv")
    body.append(b"")
    with open(csv_filename, "rb") as f:
        body.append(f.read())
    body.append(f"--{boundary}--".encode())
    body.append(b"")
    
    payload = b"\r\n".join(body)
    req = urllib.request.Request(
        f"{API_BASE}/api/upload",
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    
    with urllib.request.urlopen(req) as resp:
        upload_data = json.loads(resp.read().decode())
    
    upload_id = upload_data["upload_id"]
    sheets = upload_data["sheets"]
    print(f"[2] Uploaded file successfully! Upload ID: {upload_id}, Sheets: {sheets}")

    # 3. Start Job
    start_payload = json.dumps({
        "filepath": upload_data["filename"],
        "mode": "api_response_audit",
        "sheets": [s["name"] for s in sheets],
        "force_recheck": True
    }).encode("utf-8")

    req_start = urllib.request.Request(
        f"{API_BASE}/api/upload/{upload_id}/start",
        data=start_payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req_start) as resp:
        start_res = json.loads(resp.read().decode())
    print(f"[3] Job started successfully: {start_res}")

    # 4. Get job ID from upload
    with urllib.request.urlopen(f"{API_BASE}/api/uploads/{upload_id}/jobs") as resp:
        jobs_data = json.loads(resp.read().decode())
    job_id = jobs_data["jobs"][0]["id"]
    print(f"[4] Monitored Job ID: {job_id}")

    # 5. Poll job status until COMPLETED
    print("[5] Waiting for job to complete...")
    max_wait = 90
    start_t = time.time()
    final_stats = None
    
    while time.time() - start_t < max_wait:
        with urllib.request.urlopen(f"{API_BASE}/api/jobs/{job_id}/stats") as resp:
            final_stats = json.loads(resp.read().decode())
        status = final_stats.get("status")
        total_p = final_stats.get("total_pages", 0)
        print(f"    Status: {status} | Processed Pages: {total_p}/{len(urls)}")
        
        if status in ["COMPLETED", "FAILED", "PARTIAL"]:
            break
        time.sleep(2)

    print(f"[5] Final Job Status: {final_stats.get('status')}")
    print(f"    Stats Breakdown: {final_stats}")
    assert final_stats.get("status") == "COMPLETED", f"Expected COMPLETED, got {final_stats.get('status')}"

    # 6. Check results and screenshot endpoint
    with urllib.request.urlopen(f"{API_BASE}/api/jobs/{job_id}/api-audit-results") as resp:
        results_data = json.loads(resp.read().decode())
    
    pages = results_data.get("pages", [])
    print(f"\n[6] Retrieved {len(pages)} Page Audit Results from API:")
    tested_screenshot = False
    for p in pages:
        print(f"    - URL: {p['page_url']}")
        print(f"      Status: {p['page_status']} | HTTP: {p['http_status']} | APIs: {p['api_count']}")
        print(f"      Reason: {p['summary_reason']}")
        if p.get("screenshot_url"):
            print(f"      Screenshot URL: {p['screenshot_url']}")
            # Test protected screenshot endpoint
            ss_req = urllib.request.Request(f"{API_BASE}{p['screenshot_url']}")
            with urllib.request.urlopen(ss_req) as ss_resp:
                assert ss_resp.status == 200
                assert ss_resp.headers.get("content-type") == "image/png"
                img_data = ss_resp.read()
                assert len(img_data) > 0
                print(f"      [OK] Screenshot downloaded successfully ({len(img_data)} bytes)")
                tested_screenshot = True

    # 7. Download Excel Report
    print("\n[7] Testing Report Download Endpoint...")
    dl_req = urllib.request.Request(f"{API_BASE}/api/jobs/{job_id}/download")
    with urllib.request.urlopen(dl_req) as dl_resp:
        cd_header = dl_resp.headers.get("Content-Disposition")
        print(f"    Content-Disposition Header: {cd_header}")
        assert "Page-API-Audit-Report" in cd_header, f"Expected Page-API-Audit-Report in header, got: {cd_header}"
        assert cd_header.endswith('.xlsx"'), f"Expected .xlsx extension, got: {cd_header}"
        
        excel_bytes = dl_resp.read()
        downloaded_file = "downloaded_e2e_report.xlsx"
        with open(downloaded_file, "wb") as f:
            f.write(excel_bytes)
        print(f"    Downloaded report size: {len(excel_bytes)} bytes")

    # 8. Verify Downloaded Excel Formatting and Contents
    wb = openpyxl.load_workbook(downloaded_file)
    expected_sheets = ["Summary", "Page Results", "API Responses", "Diagnostics"]
    assert wb.sheetnames == expected_sheets, f"Expected {expected_sheets}, got {wb.sheetnames}"
    
    for s_name in expected_sheets:
        ws = wb[s_name]
        assert ws.freeze_panes == "A2", f"Sheet {s_name} freeze_panes mismatch"
        assert ws.auto_filter.ref is not None, f"Sheet {s_name} missing auto_filter"
        assert ws["A1"].font.bold is True, f"Sheet {s_name} A1 font not bold"
        assert ws["A1"].fill.start_color.rgb == "004F81BD", f"Sheet {s_name} A1 fill mismatch"
        print(f"    [OK] Sheet '{s_name}' verified (Freeze A2, Filter, Pro Header)")

    # Clean up test files
    for f in [csv_filename, downloaded_file]:
        try:
            if os.path.exists(f): os.remove(f)
        except: pass

    print("\n" + "=" * 60)
    print("LIVE END-TO-END PIPELINE VALIDATION COMPLETED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    main()
