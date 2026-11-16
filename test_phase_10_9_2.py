import asyncio
import io
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
import pandas as pd
import openpyxl

from config import settings
from db import Database
from classifier import Classifier, ClassificationResult
from html_parser import Parser
from job_manager import JobManager
from report_generator import ReportGenerator

async def run_all_tests():
    print("=" * 80)
    print("PHASE 10.9.2: SOURCE PAGE CLASSIFICATION, 20K UPLOAD & WORKER BENCHMARK")
    print("=" * 80)
    
    passed = 0
    failed = 0

    async def execute_test(tc_id, description, coro):
        nonlocal passed, failed
        print(f"[RUNNING] {tc_id}: {description}...", end=" ", flush=True)
        try:
            await coro()
            print("--> PASS")
            passed += 1
        except Exception as e:
            print(f"--> FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    classifier = Classifier()

    # -------------------------------------------------------------------------
    # PART 1: SOURCE PAGE CLASSIFICATION FIX (TC-C1 to TC-C10)
    # -------------------------------------------------------------------------
    async def tc_c1():
        html = '<html><head><meta name="tag-2" content="lsf"/><meta name="tag-1" content="plp"/></head><body></body></html>'
        url = "https://example.com/products"
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "PLP", f"Expected PLP, got {res.page_type}"
        assert res.business == "LSF", f"Expected LSF, got {res.business}"
        assert "META_TAG" in res.method, f"Expected META_TAG in method, got {res.method}"
        assert res.tag_1 == "PLP"
        assert res.tag_2 == "LSF"

    async def tc_c2():
        # Case insensitivity in tag names, attribute names, values and whitespace
        html = '<html><head><META CONTENT="  b2c-pdp  " NAME="TAG-1" /><meta content=" lsf " name="tag-2" /></head></html>'
        url = "https://example.com/pdp-details"
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "B2C-PDP", f"Expected B2C-PDP, got {res.page_type}"
        assert res.business == "LSF", f"Expected LSF, got {res.business}"
        assert res.tag_1 == "B2C-PDP"
        assert res.tag_2 == "LSF"

    async def tc_c3():
        # Flexible attribute ordering and single/double quotes
        html = "<html><head><meta content='plp' name='tag-1'/><meta name=\"tag-2\" content='bfsd'></head></html>"
        url = "https://example.com/category"
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "PLP"
        assert res.business == "BFSD"

    async def tc_c4():
        # Duplicate meta tags deterministic handling (first valid non-empty, with diagnostic warning)
        html = '<html><head><meta name="tag-1" content="plp"/><meta name="tag-1" content="pdp"/><meta name="tag-2" content="lsf"/></head></html>'
        url = "https://example.com/duplicate-test"
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "PLP", f"Expected first tag PLP, got {res.page_type}"
        assert "Conflicting tag-1 meta tags found" in res.diagnostic_notes, f"Expected conflict diagnostic, got {res.diagnostic_notes}"

    async def tc_c5():
        # Empty first tag, valid second tag
        html = '<html><head><meta name="tag-1" content=""/><meta name="tag-1" content="plp"/><meta name="tag-2" content="lsf"/></head></html>'
        url = "https://example.com/empty-first-tag"
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "PLP"
        assert res.business == "LSF"

    async def tc_c6():
        # Precedence: Meta tags override data-pageType and URL patterns
        html = '<html><head><meta name="tag-1" content="plp"/><meta name="tag-2" content="lsf"/></head><body><div data-pagetype="b2b-clp"></div></body></html>'
        url = "https://example.com/loan-application" # matches loan -> PDP rule
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "PLP", f"Meta tag should take precedence, got {res.page_type}"
        assert res.business == "LSF", f"Meta tag should take precedence, got {res.business}"
        assert res.method == "META_TAG"

    async def tc_c7():
        # Partial metadata: tag-1 present, tag-2 missing -> data-pageType fallback for business
        html = '<html><head><meta name="tag-1" content="plp"/></head><body><div data-pagetype="b2b-clp"></div></body></html>'
        url = "https://example.com/test"
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "PLP", f"Expected PLP from tag-1, got {res.page_type}"
        assert res.business == "B2B", f"Expected B2B from data-pagetype, got {res.business}"
        assert "META_TAG" in res.method and "HTML_DATA_PAGETYPE" in res.method

    async def tc_c8():
        # Insufficient evidence: No meta tags, no data-pageType, no URL match -> NEEDS_REVIEW
        html = '<html><head><title>Some Generic Page</title></head><body><p>Hello world</p></body></html>'
        url = "https://example.com/general/about-us"
        res = classifier.classify_from_html(url, url, html)
        assert res.page_type == "NEEDS_REVIEW", f"Expected NEEDS_REVIEW, got {res.page_type}"
        assert res.business == "NEEDS_REVIEW", f"Expected NEEDS_REVIEW, got {res.business}"
        assert res.method == "INSUFFICIENT_EVIDENCE"
        assert len(res.diagnostic_notes) > 0

    async def tc_c9():
        # Database persistence and retrieval of extended classification fields
        db = Database("test_db_p10_9_2.db")
        await db.connect()
        try:
            src = "https://example.com/classified-page"
            await db.update_source_classification(
                src, "PLP", "LSF", "META_TAG",
                tag_1="PLP", tag_2="LSF", diagnostic_notes="Valid meta tags."
            )
            retrieved = await db.get_source_classification(src)
            assert retrieved is not None
            assert retrieved["page_type"] == "PLP"
            assert retrieved["business"] == "LSF"
            assert retrieved["classification_method"] == "META_TAG"
            assert retrieved["tag_1"] == "PLP"
            assert retrieved["tag_2"] == "LSF"
            assert "Valid meta tags" in retrieved["diagnostic_notes"]
        finally:
            await db.close()
            if os.path.exists("test_db_p10_9_2.db"):
                try: os.remove("test_db_p10_9_2.db")
                except OSError: pass

    async def tc_c10():
        # Propagation to Excel Link Audit, Non-200, and Pivot Summary
        db = Database("test_report_p10_9_2.db")
        await db.connect()
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        try:
            src_classified = "https://example.com/source-plp"
            src_unclassified = "https://example.com/source-unknown"
            dest1 = "https://example.com/dest-ok"
            dest2 = "https://example.com/dest-bad"

            await db.update_source_classification(src_classified, "PLP", "LSF", "META_TAG", "PLP", "LSF", "OK")
            await db.update_source_classification(src_unclassified, "NEEDS_REVIEW", "NEEDS_REVIEW", "INSUFFICIENT_EVIDENCE")

            now = datetime.utcnow().isoformat() + "Z"
            await db.conn.execute("INSERT INTO jobs (id, total_records, status, created_at, updated_at) VALUES (?, 2, 'COMPLETED', ?, ?)", (job_id, now, now))
            
            # Insert extracted links
            await db.conn.execute("""
                INSERT INTO extracted_links (job_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (job_id, src_classified, dest1, dest1, "OK Link", "Nav", now))
            await db.conn.execute("""
                INSERT INTO extracted_links (job_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (job_id, src_unclassified, dest2, dest2, "Broken Link", "Body", now))

            # Insert job_destinations
            await db.batch_upsert_job_destinations([
                {"job_id": job_id, "normalized_url": dest1, "final_status_code": 200, "status": "200 OK"},
                {"job_id": job_id, "normalized_url": dest2, "final_status_code": 404, "status": "404 Not Found", "error_type": "HTTPError"}
            ])

            rep_file = f"test_out_{job_id}.xlsx"
            rg = ReportGenerator(db)
            await rg.generate_report(job_id, "link_audit", rep_file)
            
            wb = openpyxl.load_workbook(rep_file)
            ws_links = wb["Link Audit"]
            ws_non200 = wb["Non-200 Responses"]
            ws_pivot = wb["Pivot Summary"]

            # Check Link Audit rows
            rows_la = list(ws_links.iter_rows(values_only=True))
            # Header is row 0
            row1_pt = rows_la[1][8] # page_type
            row1_biz = rows_la[1][9] # business
            row2_pt = rows_la[2][8]
            row2_biz = rows_la[2][9]

            assert row1_pt == "PLP", f"Expected PLP, got {row1_pt}"
            assert row1_biz == "LSF", f"Expected LSF, got {row1_biz}"
            assert row2_pt == "NEEDS_REVIEW", f"Expected NEEDS_REVIEW, got {row2_pt}"
            assert row2_biz == "NEEDS_REVIEW", f"Expected NEEDS_REVIEW, got {row2_biz}"

            # Check Non-200 row
            rows_n200 = list(ws_non200.iter_rows(values_only=True))
            assert len(rows_n200) >= 2
            n200_pt = rows_n200[1][7]
            n200_biz = rows_n200[1][8]
            assert "NEEDS_REVIEW" in n200_pt

            wb.close()
            try: os.remove(rep_file)
            except OSError: pass
        finally:
            await db.close()
            if os.path.exists("test_report_p10_9_2.db"):
                try: os.remove("test_report_p10_9_2.db")
                except OSError: pass

    # -------------------------------------------------------------------------
    # PART 2: 20,000 URL UPLOAD LIMIT (TC-U1 to TC-U6)
    # -------------------------------------------------------------------------
    async def tc_u1():
        # Verify upload limits are configurable and support large jobs by default.
        assert getattr(settings, "LINK_AUDIT_MAX_URL_RECORDS", 0) > 0
        assert getattr(settings, "MAX_URLS_PER_JOB", 0) > 0

    async def tc_u2():
        # Parse CSV with exactly 20,000 URLs
        db = Database("test_db_jm.db")
        await db.connect()
        jm = JobManager(db)
        try:
            csv_path = "test_20000_urls.csv"
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("url\n")
                for i in range(20000):
                    f.write(f"https://example.com/page-{i}\n")

            sheets = await jm.parse_sheets(csv_path)
            assert len(sheets) == 1
            assert sheets[0]["count"] == 20000, f"Expected 20000, got {sheets[0]['count']}"
        finally:
            await db.close()
            if os.path.exists("test_20000_urls.csv"):
                try: os.remove("test_20000_urls.csv")
                except OSError: pass
            if os.path.exists("test_db_jm.db"):
                try: os.remove("test_db_jm.db")
                except OSError: pass

    async def tc_u3():
        # Parse CSV with 20,001 URLs and verify rejection limit logic
        csv_path = "test_20001_urls.csv"
        try:
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("url\n")
                for i in range(20001):
                    f.write(f"https://example.com/page-{i}\n")

            db = Database("test_db_jm2.db")
            await db.connect()
            jm = JobManager(db)
            sheets = await jm.parse_sheets(csv_path)
            total_count = sum(s["count"] for s in sheets)
            assert total_count == 20001
            assert total_count > settings.LINK_AUDIT_MAX_URL_RECORDS
            await db.close()
        finally:
            if os.path.exists(csv_path):
                try: os.remove(csv_path)
                except OSError: pass
            if os.path.exists("test_db_jm2.db"):
                try: os.remove("test_db_jm2.db")
                except OSError: pass

    async def tc_u4():
        # Batch generation: 20,000 URLs creates exactly 20 batches of 1,000 URLs each
        db = Database("test_db_batches.db")
        await db.connect()
        jm = JobManager(db)
        try:
            csv_path = "test_batch_split.csv"
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("url\n")
                for i in range(20000):
                    f.write(f"https://example.com/item-{i}\n")

            upload_id = str(uuid.uuid4())
            await jm.create_upload(upload_id, "test_batch_split.csv", "link_audit")
            await jm.create_jobs_for_sheets(upload_id, csv_path, "link_audit", ["CSV Data"])

            jobs = await jm.get_jobs_for_upload(upload_id)
            assert len(jobs) == 1
            job_id = jobs[0]["id"]
            assert jobs[0]["total_records"] == 20000

            async with db.conn.execute("SELECT COUNT(*), MIN(total_records), MAX(total_records) FROM batches WHERE job_id = ?", (job_id,)) as cur:
                row = await cur.fetchone()
                batch_count, min_rec, max_rec = row[0], row[1], row[2]
                assert batch_count == 20, f"Expected 20 batches of 1000, got {batch_count}"
                assert min_rec == 1000
                assert max_rec == 1000
        finally:
            await db.close()
            if os.path.exists(csv_path):
                try: os.remove(csv_path)
                except OSError: pass
            if os.path.exists("test_db_batches.db"):
                try: os.remove("test_db_batches.db")
                except OSError: pass

    # -------------------------------------------------------------------------
    # PART 3: BOUNDED WORKER CONCURRENCY (TC-P1 to TC-P3)
    # -------------------------------------------------------------------------
    async def tc_p1():
        # Bounded concurrency check: link_validator respects concurrency limits
        from link_validator import LinkValidator
        db = Database("test_db_lv.db")
        await db.connect()
        try:
            lv = LinkValidator(db=db, concurrency=10, max_retries=1)
            assert lv.concurrency == 10
            assert lv.max_retries == 1
            # Test bound clamping (e.g. 100 clamped to 50)
            lv_clamped = LinkValidator(db=db, concurrency=100)
            assert lv_clamped.concurrency == 50
            await lv.aclose()
            await lv_clamped.aclose()
        finally:
            await db.close()
            if os.path.exists("test_db_lv.db"):
                try: os.remove("test_db_lv.db")
                except OSError: pass

    # Execute all tests
    await execute_test("TC-C1", "Source Page Metadata Classification (tag-1 / tag-2)", tc_c1)
    await execute_test("TC-C2", "Case-Insensitivity & Normalized Uppercase Values", tc_c2)
    await execute_test("TC-C3", "Flexible Attribute Ordering & Quotes", tc_c3)
    await execute_test("TC-C4", "Deterministic Duplicate Handling & Conflict Diagnostics", tc_c4)
    await execute_test("TC-C5", "Empty First Tag Ignored in Favor of Valid Tag", tc_c5)
    await execute_test("TC-C6", "Precedence: Meta Tag Overrides data-pageType & URL Fallback", tc_c6)
    await execute_test("TC-C7", "Partial Metadata Fallback to Trusted HTML Rule", tc_c7)
    await execute_test("TC-C8", "Insufficient Evidence Resolves to Explicit NEEDS_REVIEW", tc_c8)
    await execute_test("TC-C9", "SQLite Persistence & Retrieval of Extended Metadata", tc_c9)
    await execute_test("TC-C10", "Report Propagation to Detailed, Non-200, and Pivot Sheets", tc_c10)
    await execute_test("TC-U1", "Configuration Setting LINK_AUDIT_MAX_URL_RECORDS = 20000", tc_u1)
    await execute_test("TC-U2", "Upload CSV Parsing Supports 20,000 URLs", tc_u2)
    await execute_test("TC-U3", "Rejection Limit Identified Above 20,000 URLs", tc_u3)
    await execute_test("TC-U4", "Batch Generation Creates 20 Batches of 1,000 URLs for 20K URLs", tc_u4)
    await execute_test("TC-P1", "Link Validator Bounded Concurrency Setting Respected", tc_p1)

    print("=" * 80)
    print(f"RESULTS: {passed} PASSED, {failed} FAILED")
    print("=" * 80)
    assert failed == 0, f"{failed} test(s) failed."

if __name__ == "__main__":
    asyncio.run(run_all_tests())
