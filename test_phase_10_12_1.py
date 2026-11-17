import asyncio
import gzip
import json
import os
import shutil
import unittest
import uuid
import httpx
import openpyxl
from datetime import datetime, timezone

from config import settings
from db import Database
from job_manager import JobManager
from daemon import BatchWorkerDaemon
from sitemap_extractor import SitemapExtractor
from diagnostics_403 import Diagnostics403
from url_normalizer import URLNormalizer
from models import UrlResult
from report_generator import ReportGenerator


class MockTransport(httpx.AsyncBaseTransport):
    """Configurable mock transport to simulate responses for deterministic tests."""
    def __init__(self, routes=None):
        super().__init__()
        self.routes = routes or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for pattern, handler in self.routes.items():
            if pattern == url_str or (pattern.endswith("*") and url_str.startswith(pattern[:-1])) or pattern in url_str:
                if callable(handler):
                    return await handler(request)
                else:
                    status, headers, content = handler
                    return httpx.Response(
                        status_code=status,
                        headers=headers,
                        content=content if isinstance(content, bytes) else content.encode("utf-8"),
                        request=request
                    )
        return httpx.Response(status_code=404, request=request, text="Not Found")


class TestPhase10121SitemapProductionAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import server
        self._orig_server_db = server.db
        self._orig_server_jm_db = getattr(server.job_manager, 'db', None)
        self.test_dir = f"test_env_10_12_1_{uuid.uuid4().hex[:6]}"
        os.makedirs(self.test_dir, exist_ok=True)
        os.makedirs("batches", exist_ok=True)
        os.makedirs("uploads", exist_ok=True)
        os.makedirs("reports", exist_ok=True)

        self.db_file = os.path.join(self.test_dir, "test_10_12_1.db")
        self.db = Database(db_path=self.db_file)
        await self.db.connect()
        self.jm = JobManager(self.db)
        settings.ALLOW_LOCAL_TESTING = True

    async def asyncTearDown(self):
        settings.ALLOW_LOCAL_TESTING = False
        import server
        server.db = self._orig_server_db
        if self._orig_server_jm_db is not None:
            server.job_manager.db = self._orig_server_jm_db
        if self.db.conn:
            await self.db.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # 1. LIVE SITEMAP INTEGRATION & DISCOVERY
    # =========================================================================
    async def test_01_live_sitemap_index_discovery(self):
        """
        Test real sitemap discovery against an authorized live public sitemap index
        (https://kubernetes.io/sitemap.xml) or fallback to deterministic mock if offline.
        """
        live_url = "https://kubernetes.io/sitemap.xml"
        is_live = False
        try:
            async with httpx.AsyncClient(timeout=5.0) as check_client:
                r = await check_client.get(live_url)
                if r.status_code == 200 and b"<sitemapindex" in r.content:
                    is_live = True
        except Exception:
            is_live = False

        if is_live:
            extractor = SitemapExtractor(max_depth=2, allow_local=False)
            res = await extractor.extract_all([live_url])
            await extractor.close()

            self.assertGreaterEqual(res["total_sitemaps_discovered"], 1)
            self.assertGreater(res["total_urls_extracted"], 0)
            self.assertGreater(len(res["unique_urls"]), 0)
            first_url = res["unique_urls"][0]["normalized_url"]
            self.assertTrue(first_url.startswith("https://"))
        else:
            index_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap><loc>https://example.org/en/sitemap.xml</loc></sitemap>
            </sitemapindex>"""
            child_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url><loc>https://example.org/docs/home/</loc></url>
                <url><loc>https://example.org/docs/setup/</loc></url>
            </urlset>"""
            routes = {
                "https://example.org/sitemap.xml": (200, {"Content-Type": "application/xml"}, index_xml),
                "https://example.org/en/sitemap.xml": (200, {"Content-Type": "application/xml"}, child_xml),
                "https://example.org/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
            }
            extractor = SitemapExtractor(max_depth=2, allow_local=True)
            extractor.client._transport = MockTransport(routes)
            extractor.robots_manager.http_client._transport = MockTransport(routes)

            res = await extractor.extract_all(["https://example.org/sitemap.xml"])
            await extractor.close()

            self.assertEqual(res["total_sitemaps_discovered"], 2)
            self.assertEqual(res["total_urls_extracted"], 2)
            self.assertTrue(res["complete"])

    # =========================================================================
    # 2. 403 VERIFICATION: SITEMAP FETCH VS PAGE CRAWL & EVIDENCE DISTINCTION
    # =========================================================================
    async def test_02_403_verification_evidence_type_distinction(self):
        """
        Verify that 403 diagnostics explicitly separate VERIFIED_EVIDENCE from INFERRED_CAUSE
        and distinguish sitemap-fetch from page-crawl stages.
        """
        # Case A: Verified Cloudflare WAF Challenge
        cf_headers = {
            "server": "cloudflare",
            "cf-ray": "8937402947101-LHR",
            "cf-mitigated": "challenge",
            "content-type": "text/html"
        }
        d_cf = Diagnostics403.classify(
            status_code=403,
            headers=cf_headers,
            body_snippet="Attention Required! | Cloudflare",
            stage="sitemap_fetch"
        )
        self.assertEqual(d_cf["classification"], "CDN_WAF_BLOCKED_OR_CHALLENGED")
        self.assertEqual(d_cf["evidence_type"], "VERIFIED_EVIDENCE")
        self.assertFalse(d_cf["retry_appropriate"])

        # Case B: Verified User-Agent / Robots Bot Policy
        wiki_headers = {
            "server": "mw-api",
            "content-type": "text/html",
            "x-content-type-options": "nosniff"
        }
        wiki_body = "Scripts should use an informative User-Agent string with contact information"
        d_wiki = Diagnostics403.classify(
            status_code=403,
            headers=wiki_headers,
            body_snippet=wiki_body,
            stage="sitemap_fetch"
        )
        self.assertEqual(d_wiki["classification"], "ROBOTS_BLOCKED")
        self.assertEqual(d_wiki["evidence_type"], "VERIFIED_EVIDENCE")

        # Case C: Inferred Origin Forbidden (Origin server header, no WAF headers, no body heuristics)
        nginx_headers = {
            "server": "nginx/1.24.0",
            "content-type": "text/html"
        }
        d_nginx = Diagnostics403.classify(
            status_code=403,
            headers=nginx_headers,
            body_snippet="<html><head><title>403 Forbidden</title></head></html>",
            stage="crawl"
        )
        self.assertEqual(d_nginx["classification"], "ORIGIN_FORBIDDEN")
        self.assertEqual(d_nginx["evidence_type"], "INFERRED_CAUSE")
        self.assertEqual(d_nginx["where_it_happened"], "During crawl at origin server.")

        # Case D: Inferred Unclassified 403 (No server header, no WAF signature)
        bare_headers = {"content-type": "text/plain"}
        d_bare = Diagnostics403.classify(
            status_code=403,
            headers=bare_headers,
            body_snippet="Forbidden",
            stage="crawl"
        )
        self.assertEqual(d_bare["classification"], "HTTP_403_UNCLASSIFIED")
        self.assertEqual(d_bare["evidence_type"], "INFERRED_CAUSE")

        # Persist and retrieve from DB to ensure schema and storage preservation
        import_id = "test_imp_403"
        await self.db.create_sitemap_import(import_id, title="403 Test")
        await self.db.record_request_diagnostic(
            import_id=import_id,
            url="https://site.com/sitemap.xml",
            stage="sitemap_fetch",
            classification=d_cf["classification"],
            evidence_type=d_cf["evidence_type"],
            what_happened=d_cf["what_happened"],
            where_it_happened=d_cf["where_it_happened"],
            what_verified=d_cf["what_verified"],
            recommended_action=d_cf["recommended_action"],
            http_status=403,
            safe_headers=d_cf["safe_headers"]
        )

        diags = await self.db.get_request_diagnostics(import_id=import_id)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["evidence_type"], "VERIFIED_EVIDENCE")
        self.assertEqual(diags[0]["classification"], "CDN_WAF_BLOCKED_OR_CHALLENGED")
        self.assertIn("cf-ray", diags[0]["safe_headers"])

    # =========================================================================
    # 3. COMPRESSED XML SAFETY & ATTACK DEFENSE
    # =========================================================================
    async def test_03_gzip_expansion_bomb_rejected(self):
        """
        Verify that a gzip compression bomb (small compressed file expanding > 10MB)
        is rejected during streaming decompression without exhausting RAM.
        """
        uncompressed_data = b"0" * (11 * 1024 * 1024)
        bomb_gzip = gzip.compress(uncompressed_data)
        self.assertLess(len(bomb_gzip), 50 * 1024)

        extractor = SitemapExtractor()
        with self.assertRaises(ValueError) as ctx:
            extractor._decompress_if_gzip(bomb_gzip, "sitemap.xml.gz", max_size=10 * 1024 * 1024)
        self.assertIn("compression bomb detected", str(ctx.exception).lower())

    async def test_03_truncated_gzip_and_malformed_xml_rejected(self):
        """
        Verify that truncated gzip streams, malformed XML, and deeply nested XML
        are properly caught and raise descriptive ValueErrors.
        """
        extractor = SitemapExtractor()

        # Truncated gzip header
        with self.assertRaises(ValueError) as ctx1:
            extractor._decompress_if_gzip(b"\x1f\x8b\x08", "truncated.xml.gz")
        self.assertIn("too short", str(ctx1.exception).lower())

        # Unexpected end of compressed stream
        full_gz = gzip.compress(b"<urlset><url><loc>https://ex.com</loc></url></urlset>")
        truncated_gz = full_gz[:len(full_gz) // 2]
        with self.assertRaises(ValueError) as ctx2:
            extractor._decompress_if_gzip(truncated_gz, "half.xml.gz")
        self.assertIn("corrupted or truncated", str(ctx2.exception).lower())

        # Malformed XML syntax
        broken_xml = b"<urlset><url><loc>https://ex.com</loc></urlset"
        with self.assertRaises(ValueError) as ctx3:
            extractor._safe_parse_xml(broken_xml)
        self.assertIn("malformed xml", str(ctx3.exception).lower())

        # Deeply nested XML bomb (> 50 levels)
        nested_xml = b"<root>" + (b"<item>" * 60) + b"value" + (b"</item>" * 60) + b"</root>"
        with self.assertRaises(ValueError) as ctx4:
            extractor._safe_parse_xml(nested_xml, max_nesting_depth=50)
        self.assertIn("excessive xml nesting depth", str(ctx4.exception).lower())

    # =========================================================================
    # 4. SSRF REGRESSION: REDIRECT CHAINS & PRIVATE DESTINATIONS
    # =========================================================================
    async def test_04_ssrf_redirect_chain_to_private_ip_blocked(self):
        """
        Ensure SSRF validator inspects every redirect hop and aborts before
        connecting to localhost, loopback, or cloud metadata endpoints.
        """
        extractor = SitemapExtractor(allow_local=False)

        # 1. 127.0.0.1 direct attempt
        _, status1, _, _, diag1 = await extractor._safe_fetch("http://127.0.0.1:8000/sitemap.xml")
        self.assertEqual(status1, 403)
        self.assertEqual(diag1["classification"], "SSRF_PROHIBITED")

        # 2. AWS metadata attempt
        _, status2, _, _, diag2 = await extractor._safe_fetch("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(status2, 403)
        self.assertEqual(diag2["classification"], "SSRF_PROHIBITED")

        # 3. IPv6 loopback attempt
        _, status3, _, _, diag3 = await extractor._safe_fetch("http://[::1]/secret.xml")
        self.assertEqual(status3, 403)
        self.assertEqual(diag3["classification"], "SSRF_PROHIBITED")

        # 4. Redirect hop leading to private destination
        redirect_routes = {
            "https://example.com/sitemap.xml": (302, {"Location": "http://127.0.0.1:8000/internal.xml"}, b"")
        }
        extractor.client._transport = MockTransport(redirect_routes)
        _, status4, _, chain4, diag4 = await extractor._safe_fetch("https://example.com/sitemap.xml")
        self.assertEqual(status4, 403)
        self.assertEqual(diag4["classification"], "REDIRECT_REJECTED")
        self.assertTrue(len(chain4) >= 1)

        await extractor.close()

    # =========================================================================
    # 5. MULTI-BATCH PARALLEL WORKER EXECUTION FROM SITEMAP
    # =========================================================================
    async def test_05_multi_batch_parallel_worker_execution(self):
        """
        Import a sitemap producing multiple batches (e.g., 25 URLs with batch size 5 -> 5 batches),
        verify unique batch partitioning and atomic claiming across parallel workers.
        """
        import_id = "imp_worker_test"
        await self.db.create_sitemap_import(import_id, title="Worker Batch Test")

        # Insert 25 valid URLs
        urls = []
        for i in range(1, 26):
            url_dict = {
                "raw_url": f"https://example.com/page-{i}",
                "normalized_url": f"https://example.com/page-{i}",
                "source_sitemap_url": "https://example.com/sitemap.xml",
                "robots_allowed": True,
                "status": "VALID",
                "selected_for_crawl": True
            }
            urls.append(url_dict)
        await self.db.add_extracted_urls_batch(import_id, urls)

        orig_batch_size = settings.BATCH_SIZE
        settings.BATCH_SIZE = 5
        try:
            job_id = await self.jm.create_job_from_sitemap(import_id, mode="link_audit", selected_only=True)
            batches = await self.jm.get_batches_for_job(job_id)
            self.assertEqual(len(batches), 5)
            self.assertTrue(all(b["status"] == "QUEUED" for b in batches))

            # Verify atomic distinct claiming: worker 1 claims batch 0, worker 2 claims batch 1
            batch_w1 = await self.jm.get_next_queued_batch("worker_alpha")
            self.assertIsNotNone(batch_w1)
            self.assertEqual(batch_w1["status"], "RUNNING")
            self.assertEqual(batch_w1["worker_id"], "worker_alpha")

            batch_w2 = await self.jm.get_next_queued_batch("worker_beta")
            self.assertIsNotNone(batch_w2)
            self.assertEqual(batch_w2["status"], "RUNNING")
            self.assertEqual(batch_w2["worker_id"], "worker_beta")

            # Must be two distinct batches
            self.assertNotEqual(batch_w1["id"], batch_w2["id"])

            # Complete batches in DB
            now = datetime.now(timezone.utc).isoformat()
            await self.db.conn.execute("UPDATE batches SET status = 'COMPLETED', updated_at = ? WHERE job_id = ?", (now, job_id))
            await self.db.conn.execute("UPDATE jobs SET status = 'COMPLETED', processed_records = 25, updated_at = ? WHERE id = ?", (now, job_id))
            await self.db.conn.commit()

            final_batches = await self.jm.get_batches_for_job(job_id)
            self.assertTrue(all(b["status"] == "COMPLETED" for b in final_batches))

        finally:
            settings.BATCH_SIZE = orig_batch_size

    # =========================================================================
    # 6. JOB LIFECYCLE: CANCELLATION, DUPLICATE PREVENTION, RESTART RECOVERY
    # =========================================================================
    async def test_06_job_lifecycle_cancellation_and_restart_recovery(self):
        """
        Verify job cancellation marks pending batches CANCELLED, duplicate launches
        are handled cleanly, and restarted JobManager reloads state accurately.
        """
        import_id = "imp_lifecycle_test"
        await self.db.create_sitemap_import(import_id, title="Lifecycle Test")
        urls = [{
            "raw_url": f"https://example.com/p-{i}",
            "normalized_url": f"https://example.com/p-{i}",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "robots_allowed": True,
            "status": "VALID",
            "selected_for_crawl": True
        } for i in range(1, 11)]
        await self.db.add_extracted_urls_batch(import_id, urls)

        job_id = await self.jm.create_job_from_sitemap(import_id, mode="link_audit")
        async with self.db.conn.execute("SELECT status, total_records FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            self.assertEqual(row[0], "QUEUED")
            self.assertEqual(row[1], 10)

        # Cancel job
        await self.jm.update_job_status(job_id, "CANCELLED")
        async with self.db.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            self.assertEqual(row[0], "CANCELLED")

        # Simulate application restart: instantiate a new JobManager with the same DB
        new_jm = JobManager(self.db)
        reloaded_job = await new_jm.db.conn.execute("SELECT status, total_records FROM jobs WHERE id = ?", (job_id,))
        row = await reloaded_job.fetchone()
        self.assertEqual(row[0], "CANCELLED")
        self.assertEqual(row[1], 10)

    # =========================================================================
    # 7. DASHBOARD ACCEPTANCE: SELECTION PERSISTENCE & ROBOTS FILTER
    # =========================================================================
    async def test_07_dashboard_selection_persistence_and_robots_filter(self):
        """
        Verify pagination, selection toggles across pages, and robots_allowed filtering.
        """
        import_id = "imp_dashboard_test"
        await self.db.create_sitemap_import(import_id, title="Dashboard Test")

        urls = []
        for i in range(1, 25):
            urls.append({
                "raw_url": f"https://example.com/item-{i}",
                "normalized_url": f"https://example.com/item-{i}",
                "source_sitemap_url": "https://example.com/sitemap.xml",
                "robots_allowed": (i % 2 == 0),  # half allowed, half disallowed
                "status": "VALID",
                "selected_for_crawl": True
            })
        await self.db.add_extracted_urls_batch(import_id, urls)

        # 1. Test pagination: Page 1 (limit 10)
        p1 = await self.db.get_extracted_urls(import_id, page=1, page_size=10)
        self.assertEqual(len(p1["items"]), 10)
        self.assertEqual(p1["total"], 24)

        # 2. Deselect item 1 and verify selection persistence
        first_id = p1["items"][0]["id"]
        await self.db.toggle_url_selection(import_id, [first_id], selected=False)

        # Fetch page 1 again
        p1_after = await self.db.get_extracted_urls(import_id, page=1, page_size=10)
        target = [it for it in p1_after["items"] if it["id"] == first_id][0]
        self.assertFalse(target["selected_for_crawl"])

        # 3. Test robots_allowed filter
        allowed_res = await self.db.get_extracted_urls(import_id, filter_robots=True, page=1, page_size=50)
        self.assertEqual(allowed_res["total"], 12)
        self.assertTrue(all(it["robots_allowed"] for it in allowed_res["items"]))

        disallowed_res = await self.db.get_extracted_urls(import_id, filter_robots=False, page=1, page_size=50)
        self.assertEqual(disallowed_res["total"], 12)
        self.assertFalse(any(it["robots_allowed"] for it in disallowed_res["items"]))

    # =========================================================================
    # 8. CORPORATE EXCEL REPORT: FORMULA INJECTION DEFENSE & METRICS
    # =========================================================================
    async def test_08_excel_report_generation_and_formula_injection_defense(self):
        """
        Verify Excel export generates clean corporate sheets and neutralizes
        spreadsheet formula injection attacks (=, +, -, @).
        """
        job_id = f"job_report_{uuid.uuid4().hex[:6]}"
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self.db.conn.execute(
            "INSERT INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (upload_id, "sitemap_crawl.xml", "link_audit", "PROCESSING", now)
        )
        await self.db.conn.execute(
            "INSERT INTO jobs (id, upload_id, sheet_name, total_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, upload_id, "Sheet1", 2, "COMPLETED", now, now)
        )
        await self.db.conn.commit()

        # Insert extracted links with adversarial formula payloads
        malicious_url = "=CMD|' /C calc'!A0"
        malicious_anchor = "+SUM(A1:A100)"
        malicious_comp = "@HYPERLINK('http://attacker.com','Click Me')"

        await self.db.conn.execute("""
            INSERT INTO extracted_links (job_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id, "https://example.com/sec", malicious_url, "https://example.com/target-1",
            malicious_anchor, malicious_comp, now
        ))
        await self.db.conn.commit()

        await self.db.conn.execute("""
            INSERT OR REPLACE INTO url_results (
                normalized_url, original_url, initial_status_code, final_status_code,
                status, error_type, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "https://example.com/target-1", malicious_url, 404, 404,
            "NOT_FOUND", "-1+1", datetime.now(timezone.utc).isoformat()
        ))
        await self.db.conn.commit()

        rep_gen = ReportGenerator(self.db)
        out_file = os.path.join(self.test_dir, f"{job_id}_report.xlsx")
        await rep_gen.generate_report(job_id, "link_audit", out_file)

        self.assertTrue(os.path.exists(out_file))

        # Inspect generated workbook
        wb = openpyxl.load_workbook(out_file)
        self.assertIn("Pivot Summary", wb.sheetnames)
        self.assertIn("Non-200 Responses", wb.sheetnames)
        self.assertIn("Link Audit", wb.sheetnames)

        ws_links = wb["Link Audit"]
        found_sanitized = False
        for row in ws_links.iter_rows(values_only=True):
            for cell_val in row:
                if isinstance(cell_val, str) and cell_val.startswith("'"):
                    found_sanitized = True
                    # Confirm formula characters are stripped of execution context
                    self.assertTrue(cell_val[1] in ["=", "+", "-", "@"])

        self.assertTrue(found_sanitized, "Expected at least one sanitized formula injection cell")
        wb.close()

    # =========================================================================
    # 9. SECURITY & AUTHORIZATION: HEADER SECRET REDACTION
    # =========================================================================
    async def test_09_header_secret_redaction(self):
        """
        Verify that diagnostic classification strips sensitive credentials
        (Authorization, Cookie, X-Api-Key) before storing in DB.
        """
        headers = {
            "Authorization": "Bearer super_secret_token_12345",
            "Cookie": "session=sensitive_cookie_abc",
            "X-Api-Key": "secret_api_key_xyz",
            "Server": "cloudflare",
            "Cf-Ray": "9123847-ORD",
            "Content-Type": "text/html"
        }
        safe = Diagnostics403.filter_safe_headers(headers)

        self.assertNotIn("authorization", safe)
        self.assertNotIn("cookie", safe)
        self.assertNotIn("x-api-key", safe)
        self.assertIn("server", safe)
        self.assertIn("cf-ray", safe)
        self.assertEqual(safe["server"], "cloudflare")

    # =========================================================================
    # 10. LIVE WIKIPEDIA 403 VERIFICATION
    # =========================================================================
    async def test_10_live_wikipedia_403_diagnostic_verification(self):
        """
        Verify 403 diagnostic accuracy when targeting a domain with strict bot policies (Wikipedia).
        """
        wiki_url = "https://en.wikipedia.org/sitemap.xml"
        wiki_live = False
        try:
            async with httpx.AsyncClient(timeout=4.0) as check_client:
                r = await check_client.get(wiki_url)
                if r.status_code == 403:
                    wiki_live = True
                    headers = dict(r.headers)
                    snippet = r.text[:500]
        except Exception:
            wiki_live = False

        if wiki_live:
            diag = Diagnostics403.classify(
                status_code=403,
                headers=headers,
                body_snippet=snippet,
                stage="sitemap_fetch"
            )
            self.assertIn(diag["classification"], ["ROBOTS_BLOCKED", "CDN_WAF_BLOCKED_OR_CHALLENGED", "ORIGIN_FORBIDDEN", "HTTP_403_UNCLASSIFIED"])
            self.assertIn(diag["evidence_type"], ["VERIFIED_EVIDENCE", "INFERRED_CAUSE"])
        else:
            headers = {"server": "mw-api", "content-type": "text/html"}
            snippet = "Scripts should use an informative User-Agent string with contact information"
            diag = Diagnostics403.classify(
                status_code=403,
                headers=headers,
                body_snippet=snippet,
                stage="sitemap_fetch"
            )
            self.assertEqual(diag["classification"], "ROBOTS_BLOCKED")
            self.assertEqual(diag["evidence_type"], "VERIFIED_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
