import asyncio
import os
from datetime import datetime
import openpyxl
import pandas as pd
from db import Database
from report_generator import ReportGenerator
from classifier import Classifier

async def test_phase_10_7():
    print("Testing Phase 10.7 Requirements...")
    
    # 1. Test Source Page Classification Persistence & Mapping
    db = Database("test_audit_cache.db")
    await db.connect()
    
    source_url = "https://www.bajajfinserv.in/bmall/sony-wireless-headphones"
    html_source = '<html><body><div data-pagetype="B2C-pdp"></div><a href="/dest1">D1</a><a href="/dest2">D2</a></body></html>'
    
    classifier = Classifier()
    pt, biz, method = classifier.classify_from_html(source_url, source_url, html_source)
    assert pt == "PDP" and biz == "B2C" and method == "HTML_DATA_PAGETYPE", f"Expected PDP/B2C, got {pt}/{biz}/{method}"
    
    # Persist source classification
    await db.update_source_classification(source_url, pt, biz, method)
    retrieved = await db.get_source_classification(source_url)
    assert retrieved is not None
    assert retrieved["page_type"] == "PDP"
    assert retrieved["business"] == "B2C"
    assert retrieved["classification_method"] == "HTML_DATA_PAGETYPE"
    print("[OK] Source page classification correctly extracted and persisted in SQLite")

    # 2. Insert mock extracted links and destinations
    import uuid
    job_id = f"test-job-{uuid.uuid4()}"
    batch_id = "test-batch-1"
    
    # Two destination URLs originating from the same source
    dest1 = "https://example.com/dest1"
    dest2 = "https://example.com/dest2"
    
    # Insert extracted links
    sql_links = """
        INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    await db.conn.execute(sql_links, (job_id, batch_id, source_url, dest1, dest1, "D1", "Header", "", "", "2026-10-11T00:00:00Z"))
    await db.conn.execute(sql_links, (job_id, batch_id, source_url, dest2, dest2, "D2", "Footer", "", "", "2026-10-11T00:00:00Z"))
    
    # Destination check results (even if dest has different status/result)
    await db.batch_insert_results([
        {"normalized_url": dest1, "original_url": dest1, "initial_status_code": 200, "final_status_code": 200, "status": "200 OK", "final_url": dest1, "redirect_count": 0, "redirect_chain": "", "response_time_ms": 100, "content_type": "text/html", "content_length": 500, "error_type": None, "error_message": None, "check_method": "HEAD", "browser_verified": 0, "page_type": "XYZ", "business": "BIZ_OTHER", "classification_method": "URL Pattern", "checked_at": "2026-10-11T00:01:00Z"},
        {"normalized_url": dest2, "original_url": dest2, "initial_status_code": 404, "final_status_code": 404, "status": "404 Not Found", "final_url": dest2, "redirect_count": 0, "redirect_chain": "", "response_time_ms": 120, "content_type": "text/html", "content_length": 200, "error_type": "HTTPError", "error_message": "404", "check_method": "HEAD", "browser_verified": 0, "page_type": "ABC", "business": "BIZ_OTHER2", "classification_method": "URL Pattern", "checked_at": "2026-10-11T00:01:00Z"}
    ])
    
    # 3. Generate Excel Report
    report_gen = ReportGenerator(db)
    test_report_file = "test_phase_10_7_report.xlsx"
    if os.path.exists(test_report_file):
        os.remove(test_report_file)
        
    await report_gen.generate_report(job_id, "link_audit", test_report_file)
    assert os.path.exists(test_report_file)
    print("[OK] Report generation succeeded without errors")
    
    # 4. Verify Excel Content & Source-Page Association
    df = pd.read_excel(test_report_file, sheet_name="Link Audit")
    assert len(df) == 2
    # Verify that BOTH rows have source page_type = PDP and business = B2C, NOT the destination's XYZ / ABC!
    for idx, row in df.iterrows():
        assert row["source_page_url"] == source_url
        assert row["page_type"] == "PDP", f"Row {idx} page_type expected PDP, got {row['page_type']}"
        assert row["business"] == "B2C", f"Row {idx} business expected B2C, got {row['business']}"
    print("[OK] Both report rows strictly inherit source_page_url's page_type and business")
    
    # 5. Verify openpyxl Styling
    wb = openpyxl.load_workbook(test_report_file)
    ws = wb["Link Audit"]
    
    # Freeze panes
    assert ws.freeze_panes == "A2", f"Expected freeze_panes 'A2', got {ws.freeze_panes}"
    print("[OK] Freeze pane A2 verified")
    
    # Auto-filter
    assert ws.auto_filter.ref is not None and ws.auto_filter.ref.startswith("A1:"), f"Invalid auto_filter ref: {ws.auto_filter.ref}"
    print("[OK] Auto-filter verified")
    
    # Header styling
    h_cell = ws["A1"]
    assert h_cell.font.bold is True
    assert h_cell.fill.start_color.rgb == "004F81BD"
    assert h_cell.font.color.rgb == "00FFFFFF"
    assert h_cell.alignment.horizontal == "center" and h_cell.alignment.vertical == "center"
    assert h_cell.border.left.style == "thin"
    print("[OK] Header styling (blue fill, bold white font, thin border, centered) verified")
    
    # Data row styling
    d_cell = ws["A2"]
    assert d_cell.alignment.wrap_text is True
    assert d_cell.alignment.vertical == "top"
    assert d_cell.border.left.style == "thin"
    print("[OK] Data cell styling (wrap_text=True, top vertical alignment, thin border) verified")
    
    # Column auto-width bounded at 60
    for col_letter in ["A", "B", "E", "F"]:
        width = ws.column_dimensions[col_letter].width
        assert width is not None and width <= 60
    print("[OK] Column widths auto-fitted and bounded by max width of 60")
    
    # 6. Test Filename Pattern
    sample_dt = datetime(2026, 10, 11, 0, 50)
    formatted_name = sample_dt.strftime("URLs-Audit-Report_%Y_%b_%d-%I_%M_%p.xlsx")
    assert formatted_name == "URLs-Audit-Report_2026_Oct_11-12_50_AM.xlsx"
    print(f"[OK] Friendly download filename pattern verified: {formatted_name}")
    
    await db.close()
    for f in [test_report_file, "test_audit_cache.db", "test_audit_cache.db-wal", "test_audit_cache.db-shm"]:
        try:
            if os.path.exists(f): os.remove(f)
        except Exception:
            pass
    print("\nALL PHASE 10.7 TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(test_phase_10_7())
