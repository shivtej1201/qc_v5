import os
import sys
import uuid
import asyncio
import traceback
import pandas as pd
import openpyxl
from datetime import datetime, timezone

from db import Database
from url_normalizer import URLNormalizer
from report_generator import ReportGenerator

async def insert_extracted_links(db: Database, links):
    sql = """
        INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, image_name, loading_type, extracted_timestamp)
        VALUES (:job_id, :batch_id, :source_url, :original_url, :normalized_url, :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp)
    """
    clean_links = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for l in links:
        clean_links.append({
            "job_id": l.get("job_id", ""),
            "batch_id": l.get("batch_id", ""),
            "source_url": l.get("source_url", ""),
            "original_url": l.get("original_url", ""),
            "normalized_url": l.get("normalized_url", ""),
            "anchor_text": l.get("anchor_text", ""),
            "component_name": l.get("component_name", ""),
            "image_name": l.get("image_name", ""),
            "loading_type": l.get("loading_type", ""),
            "extracted_timestamp": l.get("extracted_timestamp", now_iso)
        })
    await db.conn.executemany(sql, clean_links)
    await db.conn.commit()

async def run_all_tests():
    print("=" * 70)
    print("PHASE 10.8: LINK AUDITOR OPTIMIZATION & PIVOT SUMMARY TEST SUITE")
    print("=" * 70)

    db_file = f"test_db_p108_{uuid.uuid4().hex[:6]}.sqlite"
    db = Database(db_file)
    await db.connect()
    
    passed = 0
    failed = 0

    async def run_test(test_name, coro):
        nonlocal passed, failed
        try:
            print(f"[RUNNING] {test_name}...", end=" ", flush=True)
            await coro
            print("--> PASS")
            passed += 1
        except Exception as e:
            print("--> FAIL")
            traceback.print_exc()
            failed += 1

    try:
        # TC-1: Repeated destination across multiple sources validated once in job_destinations
        async def tc01():
            job_id = f"job_tc01_{uuid.uuid4().hex[:6]}"
            batch_id = f"batch_{uuid.uuid4().hex[:6]}"
            dest_url = "https://example.com/shared-target"
            norm_dest = URLNormalizer.normalize(dest_url)
            
            links = []
            for i in range(5):
                links.append({
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "source_url": f"https://example.com/source-{i}",
                    "original_url": dest_url,
                    "normalized_url": norm_dest,
                    "anchor_text": f"Target Link {i}",
                    "component_name": "FooterNav",
                    "extracted_timestamp": datetime.now(timezone.utc).isoformat()
                })
            await insert_extracted_links(db, links)

            dest_results = [{
                "job_id": job_id,
                "normalized_url": norm_dest,
                "initial_status_code": 200,
                "final_status_code": 200,
                "status": "Working",
                "final_url": norm_dest,
                "redirect_count": 0,
                "redirect_chain": "[]",
                "response_time_ms": 120.0,
                "content_type": "text/html",
                "content_length": 1024,
                "error_type": "",
                "error_message": "",
                "check_method": "HTTP",
                "browser_verified": "NO",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "retries_count": 0
            }]
            await db.batch_upsert_job_destinations(dest_results)

            dests = await db.get_job_destinations(job_id)
            assert len(dests) == 1, f"Expected 1 unique destination in job_destinations, got {len(dests)}"
            assert dests[0]["normalized_url"] == norm_dest

            stats = await db.get_link_audit_stats(job_id)
            assert stats["total_extracted"] == 5, f"Expected 5 extracted, got {stats['total_extracted']}"
            assert stats["total_unique"] == 1, f"Expected 1 unique, got {stats['total_unique']}"
            assert stats["duplicate_avoided"] == 4, f"Expected 4 avoided, got {stats['duplicate_avoided']}"
            assert stats["dedup_ratio"] == 80.0, f"Expected 80.0%, got {stats['dedup_ratio']}"
            assert stats["unique_validated"] == 1
            assert stats["working_200"] == 1
        await run_test("TC-01: Deduplicate destination URLs per job (validate unique destination once)", tc01())

        # TC-2: All original source relationships preserved in extracted_links and Link Audit sheet
        async def tc02():
            job_id = f"job_tc02_{uuid.uuid4().hex[:6]}"
            batch_id = f"batch_{uuid.uuid4().hex[:6]}"
            dest_url = "https://example.com/common-dest"
            norm_dest = URLNormalizer.normalize(dest_url)
            
            links = []
            for i in range(4):
                src = f"https://example.com/catalog/page-{i}"
                await db.update_source_classification(src, "PLP", "Loans", "meta")
                links.append({
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "source_url": src,
                    "original_url": dest_url,
                    "normalized_url": norm_dest,
                    "anchor_text": f"Apply Now {i}",
                    "component_name": "CardBanner",
                    "extracted_timestamp": datetime.now(timezone.utc).isoformat()
                })
            await insert_extracted_links(db, links)
            
            await db.batch_upsert_job_destinations([{
                "job_id": job_id,
                "normalized_url": norm_dest,
                "initial_status_code": 200,
                "final_status_code": 200,
                "status": "Working",
                "final_url": norm_dest
            }])

            rep_gen = ReportGenerator(db)
            report_file = f"test_out_{uuid.uuid4().hex[:6]}.xlsx"
            try:
                await rep_gen.generate_link_audit_report(job_id, report_file)
                wb = openpyxl.load_workbook(report_file)
                assert "Link Audit" in wb.sheetnames
                df_links = pd.read_excel(report_file, sheet_name="Link Audit")
                assert len(df_links) == 4, f"Expected 4 relationship occurrences, got {len(df_links)}"
                assert list(df_links["source_page_url"]) == [f"https://example.com/catalog/page-{i}" for i in range(4)]
                assert all(df_links["page_type"] == "PLP")
                assert all(df_links["business"] == "Loans")
            finally:
                if os.path.exists(report_file): os.remove(report_file)
        await run_test("TC-02: All original source relationships preserved in detailed Link Audit sheet", tc02())

        # TC-3 & TC-4: URL Normalization Rules
        async def tc03_04():
            url1 = "https://example.com/loans/apply#step-1"
            url2 = "https://example.com/loans/apply#step-2"
            url3 = "https://example.com/loans/apply"
            norm1 = URLNormalizer.normalize(url1)
            norm2 = URLNormalizer.normalize(url2)
            norm3 = URLNormalizer.normalize(url3)
            assert norm1 == norm2 == norm3, "URL fragments should be stripped during deduplication"

            q1 = "https://example.com/product?id=1"
            q2 = "https://example.com/product?id=2"
            assert URLNormalizer.normalize(q1) != URLNormalizer.normalize(q2), "Distinct query params must not be merged"
        await run_test("TC-03 & TC-04: Deduplication normalization strips fragments but preserves query params", tc03_04())

        # TC-5: Unique non-200 destination in Non-200 Responses sheet
        async def tc05():
            job_id = f"job_tc05_{uuid.uuid4().hex[:6]}"
            batch_id = f"batch_{uuid.uuid4().hex[:6]}"
            broken_url = "https://example.com/dead-link-404"
            norm_broken = URLNormalizer.normalize(broken_url)

            for i in range(3):
                src = f"https://example.com/source-{i}"
                await db.update_source_classification(src, "PDP", "Insurance", "meta")
            
            links = [
                {"job_id": job_id, "batch_id": batch_id, "source_url": "https://example.com/source-0", "original_url": broken_url, "normalized_url": norm_broken, "anchor_text": "Link 1"},
                {"job_id": job_id, "batch_id": batch_id, "source_url": "https://example.com/source-0", "original_url": broken_url, "normalized_url": norm_broken, "anchor_text": "Link 2"},
                {"job_id": job_id, "batch_id": batch_id, "source_url": "https://example.com/source-1", "original_url": broken_url, "normalized_url": norm_broken, "anchor_text": "Link 3"},
                {"job_id": job_id, "batch_id": batch_id, "source_url": "https://example.com/source-2", "original_url": broken_url, "normalized_url": norm_broken, "anchor_text": "Link 4"},
                {"job_id": job_id, "batch_id": batch_id, "source_url": "https://example.com/source-2", "original_url": broken_url, "normalized_url": norm_broken, "anchor_text": "Link 5"},
            ]
            await insert_extracted_links(db, links)

            await db.batch_upsert_job_destinations([{
                "job_id": job_id,
                "normalized_url": norm_broken,
                "initial_status_code": 404,
                "final_status_code": 404,
                "status": "Broken (Client Error)",
                "final_url": norm_broken,
                "error_type": "Client Error",
                "error_message": "404 Not Found"
            }])

            non_200s = await db.get_job_non_200_destinations(job_id)
            assert len(non_200s) == 1
            assert non_200s[0]["normalized_url"] == norm_broken
            assert non_200s[0]["affected_relationships_count"] == 5
            assert non_200s[0]["affected_sources_count"] == 3
            assert non_200s[0]["source_page_type"] == "PDP"
            assert non_200s[0]["business"] == "Insurance"

            rep_gen = ReportGenerator(db)
            report_file = f"test_out_{uuid.uuid4().hex[:6]}.xlsx"
            try:
                await rep_gen.generate_link_audit_report(job_id, report_file)
                df_non200 = pd.read_excel(report_file, sheet_name="Non-200 Responses")
                assert len(df_non200) == 1
                row = df_non200.iloc[0]
                assert row["destination_url"] == norm_broken
                assert row["http_status_code"] == 404
                assert row["status_category"] == "HTTP 4xx (Client Error)"
                assert row["affected_relationships_count"] == 5
                assert row["affected_sources_count"] == 3
                assert row["source_page_type"] == "PDP"
                assert row["business"] == "Insurance"
            finally:
                if os.path.exists(report_file): os.remove(report_file)
        await run_test("TC-05: Non-200 sheet contains unique failed destinations with impact counts", tc05())

        # TC-6, 7, 8: Exact Reconciliation and Categories
        async def tc06_07_08():
            job_id = f"job_tc06_{uuid.uuid4().hex[:6]}"
            batch_id = f"batch_{uuid.uuid4().hex[:6]}"

            test_cases = [
                ("https://example.com/ok-200", 200, "Working", "", "", 10),
                ("https://example.com/redir-301", 301, "Redirect", "", "https://example.com/dest", 5),
                ("https://example.com/err-403", 403, "Forbidden", "Client Error", "", 4),
                ("https://example.com/err-404", 404, "Not Found", "Client Error", "", 6),
                ("https://example.com/err-500", 500, "Server Error", "Server Error", "", 3),
                ("https://example.com/timeout", "TIMEOUT", "Timeout", "Timeout", "", 2),
                ("https://example.com/blocked", None, "Blocked", "SSRF Blocked", "", 1),
            ]

            links = []
            dest_records = []
            total_expected_rels = sum(tc[5] for tc in test_cases)
            total_expected_unique = len(test_cases)

            for idx, (url, code, stat, err, f_url, count) in enumerate(test_cases):
                norm = URLNormalizer.normalize(url)
                src = f"https://example.com/source-{idx % 3}"
                pt = ["PLP", "PDP", "Article"][idx % 3]
                biz = ["Loans", "Cards", "Investments"][idx % 3]
                await db.update_source_classification(src, pt, biz, "meta")

                for c in range(count):
                    links.append({
                        "job_id": job_id,
                        "batch_id": batch_id,
                        "source_url": src,
                        "original_url": url,
                        "normalized_url": norm,
                        "anchor_text": f"Anchor {idx}_{c}",
                        "component_name": "BodyContent"
                    })

                dest_records.append({
                    "job_id": job_id,
                    "normalized_url": norm,
                    "final_status_code": code if isinstance(code, int) else None,
                    "status": stat,
                    "error_type": err,
                    "final_url": f_url or norm
                })

            await insert_extracted_links(db, links)
            await db.batch_upsert_job_destinations(dest_records)

            rep_gen = ReportGenerator(db)
            report_file = f"test_out_{uuid.uuid4().hex[:6]}.xlsx"
            try:
                await rep_gen.generate_link_audit_report(job_id, report_file)
                wb = openpyxl.load_workbook(report_file)
                ws_pivot = wb["Pivot Summary"]

                sec_a_rows = []
                total_unique_cell = None
                total_rels_cell = None
                for r in range(4, 25):
                    val = ws_pivot.cell(row=r, column=1).value
                    if val in [
                        "HTTP 200 (Success)", "HTTP 3xx (Redirect)", "HTTP 4xx (Client Error)",
                        "HTTP 5xx (Server Error)", "Timeout", "DNS / Connection Error",
                        "Blocked / Skipped", "Other / Inconclusive"
                    ]:
                        u_cnt = ws_pivot.cell(row=r, column=2).value
                        r_cnt = ws_pivot.cell(row=r, column=4).value
                        sec_a_rows.append((val, u_cnt, r_cnt))
                    elif val == "Total":
                        total_unique_cell = ws_pivot.cell(row=r, column=2).value
                        total_rels_cell = ws_pivot.cell(row=r, column=4).value
                        break

                assert len(sec_a_rows) == 8, f"Expected 8 categories in Section A, got {len(sec_a_rows)}"
                sum_unique = sum(r[1] for r in sec_a_rows)
                sum_rels = sum(r[2] for r in sec_a_rows)

                assert sum_unique == total_expected_unique, f"Sum unique ({sum_unique}) != expected ({total_expected_unique})"
                assert sum_rels == total_expected_rels, f"Sum rels ({sum_rels}) != expected ({total_expected_rels})"
                assert total_unique_cell == total_expected_unique
                assert total_rels_cell == total_expected_rels
            finally:
                if os.path.exists(report_file): os.remove(report_file)
        await run_test("TC-06, 07, 08: Exact mathematical reconciliation in Pivot Summary Section A", tc06_07_08())

        # TC-9: Source page type and business derived from source page URL
        async def tc09():
            job_id = f"job_tc09_{uuid.uuid4().hex[:6]}"
            batch_id = f"batch_{uuid.uuid4().hex[:6]}"
            src1 = "https://example.com/personal-loan"
            src2 = "https://example.com/credit-cards/platinum"
            dest = "https://example.com/common-apply"
            norm_dest = URLNormalizer.normalize(dest)

            await db.update_source_classification(src1, "PLP", "Loans", "data-pageType")
            await db.update_source_classification(src2, "PDP", "Cards", "data-pageType")

            links = [
                {"job_id": job_id, "batch_id": batch_id, "source_url": src1, "original_url": dest, "normalized_url": norm_dest},
                {"job_id": job_id, "batch_id": batch_id, "source_url": src2, "original_url": dest, "normalized_url": norm_dest},
            ]
            await insert_extracted_links(db, links)
            await db.batch_upsert_job_destinations([{
                "job_id": job_id,
                "normalized_url": norm_dest,
                "final_status_code": 200,
                "status": "Working"
            }])

            rep_gen = ReportGenerator(db)
            report_file = f"test_out_{uuid.uuid4().hex[:6]}.xlsx"
            try:
                await rep_gen.generate_link_audit_report(job_id, report_file)
                df_links = pd.read_excel(report_file, sheet_name="Link Audit")
                row1 = df_links[df_links["source_page_url"] == src1].iloc[0]
                assert row1["page_type"] == "PLP" and row1["business"] == "Loans"
                row2 = df_links[df_links["source_page_url"] == src2].iloc[0]
                assert row2["page_type"] == "PDP" and row2["business"] == "Cards"
            finally:
                if os.path.exists(report_file): os.remove(report_file)
        await run_test("TC-09: Source page classification properly assigned per source URL", tc09())

        # TC-10: Empty audit job handling
        async def tc10():
            job_id = f"job_empty_{uuid.uuid4().hex[:6]}"
            rep_gen = ReportGenerator(db)
            report_file = f"test_out_{uuid.uuid4().hex[:6]}.xlsx"
            try:
                await rep_gen.generate_link_audit_report(job_id, report_file)
                assert os.path.exists(report_file)
                wb = openpyxl.load_workbook(report_file)
                assert "Pivot Summary" in wb.sheetnames
                assert "Non-200 Responses" in wb.sheetnames
                assert "Link Audit" in wb.sheetnames
            finally:
                if os.path.exists(report_file): os.remove(report_file)
        await run_test("TC-10: Empty audit jobs generate valid empty workbooks without crash", tc10())

        # TC-11: Retries idempotent in job_destinations
        async def tc11():
            job_id = f"job_tc11_{uuid.uuid4().hex[:6]}"
            norm_url = "https://example.com/retry-target"

            await db.batch_upsert_job_destinations([{
                "job_id": job_id,
                "normalized_url": norm_url,
                "final_status_code": None,
                "status": "Timeout",
                "error_type": "Timeout",
                "retries_count": 0
            }])

            dests = await db.get_job_destinations(job_id)
            assert len(dests) == 1
            assert dests[0]["status"] == "Timeout"

            await db.batch_upsert_job_destinations([{
                "job_id": job_id,
                "normalized_url": norm_url,
                "final_status_code": 200,
                "status": "Working",
                "error_type": "",
                "retries_count": 1
            }])

            dests_after = await db.get_job_destinations(job_id)
            assert len(dests_after) == 1, "Duplicate record created on retry!"
            assert dests_after[0]["status"] == "Working"
            assert dests_after[0]["final_status_code"] == 200
            assert dests_after[0]["retries_count"] == 1
        await run_test("TC-11: Retries update existing records without creating duplicates", tc11())

        # TC-12: Sections C, D, E, F coverage in Pivot Summary
        async def tc12():
            job_id = f"job_tc12_{uuid.uuid4().hex[:6]}"
            batch_id = f"batch_{uuid.uuid4().hex[:6]}"
            
            src1 = "https://example.com/plp1"
            src2 = "https://example.com/pdp1"
            await db.update_source_classification(src1, "PLP", "Loans", "meta")
            await db.update_source_classification(src2, "PDP", "Cards", "meta")

            d1 = "https://example.com/dest-ok"
            d2 = "https://example.com/dest-fail"
            norm1 = URLNormalizer.normalize(d1)
            norm2 = URLNormalizer.normalize(d2)

            links = [
                {"job_id": job_id, "batch_id": batch_id, "source_url": src1, "original_url": d1, "normalized_url": norm1},
                {"job_id": job_id, "batch_id": batch_id, "source_url": src1, "original_url": d2, "normalized_url": norm2},
                {"job_id": job_id, "batch_id": batch_id, "source_url": src2, "original_url": d2, "normalized_url": norm2},
            ]
            await insert_extracted_links(db, links)

            await db.batch_upsert_job_destinations([
                {"job_id": job_id, "normalized_url": norm1, "final_status_code": 200, "status": "Working"},
                {"job_id": job_id, "normalized_url": norm2, "final_status_code": 500, "status": "Server Error", "error_type": "Server Error"}
            ])

            rep_gen = ReportGenerator(db)
            report_file = f"test_out_{uuid.uuid4().hex[:6]}.xlsx"
            try:
                await rep_gen.generate_link_audit_report(job_id, report_file)
                wb = openpyxl.load_workbook(report_file)
                ws = wb["Pivot Summary"]
                
                content = [ws.cell(row=r, column=1).value for r in range(1, ws.max_row + 1)]
                assert any("SECTION A" in str(c) for c in content)
                assert any("SECTION B" in str(c) for c in content)
                assert any("SECTION C" in str(c) for c in content)
                assert any("SECTION D" in str(c) for c in content)
                assert any("SECTION E" in str(c) for c in content)
                assert any("SECTION F" in str(c) for c in content)
            finally:
                if os.path.exists(report_file): os.remove(report_file)
        await run_test("TC-12: Pivot Summary contains Sections A, B, C, D, E, and F formatted tables", tc12())

        # TC-13: Preserves image_audit single-sheet formatting
        async def tc13():
            job_id = f"job_img_{uuid.uuid4().hex[:6]}"
            batch_id = f"batch_{uuid.uuid4().hex[:6]}"
            src = "https://example.com/page"
            img_url = "https://example.com/banner.png"
            norm_img = URLNormalizer.normalize(img_url)

            await insert_extracted_links(db, [{
                "job_id": job_id,
                "batch_id": batch_id,
                "source_url": src,
                "original_url": img_url,
                "normalized_url": norm_img,
                "image_name": "banner.png",
                "loading_type": "eager"
            }])

            rep_gen = ReportGenerator(db)
            report_file = f"test_out_{uuid.uuid4().hex[:6]}.xlsx"
            try:
                await rep_gen.generate_report(job_id, "image_audit", report_file)
                wb = openpyxl.load_workbook(report_file)
                assert "Image Audit" in wb.sheetnames
                assert len(wb.sheetnames) == 1, "Image audit must remain single sheet"
            finally:
                if os.path.exists(report_file): os.remove(report_file)
        await run_test("TC-13: Image Audit single-sheet format remains strictly unregressed", tc13())

    finally:
        await db.close()
        for f in [db_file, f"{db_file}-wal", f"{db_file}-shm"]:
            if os.path.exists(f):
                try: os.remove(f)
                except OSError: pass

    print("=" * 70)
    print(f"RESULTS: {passed} PASSED, {failed} FAILED")
    print("=" * 70)
    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_all_tests())
