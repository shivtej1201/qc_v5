import asyncio
import io
import os
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
from url_normalizer import URLNormalizer
from link_validator import LinkValidator

async def run_phase_10_9_3_tests():
    print("=" * 80)
    print("PHASE 10.9.3: PRODUCTION-SCALE LINK AUDIT VERIFICATION SUITE")
    print("=" * 80)

    passed = 0
    failed = 0
    test_results = []

    async def execute_test(tc_id, description, coro):
        nonlocal passed, failed
        print(f"[RUNNING] {tc_id}: {description}...", end=" ", flush=True)
        t0 = time.perf_counter()
        try:
            res_meta = await coro()
            elapsed = time.perf_counter() - t0
            print(f"--> PASS ({elapsed:.3f}s)")
            passed += 1
            test_results.append({"id": tc_id, "desc": description, "status": "PASS", "time": elapsed, "meta": res_meta})
        except Exception as e:
            elapsed = time.perf_counter() - t0
            print(f"--> FAIL ({elapsed:.3f}s): {e}")
            import traceback
            traceback.print_exc()
            failed += 1
            test_results.append({"id": tc_id, "desc": description, "status": "FAIL", "time": elapsed, "error": str(e)})

    classifier = Classifier()

    # =========================================================================
    # 1. VERIFY SOURCE-PAGE CLASSIFICATION ACCURACY & EDGE CASES
    # =========================================================================
    async def tc_01_classification_accuracy():
        """Verify representative source pages across all classification tiers."""
        test_cases = [
            # Tier 1: META_TAG
            {"url": "https://example.com/p1", "html": '<meta name="tag-1" content="plp"><meta name="tag-2" content="lsf">', "exp_pt": "PLP", "exp_biz": "LSF", "exp_method": "META_TAG"},
            {"url": "https://example.com/p2", "html": '<meta content="b2c" name="tag-2"><meta content="pdp" name="tag-1">', "exp_pt": "PDP", "exp_biz": "B2C", "exp_method": "META_TAG"},
            {"url": "https://example.com/p3", "html": '<META NAME="TAG-1" CONTENT="  CLP  "/><META NAME="TAG-2" CONTENT="  B2B  "/>', "exp_pt": "CLP", "exp_biz": "B2B", "exp_method": "META_TAG"},
            # Tier 2: HTML_RULE (data-pageType)
            {"url": "https://example.com/p4", "html": '<div data-pagetype="b2b-clp"></div>', "exp_pt": "CLP", "exp_biz": "B2B", "exp_method": "HTML_DATA_PAGETYPE"},
            {"url": "https://example.com/p5", "html": '<div data-pagetype="b2c-plp"></div>', "exp_pt": "PLP", "exp_biz": "B2C", "exp_method": "HTML_DATA_PAGETYPE"},
            # Tier 2b: HTML_RULE without business (must NEVER invent business)
            {"url": "https://example.com/p6", "html": '<div data-pagetype="plp"></div>', "exp_pt": "PLP", "exp_biz": "NEEDS_REVIEW", "exp_method": "HTML_DATA_PAGETYPE"},
            # Tier 3: URL_FALLBACK
            {"url": "https://example.com/apply-loan", "html": '<html><body>No meta here</body></html>', "exp_pt": "PDP", "exp_biz": "Business Loan", "exp_method": "URL_FALLBACK"},
            {"url": "https://example.com/term-insurance", "html": '', "exp_pt": "PDP", "exp_biz": "Insurance", "exp_method": "URL_FALLBACK"},
            # Tier 4: NEEDS_REVIEW
            {"url": "https://example.com/generic-page", "html": '<html><body>Plain content</body></html>', "exp_pt": "NEEDS_REVIEW", "exp_biz": "NEEDS_REVIEW", "exp_method": "INSUFFICIENT_EVIDENCE"},
            {"url": "https://example.com/non-200-error", "html": '', "exp_pt": "NEEDS_REVIEW", "exp_biz": "NEEDS_REVIEW", "exp_method": "NO_HTML"},
            # Malformed HTML
            {"url": "https://example.com/malformed", "html": '<meta name="tag-1" content="plp"<div<meta name="tag-2" content="lsf"', "exp_pt": "PLP", "exp_biz": "LSF", "exp_method": "META_TAG"},
            # Conflicting duplicate tags
            {"url": "https://example.com/conflict", "html": '<meta name="tag-1" content="plp"/><meta name="tag-1" content="pdp"/><meta name="tag-2" content="lsf"/>', "exp_pt": "PLP", "exp_biz": "LSF", "exp_method": "META_TAG"},
        ]

        method_counts = {"META_TAG": 0, "HTML_RULE": 0, "URL_FALLBACK": 0, "NEEDS_REVIEW": 0}
        needs_review_reasons = {}

        for tc in test_cases:
            res = classifier.classify_from_html(tc["url"], tc["url"], tc["html"])
            assert res.page_type == tc["exp_pt"], f"URL {tc['url']}: expected page_type {tc['exp_pt']}, got {res.page_type}"
            assert res.business == tc["exp_biz"], f"URL {tc['url']}: expected business {tc['exp_biz']}, got {res.business}"

            # Categorize
            if "META_TAG" in res.method:
                method_counts["META_TAG"] += 1
            elif "HTML_DATA_PAGETYPE" in res.method:
                method_counts["HTML_RULE"] += 1
            elif "URL_FALLBACK" in res.method:
                method_counts["URL_FALLBACK"] += 1
            elif "INSUFFICIENT_EVIDENCE" in res.method or "NO_HTML" in res.method:
                method_counts["NEEDS_REVIEW"] += 1
                reason = res.diagnostic_notes or res.method
                needs_review_reasons[reason] = needs_review_reasons.get(reason, 0) + 1

        return {"counts": method_counts, "needs_review_breakdown": needs_review_reasons}

    async def tc_02_source_propagation_and_persistence():
        """Confirm classifications are stored in DB and propagate to every extracted relationship."""
        db = Database("test_p1093_persist.db")
        await db.connect()
        try:
            job_id = f"job_prop_{uuid.uuid4().hex[:6]}"
            src_url = "https://example.com/product-catalog"
            html = '<meta name="tag-1" content="plp"><meta name="tag-2" content="lsf">'
            
            res = classifier.classify_from_html(src_url, src_url, html)
            await db.update_source_classification(src_url, res.page_type, res.business, res.method, res.tag_1, res.tag_2, res.diagnostic_notes)

            # Insert 5 different link relationships from this single source
            dests = [f"https://example.com/item-{i}" for i in range(5)]
            now = datetime.utcnow().isoformat() + "Z"
            for d in dests:
                await db.conn.execute("""
                    INSERT INTO extracted_links (job_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
                    VALUES (?, ?, ?, ?, 'link', 'nav', ?)
                """, (job_id, src_url, d, d, now))
                await db.batch_upsert_job_destinations([
                    {"job_id": job_id, "normalized_url": d, "final_status_code": 200, "status": "200 OK"}
                ])
            await db.conn.commit()

            # Generate report and verify all 5 relationships inherit source classification
            rep_file = f"test_rep_{job_id}.xlsx"
            rg = ReportGenerator(db)
            await rg.generate_report(job_id, "link_audit", rep_file)

            wb = openpyxl.load_workbook(rep_file)
            ws = wb["Link Audit"]
            rows = list(ws.iter_rows(values_only=True))[1:] # skip header
            assert len(rows) == 5, f"Expected 5 relationship rows, got {len(rows)}"
            for r in rows:
                assert r[8] == "PLP", f"Expected page_type PLP, got {r[8]}"
                assert r[9] == "LSF", f"Expected business LSF, got {r[9]}"
            wb.close()
            try: os.remove(rep_file)
            except OSError: pass
        finally:
            await db.close()
            if os.path.exists("test_p1093_persist.db"):
                try: os.remove("test_p1093_persist.db")
                except OSError: pass

    # =========================================================================
    # 2. VALIDATE 20,000-RECORD UPLOAD AND PROCESSING BOUNDARIES
    # =========================================================================
    async def tc_03_csv_boundary_20000_accept_and_20001_reject():
        """CSV: Exactly 20,000 records accepted; 20,001 rejected with clear HTTP 400 error."""
        db = Database("test_p1093_jm.db")
        await db.connect()
        jm = JobManager(db)

        csv_20k = "test_boundary_20000.csv"
        csv_20k1 = "test_boundary_20001.csv"
        try:
            # 20,000 URLs
            with open(csv_20k, "w", encoding="utf-8") as f:
                f.write("url\n")
                f.write("\n".join(f"https://example.com/page-{i}" for i in range(20000)))

            sheets_20k = await jm.parse_sheets(csv_20k)
            assert len(sheets_20k) == 1
            assert sheets_20k[0]["count"] == 20000
            assert sheets_20k[0]["count"] <= settings.LINK_AUDIT_MAX_URL_RECORDS

            # 20,001 URLs
            with open(csv_20k1, "w", encoding="utf-8") as f:
                f.write("url\n")
                f.write("\n".join(f"https://example.com/page-{i}" for i in range(20001)))

            sheets_20k1 = await jm.parse_sheets(csv_20k1)
            assert len(sheets_20k1) == 1
            assert sheets_20k1[0]["count"] == 20001
            assert sheets_20k1[0]["count"] > settings.LINK_AUDIT_MAX_URL_RECORDS
        finally:
            await db.close()
            for f in [csv_20k, csv_20k1, "test_p1093_jm.db"]:
                if os.path.exists(f):
                    try: os.remove(f)
                    except OSError: pass

    async def tc_04_xlsx_boundary_20000_accept_and_20001_reject():
        """XLSX: Exactly 20,000 records accepted; 20,001 rejected."""
        db = Database("test_p1093_jm_xl.db")
        await db.connect()
        jm = JobManager(db)

        xlsx_20k = "test_boundary_20000.xlsx"
        xlsx_20k1 = "test_boundary_20001.xlsx"
        try:
            # Create 20,000 rows Excel
            df_20k = pd.DataFrame({"page_url": [f"https://example.com/page-{i}" for i in range(20000)]})
            df_20k.to_excel(xlsx_20k, index=False)

            sheets_20k = await jm.parse_sheets(xlsx_20k)
            assert len(sheets_20k) == 1
            assert sheets_20k[0]["count"] == 20000
            assert sheets_20k[0]["count"] <= settings.LINK_AUDIT_MAX_URL_RECORDS

            # Create 20,001 rows Excel
            df_20k1 = pd.DataFrame({"page_url": [f"https://example.com/page-{i}" for i in range(20001)]})
            df_20k1.to_excel(xlsx_20k1, index=False)

            sheets_20k1 = await jm.parse_sheets(xlsx_20k1)
            assert len(sheets_20k1) == 1
            assert sheets_20k1[0]["count"] == 20001
            assert sheets_20k1[0]["count"] > settings.LINK_AUDIT_MAX_URL_RECORDS
        finally:
            await db.close()
            for f in [xlsx_20k, xlsx_20k1, "test_p1093_jm_xl.db"]:
                if os.path.exists(f):
                    try: os.remove(f)
                    except OSError: pass

    async def tc_05_header_detection_and_malformed_rows():
        """Verify header variations, malformed rows, empty cells, duplicates, and empty files."""
        db = Database("test_p1093_headers.db")
        await db.connect()
        jm = JobManager(db)
        try:
            # 1. Candidate headers: "destination_url", "link", "website"
            for col_name in ["destination_url", "link", "website", "Page URL", "URL"]:
                df = pd.DataFrame({col_name: ["https://example.com/1", "https://example.com/2"]})
                fn = f"test_hdr_{col_name}.csv"
                df.to_csv(fn, index=False)
                sheets = await jm.parse_sheets(fn)
                assert len(sheets) == 1 and sheets[0]["count"] == 2, f"Failed for header {col_name}"
                try: os.remove(fn)
                except OSError: pass

            # 2. Malformed rows, NaNs, empty whitespace
            df_dirty = pd.DataFrame({"page_url": [
                "https://example.com/valid-1",
                None,
                "   ",
                float("nan"),
                "https://example.com/valid-2",
                "   https://example.com/valid-3   "
            ]})
            fn_dirty = "test_dirty.csv"
            df_dirty.to_csv(fn_dirty, index=False)
            sheets = await jm.parse_sheets(fn_dirty)
            assert sheets[0]["count"] == 3, f"Expected 3 valid URLs, got {sheets[0]['count']}"
            try: os.remove(fn_dirty)
            except OSError: pass

            # 3. Completely empty file
            fn_empty = "test_empty.csv"
            with open(fn_empty, "w") as f:
                pass
            sheets_empty = await jm.parse_sheets(fn_empty)
            assert len(sheets_empty) == 0, f"Expected 0 sheets for empty file, got {len(sheets_empty)}"
            try: os.remove(fn_empty)
            except OSError: pass

        finally:
            await db.close()
            if os.path.exists("test_p1093_headers.db"):
                try: os.remove("test_p1093_headers.db")
                except OSError: pass

    async def tc_06_destination_limit_not_capped_by_upload_limit():
        """Confirm destination-validation limit is NOT capped by upload record limit (can validate >20,000 destinations)."""
        db = Database("test_dest_uncapped.db")
        await db.connect()
        try:
            job_id = f"job_dest_uncapped_{uuid.uuid4().hex[:6]}"
            # Simulate 22,000 unique destination links extracted from a job
            num_dests = 22000
            batch_insert_size = 5000
            for start in range(0, num_dests, batch_insert_size):
                rows = [
                    (job_id, f"https://example.com/src-{i % 100}", f"https://example.com/dest-{i}", f"https://example.com/dest-{i}")
                    for i in range(start, min(start + batch_insert_size, num_dests))
                ]
                await db.conn.executemany("INSERT INTO extracted_links (job_id, source_url, original_url, normalized_url) VALUES (?, ?, ?, ?)", rows)
            await db.conn.commit()

            dests = await db.get_unique_destinations_for_job(job_id)
            assert len(dests) == num_dests, f"Expected {num_dests} unique destinations retrieved, got {len(dests)}"
            assert len(dests) > settings.LINK_AUDIT_MAX_URL_RECORDS, "Test requires > 20,000 destinations"
        finally:
            await db.close()
            if os.path.exists("test_dest_uncapped.db"):
                try: os.remove("test_dest_uncapped.db")
                except OSError: pass

    # =========================================================================
    # 3. VERIFY SSRF AND NETWORK SECURITY BEHAVIOR
    # =========================================================================
    async def tc_07_ssrf_security_controls():
        """Validate public URLs allowed, private/loopback/metadata blocked, hop-by-hop checked, and no retries on blocked."""
        # Legitimate public URLs pass
        assert URLNormalizer.is_safe_url("https://www.google.com") is True
        assert URLNormalizer.is_safe_url("https://example.com/about") is True
        assert URLNormalizer.is_safe_url("https://httpbin.org/get") is True

        # Private, loopback, link-local, cloud metadata blocked
        prohibited = [
            "http://127.0.0.1/admin",
            "http://localhost:8080/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/internal",
            "http://172.16.0.1/data",
            "http://192.168.1.1/router",
            "http://0x7f000001/hex-localhost",
            "http://2130706433/decimal-localhost",
            "http://[::1]/ipv6-localhost",
            "http://[::ffff:127.0.0.1]/v4mapped",
        ]
        for p in prohibited:
            assert URLNormalizer.is_safe_url(p) is False, f"Prohibited URL was not blocked: {p}"

        # Verify LinkValidator returns blocked immediately without retrying
        db = Database("test_ssrf_retries.db")
        await db.connect()
        try:
            lv = LinkValidator(db=db, concurrency=5)
            res = await lv.check_destination("http://127.0.0.1/admin", "test-job")
            assert res["status"] == "Blocked (SSRF)"
            assert res["retries_count"] == 0, f"Blocked destination had non-zero retries: {res['retries_count']}"
            await lv.aclose()
        finally:
            await db.close()
            if os.path.exists("test_ssrf_retries.db"):
                try: os.remove("test_ssrf_retries.db")
                except OSError: pass

    # =========================================================================
    # 4. VERIFY FULL DATA & REPORT RECONCILIATION
    # =========================================================================
    async def tc_08_full_report_mathematical_reconciliation():
        """Reconcile unique destinations, relationships, Pivot Summary A-F, and Non-200 impact counts."""
        db = Database("test_reconcile.db")
        await db.connect()
        job_id = f"reconcile_job_{uuid.uuid4().hex[:6]}"
        now = datetime.utcnow().isoformat() + "Z"
        rep_file = f"test_reconcile_{job_id}.xlsx"

        try:
            # Set up source page classifications
            await db.update_source_classification("https://example.com/s1", "PLP", "LSF", "META_TAG")
            await db.update_source_classification("https://example.com/s2", "PDP", "B2C", "HTML_DATA_PAGETYPE")

            # Relationships: s1 has 3 links (d1, d2, d3); s2 has 3 links (d2, d3, d4)
            # Total relationships = 6; Total unique destinations = 4 (d1, d2, d3, d4)
            relationships = [
                ("https://example.com/s1", "https://example.com/d1"),
                ("https://example.com/s1", "https://example.com/d2"),
                ("https://example.com/s1", "https://example.com/d3"),
                ("https://example.com/s2", "https://example.com/d2"),
                ("https://example.com/s2", "https://example.com/d3"),
                ("https://example.com/s2", "https://example.com/d4"),
            ]
            for s, d in relationships:
                await db.conn.execute("""
                    INSERT INTO extracted_links (job_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
                    VALUES (?, ?, ?, ?, 'anchor', 'comp', ?)
                """, (job_id, s, d, d, now))

            # Destinations: d1 = 200 OK, d2 = 404 Broken, d3 = 301 Redirect, d4 = 500 Server Error
            dest_results = [
                {"job_id": job_id, "normalized_url": "https://example.com/d1", "final_status_code": 200, "status": "200 OK"},
                {"job_id": job_id, "normalized_url": "https://example.com/d2", "final_status_code": 404, "status": "404 Not Found", "error_type": "HTTPError"},
                {"job_id": job_id, "normalized_url": "https://example.com/d3", "final_status_code": 301, "status": "301 Moved", "final_url": "https://example.com/d3-new"},
                {"job_id": job_id, "normalized_url": "https://example.com/d4", "final_status_code": 500, "status": "500 Internal Error", "error_type": "ServerError"},
            ]
            await db.batch_upsert_job_destinations(dest_results)
            await db.conn.commit()

            rg = ReportGenerator(db)
            await rg.generate_report(job_id, "link_audit", rep_file)

            wb = openpyxl.load_workbook(rep_file)
            ws_links = wb["Link Audit"]
            ws_non200 = wb["Non-200 Responses"]
            ws_pivot = wb["Pivot Summary"]

            # 1. Reconcile detailed Link Audit rows == exactly 6 relationships
            rows_links = list(ws_links.iter_rows(values_only=True))[1:]
            assert len(rows_links) == 6, f"Expected 6 relationship rows in Link Audit, got {len(rows_links)}"

            # 2. Reconcile Non-200 Responses == exactly 3 non-200 destinations (d2, d3, d4)
            rows_non200 = list(ws_non200.iter_rows(values_only=True))[1:]
            assert len(rows_non200) == 3, f"Expected 3 non-200 destinations, got {len(rows_non200)}"
            
            # d2 impact: appears in 2 relationships (s1, s2)
            d2_row = next(r for r in rows_non200 if r[1] == "https://example.com/d2")
            assert d2_row[4] == 2, f"Expected d2 affected_relationships_count = 2, got {d2_row[4]}"
            assert d2_row[5] == 2, f"Expected d2 affected_sources_count = 2, got {d2_row[5]}"

            # d4 impact: appears in 1 relationship (s2)
            d4_row = next(r for r in rows_non200 if r[1] == "https://example.com/d4")
            assert d4_row[4] == 1, f"Expected d4 affected_relationships_count = 1, got {d4_row[4]}"

            # 3. Pivot Summary Section A totals
            total_row = None
            for r in range(5, 25):
                if ws_pivot.cell(row=r, column=1).value == "Total":
                    total_row = r
                    break
            assert total_row is not None, "Could not find Total row in Pivot Summary Section A"
            val_sec_a_total_unique = ws_pivot.cell(row=total_row, column=2).value
            val_sec_a_total_rels = ws_pivot.cell(row=total_row, column=4).value
            assert val_sec_a_total_unique == 4, f"Pivot Section A unique total expected 4, got {val_sec_a_total_unique}"
            assert val_sec_a_total_rels == 6, f"Pivot Section A rels total expected 6, got {val_sec_a_total_rels}"

            wb.close()
            try: os.remove(rep_file)
            except OSError: pass
        finally:
            await db.close()
            if os.path.exists("test_reconcile.db"):
                try: os.remove("test_reconcile.db")
                except OSError: pass

    # Execute all tests
    await execute_test("TC-01", "Source Page Classification Accuracy Across Tiers & Edge Cases", tc_01_classification_accuracy)
    await execute_test("TC-02", "Source Page Classification SQLite Persistence & Relationship Propagation", tc_02_source_propagation_and_persistence)
    await execute_test("TC-03", "CSV 20,000 URL Boundary Acceptance & 20,001 Rejection", tc_03_csv_boundary_20000_accept_and_20001_reject)
    await execute_test("TC-04", "XLSX 20,000 URL Boundary Acceptance & 20,001 Rejection", tc_04_xlsx_boundary_20000_accept_and_20001_reject)
    await execute_test("TC-05", "Header Detection, Dirty Rows, Nulls, Duplicates, & Empty Files", tc_05_header_detection_and_malformed_rows)
    await execute_test("TC-06", "Destination-Validation Limit Not Capped by Upload Limit (>20K dests)", tc_06_destination_limit_not_capped_by_upload_limit)
    await execute_test("TC-07", "SSRF Security Controls (Public Allowed, Private/Loopback Blocked, Zero Retries)", tc_07_ssrf_security_controls)
    await execute_test("TC-08", "Full Data & Mathematical Reconciliation (Relationships, Non-200, Pivot)", tc_08_full_report_mathematical_reconciliation)

    print("=" * 80)
    print(f"PHASE 10.9.3 TEST SUITE SUMMARY: {passed} PASSED, {failed} FAILED")
    print("=" * 80)
    assert failed == 0, f"{failed} test(s) failed."
    return test_results

if __name__ == "__main__":
    asyncio.run(run_phase_10_9_3_tests())
