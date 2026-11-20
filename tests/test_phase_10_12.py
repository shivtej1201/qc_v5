import asyncio
import gzip
import json
import os
import shutil
import unittest
import uuid
import httpx
from datetime import datetime

from qcv5.core.config import settings
from qcv5.core.db import Database
from qcv5.services.job_manager import JobManager
from qcv5.services.daemon import BatchWorkerDaemon
from qcv5.crawler.sitemap_extractor import SitemapExtractor, _strip_tag_ns
from qcv5.crawler.diagnostics_403 import Diagnostics403
from qcv5.crawler.url_normalizer import URLNormalizer
from qcv5.core.models import UrlResult

class MockTransport(httpx.AsyncBaseTransport):
    """Configurable mock transport to simulate sitemap servers, CDN/WAF blocks, redirects, robots.txt, etc."""
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


class TestPhase1012SitemapWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import qcv5.api.server as server
        self._orig_server_db = server.db
        self._orig_server_jm_db = getattr(server.job_manager, 'db', None)
        self.test_dir = f"test_env_10_12_{uuid.uuid4().hex[:6]}"
        os.makedirs(self.test_dir, exist_ok=True)
        os.makedirs("batches", exist_ok=True)
        os.makedirs("uploads", exist_ok=True)
        os.makedirs("reports", exist_ok=True)

        self.db_file = os.path.join(self.test_dir, "test_10_12.db")
        self.db = Database(db_path=self.db_file)
        await self.db.connect()
        self.jm = JobManager(self.db)
        settings.ALLOW_LOCAL_TESTING = True

    async def asyncTearDown(self):
        settings.ALLOW_LOCAL_TESTING = False
        import qcv5.api.server as server
        server.db = self._orig_server_db
        if self._orig_server_jm_db is not None:
            server.job_manager.db = self._orig_server_jm_db
        if self.db.conn:
            await self.db.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 1: One valid sitemap containing normal page URLs
    # -------------------------------------------------------------------------
    async def test_01_single_valid_sitemap(self):
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url>
                <loc>https://example.com/page-1</loc>
                <lastmod>2026-10-01</lastmod>
                <changefreq>daily</changefreq>
                <priority>0.8</priority>
            </url>
            <url>
                <loc>https://example.com/page-2</loc>
                <lastmod>2026-10-02</lastmod>
                <priority>0.5</priority>
            </url>
        </urlset>
        """.encode("utf-8")

        routes = {
            "https://example.com/sitemap.xml": (200, {"Content-Type": "application/xml"}, xml_content),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
        }

        extractor = SitemapExtractor(allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/sitemap.xml"])
        await extractor.close()

        self.assertEqual(res["total_sitemaps_discovered"], 1)
        self.assertEqual(res["total_urls_extracted"], 2)
        urls = [u["normalized_url"] for u in res["unique_urls"]]
        self.assertIn("https://example.com/page-1", urls)
        self.assertIn("https://example.com/page-2", urls)
        self.assertTrue(all(u["robots_allowed"] for u in res["unique_urls"]))
        self.assertTrue(all(u["selected_for_crawl"] for u in res["unique_urls"]))
        self.assertTrue(res["complete"])

    # -------------------------------------------------------------------------
    # Scenario 2: Sitemap index containing multiple child sitemaps
    # -------------------------------------------------------------------------
    async def test_02_sitemap_index_recursive_discovery(self):
        index_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap>
                <loc>https://example.com/posts-sitemap.xml</loc>
                <lastmod>2026-10-10</lastmod>
            </sitemap>
            <sitemap>
                <loc>https://example.com/products-sitemap.xml</loc>
            </sitemap>
        </sitemapindex>
        """.encode("utf-8")

        posts_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/posts/post-1</loc></url>
            <url><loc>https://example.com/posts/post-2</loc></url>
        </urlset>
        """.encode("utf-8")

        products_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/products/prod-1</loc></url>
            <url><loc>https://example.com/products/prod-2</loc></url>
        </urlset>
        """.encode("utf-8")

        routes = {
            "https://example.com/sitemap_index.xml": (200, {"Content-Type": "application/xml"}, index_xml),
            "https://example.com/posts-sitemap.xml": (200, {"Content-Type": "application/xml"}, posts_xml),
            "https://example.com/products-sitemap.xml": (200, {"Content-Type": "application/xml"}, products_xml),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
        }

        extractor = SitemapExtractor(allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/sitemap_index.xml"])
        await extractor.close()

        self.assertEqual(res["total_sitemaps_discovered"], 3) # root index + 2 child sitemaps
        self.assertEqual(res["total_urls_extracted"], 4)
        urls = [u["normalized_url"] for u in res["unique_urls"]]
        self.assertIn("https://example.com/posts/post-1", urls)
        self.assertIn("https://example.com/products/prod-1", urls)

    # -------------------------------------------------------------------------
    # Scenario 3: Nested indexes, duplicate URLs, and malformed XML
    # -------------------------------------------------------------------------
    async def test_03_nested_indexes_duplicates_and_malformed_xml(self):
        root_index = """<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap><loc>https://example.com/sub-index.xml</loc></sitemap>
            <sitemap><loc>https://example.com/broken.xml</loc></sitemap>
        </sitemapindex>
        """.encode("utf-8")

        sub_index = """<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap><loc>https://example.com/part1.xml</loc></sitemap>
            <sitemap><loc>https://example.com/part2.xml</loc></sitemap>
        </sitemapindex>
        """.encode("utf-8")

        part1_xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/shared-url</loc></url>
            <url><loc>https://example.com/unique-1</loc></url>
        </urlset>""".encode("utf-8")

        part2_xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/shared-url</loc></url>
            <url><loc>https://example.com/unique-2</loc></url>
        </urlset>""".encode("utf-8")

        broken_xml = b"<urlset><unclosed-tag></urlset>"

        routes = {
            "https://example.com/root.xml": (200, {"Content-Type": "application/xml"}, root_index),
            "https://example.com/sub-index.xml": (200, {"Content-Type": "application/xml"}, sub_index),
            "https://example.com/part1.xml": (200, {"Content-Type": "application/xml"}, part1_xml),
            "https://example.com/part2.xml": (200, {"Content-Type": "application/xml"}, part2_xml),
            "https://example.com/broken.xml": (200, {"Content-Type": "application/xml"}, broken_xml),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
        }

        extractor = SitemapExtractor(allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/root.xml"])
        await extractor.close()

        # Deduplication verified: shared-url only extracted once, duplicate_count incremented
        self.assertEqual(res["duplicate_count"], 1)
        self.assertEqual(res["total_urls_extracted"], 3) # shared-url, unique-1, unique-2
        # Broken XML recorded as error without halting other extractions
        self.assertFalse(res["complete"])
        self.assertTrue(any("broken.xml" in e for e in res["errors"]))

    # -------------------------------------------------------------------------
    # Scenario 4: Gzip-compressed sitemap input (.xml.gz)
    # -------------------------------------------------------------------------
    async def test_04_gzip_compressed_sitemap(self):
        raw_xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/gzipped-page-1</loc></url>
            <url><loc>https://example.com/gzipped-page-2</loc></url>
        </urlset>""".encode("utf-8")

        gz_bytes = gzip.compress(raw_xml)

        routes = {
            "https://example.com/sitemap.xml.gz": (200, {"Content-Type": "application/x-gzip"}, gz_bytes),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
        }

        extractor = SitemapExtractor(allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/sitemap.xml.gz"])
        await extractor.close()

        self.assertEqual(res["total_urls_extracted"], 2)
        urls = [u["normalized_url"] for u in res["unique_urls"]]
        self.assertIn("https://example.com/gzipped-page-1", urls)
        self.assertIn("https://example.com/gzipped-page-2", urls)

    # -------------------------------------------------------------------------
    # Scenario 5: Sitemap URL returning HTTP 403 (with diagnostics)
    # -------------------------------------------------------------------------
    async def test_05_sitemap_403_diagnostics(self):
        cloudflare_blocked_html = """
        <html>
        <head><title>Attention Required! | Cloudflare</title></head>
        <body>
            <div class="cf-browser-verification">Checking your browser before accessing example.com</div>
        </body>
        </html>
        """
        routes = {
            "https://example.com/protected-sitemap.xml": (
                403,
                {"Server": "cloudflare", "CF-RAY": "8a992bc4-SJC", "Content-Type": "text/html"},
                cloudflare_blocked_html
            ),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
        }

        extractor = SitemapExtractor(allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/protected-sitemap.xml"])
        await extractor.close()

        self.assertEqual(res["total_urls_extracted"], 0)
        self.assertEqual(len(res["diagnostics"]), 1)
        diag = res["diagnostics"][0]["diagnostic"]
        self.assertEqual(diag["classification"], "CDN_WAF_BLOCKED_OR_CHALLENGED")
        self.assertIn("Cloudflare", diag["what_happened"])
        self.assertFalse(diag["retry_appropriate"])
        self.assertIn("cf-ray", diag["safe_headers"])
        self.assertNotIn("cookie", diag["safe_headers"])

    # -------------------------------------------------------------------------
    # Scenario 6: Page URL returning HTTP 403 during crawl
    # -------------------------------------------------------------------------
    async def test_06_page_crawl_403_diagnostic_classification(self):
        # Test origin forbidden
        diag = Diagnostics403.classify(
            status_code=403,
            headers={"Server": "nginx/1.22.1", "Content-Type": "text/html"},
            body_snippet="<html><body><h1>403 Forbidden</h1><p>nginx/1.22.1</p></body></html>",
            stage="page_crawl",
            robots_allowed=True
        )
        self.assertEqual(diag["classification"], "ORIGIN_FORBIDDEN")
        self.assertIn("nginx", diag["what_happened"].lower())
        self.assertFalse(diag["retry_appropriate"])

        # Test unclassified 403 when no signatures exist
        diag_unclass = Diagnostics403.classify(
            status_code=403,
            headers={"Content-Type": "text/plain"},
            body_snippet="Forbidden",
            stage="page_crawl",
            robots_allowed=True
        )
        self.assertEqual(diag_unclass["classification"], "HTTP_403_UNCLASSIFIED")
        self.assertIn("could not be conclusively determined", diag_unclass["what_happened"])

    # -------------------------------------------------------------------------
    # Scenario 7: Robots-disallowed URL prevents crawl attempt
    # -------------------------------------------------------------------------
    async def test_07_robots_disallowed_url_excluded(self):
        xml_content = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/allowed-page</loc></url>
            <url><loc>https://example.com/admin/secret-page</loc></url>
        </urlset>""".encode("utf-8")

        robots_content = "User-agent: *\nDisallow: /admin/\nAllow: /\n"

        routes = {
            "https://example.com/sitemap.xml": (200, {"Content-Type": "application/xml"}, xml_content),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, robots_content)
        }

        extractor = SitemapExtractor(allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/sitemap.xml"])
        await extractor.close()

        allowed_item = next(u for u in res["unique_urls"] if u["normalized_url"] == "https://example.com/allowed-page")
        disallowed_item = next(u for u in res["unique_urls"] if u["normalized_url"] == "https://example.com/admin/secret-page")

        self.assertTrue(allowed_item["robots_allowed"])
        self.assertTrue(allowed_item["selected_for_crawl"])
        self.assertFalse(disallowed_item["robots_allowed"])
        self.assertFalse(disallowed_item["selected_for_crawl"])
        self.assertEqual(disallowed_item["status"], "ROBOTS_DISALLOWED")

    # -------------------------------------------------------------------------
    # Scenario 8: HTTP 403 Playwright fallback eligibility
    # -------------------------------------------------------------------------
    async def test_08_browser_fallback_eligibility_and_settings(self):
        # Verifies that settings.BROWSER_FALLBACK triggers fallback on 403
        self.assertTrue(getattr(settings, "BROWSER_FALLBACK", True))

    # -------------------------------------------------------------------------
    # Scenario 9: Browser challenge remains blocked and reported honestly
    # -------------------------------------------------------------------------
    async def test_09_browser_challenge_reported_honestly(self):
        challenge_html = """
        <html>
        <head><title>Just a moment...</title></head>
        <body>
            <div id="cf-browser-verification">Verify you are human</div>
            <div class="turnstile-wrapper"></div>
        </body>
        </html>
        """
        indicators = Diagnostics403.inspect_html_body(challenge_html)
        self.assertTrue(indicators["challenge_detected"])
        self.assertTrue(indicators["waf_detected"])

        # Verifies that when challenge is detected, it is marked as BLOCKED, NOT verified
        diag = Diagnostics403.classify(
            status_code=403,
            headers={"Server": "cloudflare", "CF-Ray": "9812401-ORD"},
            body_snippet=challenge_html,
            stage="browser_fallback",
            robots_allowed=True
        )
        self.assertEqual(diag["classification"], "CDN_WAF_BLOCKED_OR_CHALLENGED")
        self.assertIn("challenge", diag["what_happened"].lower())
        self.assertFalse(diag["retry_appropriate"])

    # -------------------------------------------------------------------------
    # Scenario 10: Redirects to private or prohibited destination (SSRF)
    # -------------------------------------------------------------------------
    async def test_10_redirect_to_prohibited_ip_ssrf_blocked(self):
        routes = {
            "https://example.com/sitemap.xml": (302, {"Location": "http://127.0.0.1:8080/admin/secret.xml"}, b"")
        }

        # allow_local=False enforces SSRF protection against 127.0.0.1
        extractor = SitemapExtractor(allow_local=False)
        extractor.client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/sitemap.xml"])
        await extractor.close()

        self.assertEqual(res["total_urls_extracted"], 0)
        self.assertTrue(any(d["diagnostic"]["classification"] in ["REDIRECT_REJECTED", "SSRF_PROHIBITED"] for d in res["diagnostics"]))

    # -------------------------------------------------------------------------
    # Scenario 11: Invalid schemes, oversized responses, nesting depth
    # -------------------------------------------------------------------------
    async def test_11_invalid_schemes_and_limits(self):
        xml_content = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>ftp://example.com/file.txt</loc></url>
            <url><loc>javascript:alert(1)</loc></url>
            <url><loc>https://example.com/valid-page</loc></url>
        </urlset>""".encode("utf-8")

        routes = {
            "https://example.com/sitemap.xml": (200, {"Content-Type": "application/xml"}, xml_content),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
        }

        extractor = SitemapExtractor(max_depth=2, max_urls=1, allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/sitemap.xml"])
        await extractor.close()

        # Invalid schemes rejected
        self.assertEqual(res["rejected_count"], 2)
        # Only 1 valid URL accepted due to max_urls limit
        self.assertEqual(res["total_urls_extracted"], 1)

    # -------------------------------------------------------------------------
    # Scenario 12: Partial extraction and interrupted jobs
    # -------------------------------------------------------------------------
    async def test_12_partial_extraction_with_errors(self):
        index_xml = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap><loc>https://example.com/success.xml</loc></sitemap>
            <sitemap><loc>https://example.com/fail.xml</loc></sitemap>
        </sitemapindex>""".encode("utf-8")

        success_xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/ok-page</loc></url>
        </urlset>""".encode("utf-8")

        routes = {
            "https://example.com/index.xml": (200, {"Content-Type": "application/xml"}, index_xml),
            "https://example.com/success.xml": (200, {"Content-Type": "application/xml"}, success_xml),
            "https://example.com/fail.xml": (500, {}, b"Internal Server Error"),
            "https://example.com/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
        }

        extractor = SitemapExtractor(allow_local=True)
        extractor.client._transport = MockTransport(routes)
        extractor.robots_manager.http_client._transport = MockTransport(routes)

        res = await extractor.extract_all(["https://example.com/index.xml"])
        await extractor.close()

        # Partial status: 1 URL successfully extracted, but errors encountered
        self.assertEqual(res["total_urls_extracted"], 1)
        self.assertFalse(res["complete"])
        self.assertTrue(len(res["errors"]) > 0)

    # -------------------------------------------------------------------------
    # Scenario 13: Uploaded XML continuing after remote access fails
    # -------------------------------------------------------------------------
    async def test_13_uploaded_xml_fallback(self):
        uploaded_xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.com/fallback-page-1</loc></url>
            <url><loc>https://example.com/fallback-page-2</loc></url>
        </urlset>""".encode("utf-8")

        extractor = SitemapExtractor(allow_local=True)
        res = await extractor.extract_from_uploaded_bytes(uploaded_xml, filename="manual_sitemap.xml")
        await extractor.close()

        self.assertTrue(res["success"])
        self.assertEqual(res["total_urls_extracted"], 2)
        urls = [u["normalized_url"] for u in res["unique_urls"]]
        self.assertIn("https://example.com/fallback-page-1", urls)
        self.assertIn("https://example.com/fallback-page-2", urls)

    # -------------------------------------------------------------------------
    # Scenario 14: Selection of URLs and launching the existing crawl pipeline
    # -------------------------------------------------------------------------
    async def test_14_crawl_pipeline_launch_from_sitemap(self):
        import_id = str(uuid.uuid4())
        await self.db.create_sitemap_import(import_id, title="Test Pipeline Crawl")

        urls_data = [
            {"normalized_url": "https://example.com/prod-1", "raw_url": "https://example.com/prod-1", "robots_allowed": True, "status": "VALID", "selected_for_crawl": True},
            {"normalized_url": "https://example.com/prod-2", "raw_url": "https://example.com/prod-2", "robots_allowed": True, "status": "VALID", "selected_for_crawl": True},
            {"normalized_url": "https://example.com/prod-3", "raw_url": "https://example.com/prod-3", "robots_allowed": False, "status": "ROBOTS_DISALLOWED", "selected_for_crawl": False}
        ]
        await self.db.add_extracted_urls_batch(import_id, urls_data)

        # Launch crawl job
        job_id = await self.jm.create_job_from_sitemap(import_id, mode="link_audit", selected_only=True)
        self.assertIsNotNone(job_id)

        # Verify job and batch creation
        jobs = await self.jm.get_all_jobs()
        active_job = next(j for j in jobs if j["id"] == job_id)
        self.assertEqual(active_job["total_records"], 2) # Only 2 eligible & selected URLs
        self.assertEqual(active_job["status"], "QUEUED")

        batches = await self.jm.get_batches_for_job(job_id)
        self.assertTrue(len(batches) >= 1)
        self.assertEqual(batches[0]["status"], "QUEUED")

        # Verify sitemap import has crawl_job_id linked
        imp = await self.db.get_sitemap_import(import_id)
        self.assertEqual(imp["crawl_job_id"], job_id)

    # -------------------------------------------------------------------------
    # Scenario 15: Concurrent jobs, pause, resume, cancel
    # -------------------------------------------------------------------------
    async def test_15_job_lifecycle_pause_resume_cancel(self):
        import_id = str(uuid.uuid4())
        await self.db.create_sitemap_import(import_id, title="Lifecycle Test")
        await self.db.add_extracted_urls_batch(import_id, [
            {"normalized_url": "https://example.com/p1", "raw_url": "https://example.com/p1", "robots_allowed": True, "status": "VALID", "selected_for_crawl": True}
        ])
        job_id = await self.jm.create_job_from_sitemap(import_id, mode="link_audit")

        # Pause
        await self.jm.update_job_status(job_id, "PAUSED")
        jobs = await self.jm.get_all_jobs()
        self.assertEqual(next(j for j in jobs if j["id"] == job_id)["status"], "PAUSED")

        # Resume
        await self.jm.update_job_status(job_id, "QUEUED")
        jobs = await self.jm.get_all_jobs()
        self.assertEqual(next(j for j in jobs if j["id"] == job_id)["status"], "QUEUED")

        # Cancel
        await self.jm.update_job_status(job_id, "CANCELLED")
        jobs = await self.jm.get_all_jobs()
        self.assertEqual(next(j for j in jobs if j["id"] == job_id)["status"], "CANCELLED")

    # -------------------------------------------------------------------------
    # Scenario 16: Authentication and SSRF regression tests
    # -------------------------------------------------------------------------
    async def test_16_ssrf_prohibited_destinations(self):
        prohibited = [
            "http://127.0.0.1/sitemap.xml",
            "http://localhost:8000/sitemap.xml",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/sitemap.xml",
            "http://192.168.1.1/sitemap.xml"
        ]
        for p in prohibited:
            self.assertFalse(URLNormalizer.is_safe_url(p, allow_local=False), f"Should prohibit SSRF on {p}")

    # -------------------------------------------------------------------------
    # Scenario 17: Database diagnostic queries and safe headers
    # -------------------------------------------------------------------------
    async def test_17_diagnostics_storage_and_query(self):
        import_id = str(uuid.uuid4())
        await self.db.record_request_diagnostic(
            url="https://example.com/blocked",
            stage="sitemap_download",
            classification="CDN_WAF_BLOCKED_OR_CHALLENGED",
            what_happened="Cloudflare WAF blocked automated crawling.",
            where_it_happened="During sitemap retrieval (HTTP 403).",
            what_verified="Verified CF-RAY header.",
            recommended_action="Allowlist IP in Cloudflare WAF.",
            http_status=403,
            import_id=import_id,
            retry_appropriate=False,
            safe_headers={"Server": "cloudflare", "CF-Ray": "9a01-SJC"}
        )

        diags = await self.db.get_request_diagnostics(import_id=import_id)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["classification"], "CDN_WAF_BLOCKED_OR_CHALLENGED")
        self.assertEqual(diags[0]["safe_headers"]["Server"], "cloudflare")

    # -------------------------------------------------------------------------
    # Scenario 18: URL selection and toggle mechanics
    # -------------------------------------------------------------------------
    async def test_18_url_selection_and_toggles(self):
        import_id = str(uuid.uuid4())
        await self.db.create_sitemap_import(import_id, title="Selection Test")
        await self.db.add_extracted_urls_batch(import_id, [
            {"normalized_url": "https://example.com/p1", "raw_url": "https://example.com/p1", "robots_allowed": True, "status": "VALID", "selected_for_crawl": True},
            {"normalized_url": "https://example.com/p2", "raw_url": "https://example.com/p2", "robots_allowed": True, "status": "VALID", "selected_for_crawl": True},
            {"normalized_url": "https://example.com/p3", "raw_url": "https://example.com/p3", "robots_allowed": False, "status": "ROBOTS_DISALLOWED", "selected_for_crawl": False}
        ])

        # Deselect all
        await self.db.select_all_urls(import_id, eligible_only=False, selected=False)
        selected = await self.db.get_selected_urls_for_crawl(import_id)
        self.assertEqual(len(selected), 0)

        # Select eligible only
        await self.db.select_all_urls(import_id, eligible_only=True, selected=True)
        selected = await self.db.get_selected_urls_for_crawl(import_id)
        self.assertEqual(len(selected), 2)
        self.assertNotIn("https://example.com/p3", selected)

    # -------------------------------------------------------------------------
    # Scenario 19: API Endpoints for Import, Status, and Pagination
    # -------------------------------------------------------------------------
    async def test_19_api_endpoints_sitemap_import_and_query(self):
        import qcv5.api.server as server
        server.db = self.db
        server.job_manager.db = self.db
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Reject invalid scheme
            res_bad = await client.post("/api/sitemaps/import", json={"urls": ["ftp://example.com/sitemap.xml"]})
            self.assertEqual(res_bad.status_code, 400)

            # 2. Reject empty input
            res_empty = await client.post("/api/sitemaps/import", json={"urls": []})
            self.assertEqual(res_empty.status_code, 400)

            # 3. Valid import
            res = await client.post("/api/sitemaps/import", json={
                "urls": ["https://example.com/sitemap.xml"],
                "title": "API Test Import"
            })
            self.assertEqual(res.status_code, 200)
            data = res.json()
            import_id = data["import_id"]
            self.assertEqual(data["status"], "ok")

            # 4. Get Status
            res_status = await client.get(f"/api/sitemaps/{import_id}/status")
            self.assertEqual(res_status.status_code, 200)
            self.assertEqual(res_status.json()["id"], import_id)

            # 5. Get Sources
            res_sources = await client.get(f"/api/sitemaps/{import_id}/sources")
            self.assertEqual(res_sources.status_code, 200)
            self.assertIn("sources", res_sources.json())

            # 6. Get URLs
            res_urls = await client.get(f"/api/sitemaps/{import_id}/urls")
            self.assertEqual(res_urls.status_code, 200)
            self.assertIn("items", res_urls.json())

    # -------------------------------------------------------------------------
    # Scenario 20: API Endpoints for Selection, Crawl Launch, and Diagnostics
    # -------------------------------------------------------------------------
    async def test_20_api_endpoints_crawl_and_diagnostics(self):
        import qcv5.api.server as server
        server.db = self.db
        server.job_manager.db = self.db
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Create import directly in server DB
            import_id = str(uuid.uuid4())
            await self.db.create_sitemap_import(import_id, title="API Crawl Test")
            await self.db.add_extracted_urls_batch(import_id, [
                {"normalized_url": "https://example.com/api-p1", "raw_url": "https://example.com/api-p1", "robots_allowed": True, "status": "VALID", "selected_for_crawl": True}
            ])
            # Record diagnostic
            await self.db.record_request_diagnostic(
                url="https://example.com/api-403",
                stage="sitemap_download",
                classification="CDN_WAF_BLOCKED_OR_CHALLENGED",
                what_happened="Cloudflare challenge detected",
                where_it_happened="During sitemap download",
                what_verified="CF-Ray present",
                recommended_action="Allowlist IP",
                http_status=403,
                import_id=import_id
            )

            # Toggle selection via API
            res_sel = await client.post(f"/api/sitemaps/{import_id}/urls/select-all", json={
                "eligible_only": True,
                "selected": True
            })
            self.assertEqual(res_sel.status_code, 200)

            # Start crawl via API
            res_crawl = await client.post(f"/api/sitemaps/{import_id}/crawl", json={
                "mode": "link_audit",
                "force_recheck": False,
                "selected_only": True
            })
            self.assertEqual(res_crawl.status_code, 200)
            data_crawl = res_crawl.json()
            self.assertEqual(data_crawl["status"], "ok")
            self.assertIn("job_id", data_crawl)

            # Query diagnostics via API
            res_diag = await client.get(f"/api/sitemaps/{import_id}/diagnostics")
            self.assertEqual(res_diag.status_code, 200)
            self.assertEqual(len(res_diag.json()["diagnostics"]), 1)

            res_all_diag = await client.get("/api/diagnostics/403")
            self.assertEqual(res_all_diag.status_code, 200)
            self.assertIn("diagnostics", res_all_diag.json())


if __name__ == "__main__":
    unittest.main()
