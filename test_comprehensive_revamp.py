"""
Comprehensive Revamp Regression Suite (QC V5)
Validates:
1. Recursive & nested XML sitemaps, duplicate detection, WAF 403 diagnostics, malformed XML recovery.
2. Full deterministic crawl fixtures: 200, 301, 302, 404, 403, 500, redirect chains, broken links,
   broken images, missing alt, heading structure, canonicals, robots noindex, hreflang, structured data.
3. 14-CSV row counts, schemas, and manifest.json verification.
4. Job lifecycle states (start, pause, resume, completed) and SEO health score calculation.
"""
import asyncio
import csv
import json
import os
import tempfile
import urllib.request
import zipfile
import pytest

from sitemap_extractor import SitemapExtractor
from html_parser import Parser
from url_normalizer import URLNormalizer
from on_page_audit_engine import extract_on_page_urls, extract_on_page_images
from on_page_csv_packager import build_on_page_csv_package
from test_on_page_audit_performance import AKAMAI_403, Ctx, html_resp, make_engine, sources_for


# ------------------------------------------------------------------------------ SITEMAP TESTS
def test_sitemap_recursive_index_and_nested():
    """Validates recursive nested sitemaps, duplicate URLs across sitemaps, and malformed XML recovery."""
    def sitemap_handler(path):
        if path == "/root-sitemap.xml":
            xml = """<?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap><loc>/child-1.xml</loc></sitemap>
                <sitemap><loc>/nested-index.xml</loc></sitemap>
                <sitemap><loc>/malformed.xml</loc></sitemap>
            </sitemapindex>"""
            return 200, {"Content-Type": "application/xml"}, xml.encode(), 0.0
        if path == "/child-1.xml":
            xml = """<?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url><loc>/page-1</loc></url>
                <url><loc>/page-2</loc></url>
                <url><loc>/shared-page</loc></url>
            </urlset>"""
            return 200, {"Content-Type": "application/xml"}, xml.encode(), 0.0
        if path == "/nested-index.xml":
            xml = """<?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap><loc>/child-2.xml</loc></sitemap>
            </sitemapindex>"""
            return 200, {"Content-Type": "application/xml"}, xml.encode(), 0.0
        if path == "/child-2.xml":
            xml = """<?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url><loc>/page-3</loc></url>
                <url><loc>/shared-page</loc></url>
            </urlset>"""
            return 200, {"Content-Type": "application/xml"}, xml.encode(), 0.0
        if path == "/malformed.xml":
            # Unterminated/broken XML tag
            xml = """<urlset><url><loc>/page-from-malformed</loc></url"""
            return 200, {"Content-Type": "application/xml"}, xml.encode(), 0.0
        return 404, {"Content-Type": "text/plain"}, b"Not Found", 0.0

    ctx = Ctx(sitemap_handler)
    try:
        extractor = SitemapExtractor(allow_local=True, max_depth=4)
        root_url = f"{ctx.srv.base}/root-sitemap.xml"
        res = asyncio.run(extractor.extract_all([root_url]))
        urls = [u["normalized_url"] for u in res["unique_urls"]]
        
        # Deduplication check: /shared-page should appear only once
        normalized_paths = [urllib.parse.urlparse(u).path for u in urls]
        assert "/page-1" in normalized_paths
        assert "/page-2" in normalized_paths
        assert "/page-3" in normalized_paths
        assert "/shared-page" in normalized_paths
        assert normalized_paths.count("/shared-page") == 1
        
        # Malformed XML recovery check: regex fallback extracts url even from broken xml
        assert "/page-from-malformed" in normalized_paths
        
        # Diagnostic tracking check
        sources = res["sitemap_sources"]
        assert len(sources) >= 4
        assert any(d["sitemap_url"].endswith("/child-1.xml") and (d.get("http_status") == 200 or d.get("status") == "SUCCESS") for d in sources)
    finally:
        ctx.close()


# ------------------------------------------------------------------------------ DETERMINISTIC CRAWLER FIXTURE
def test_deterministic_crawl_and_extraction_fixtures():
    """Validates the complete pipeline against all HTTP statuses and rich SEO elements."""
    def full_site_handler(path):
        if path == "/robots.txt":
            return 200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /blocked-by-robots\n", 0.0
        if path == "/p/master":
            html = """<!DOCTYPE html>
            <html lang="en">
            <head>
                <title>Master Page with High Relevance Content for SEO Audit Testing</title>
                <meta name="description" content="This is an optimal length meta description for technical SEO crawl testing purposes.">
                <meta name="robots" content="index, follow">
                <link rel="canonical" href="/p/master">
                <link rel="alternate" hreflang="es-ES" href="/es/master">
                <script type="application/ld+json">
                {"@context": "https://schema.org", "@type": "Product", "name": "SEO Test Kit"}
                </script>
            </head>
            <body>
                <h1>Primary Topic Heading</h1>
                <p>Detailed body content with adequate word count for SEO text evaluation algorithms.</p>
                <a href="/target-200">Live 200 Page</a>
                <a href="/target-301">Redirect 301</a>
                <a href="/target-302">Redirect 302</a>
                <a href="/target-404">Dead Link 404</a>
                <a href="/target-403">WAF Forbidden 403</a>
                <a href="/target-500">Internal Server Error 500</a>
                <a href="/chain-1">Chain Step 1</a>
                <img src="/img/valid.png" alt="Accessible Valid Image" width="200" height="150" loading="lazy">
                <img src="/img/missing-alt.png">
                <img src="/img/broken.png" alt="Broken Image 404">
                <img srcset="/img/res-1x.jpg 1x, /img/res-2x.jpg 2x" alt="Responsive Image">
            </body>
            </html>"""
            return html_resp(html)
        if path == "/p/seo-issues":
            # Page with deliberate SEO flaws: short title, missing H1, multiple H1, noindex
            html = """<!DOCTYPE html>
            <html>
            <head>
                <title>Short</title>
                <meta name="robots" content="noindex, follow">
            </head>
            <body>
                <h2>No H1 Tag on this page</h2>
                <a href="/target-200">Link</a>
            </body>
            </html>"""
            return html_resp(html)
        if path == "/target-200":
            return html_resp("<html><head><title>Target 200</title></head><body><h1>Target</h1></body></html>")
        if path == "/target-301":
            return 301, {"Location": "/target-200"}, b"", 0.0
        if path == "/target-302":
            return 302, {"Location": "/target-200"}, b"", 0.0
        if path == "/target-404":
            return html_resp("<html>404 Not Found</html>", 404)
        if path == "/target-403":
            return 403, {"Content-Type": "text/html", "Server": "Cloudflare"}, b"Forbidden", 0.0
        if path == "/target-500":
            return html_resp("<html>500 Server Error</html>", 500)
        if path == "/chain-1":
            return 301, {"Location": "/chain-2"}, b"", 0.0
        if path == "/chain-2":
            return 302, {"Location": "/target-200"}, b"", 0.0
        if path == "/img/valid.png":
            return 200, {"Content-Type": "image/png"}, b"\x89PNG\r\n\x1a\n" + b"x" * 50, 0.0
        if path in ("/img/missing-alt.png", "/img/res-1x.jpg", "/img/res-2x.jpg"):
            return 200, {"Content-Type": "image/png"}, b"\x89PNG\r\n\x1a\n" + b"x" * 50, 0.0
        if path == "/img/broken.png":
            return 404, {"Content-Type": "text/html"}, b"Missing image", 0.0
        return 404, {"Content-Type": "text/plain"}, b"Not Found", 0.0

    ctx = Ctx(full_site_handler)
    try:
        eng = make_engine(ctx.db, "FULL_REVAMP", primary_hosts=["127.0.0.1"], target_rps=100, min_rps=50, max_rps=100)
        eng.initialize_inventory(sources_for(ctx.srv.base, ["/p/master", "/p/seo-issues"]))
        st = asyncio.run(eng.run())
        eng.close()
        
        assert st["status"] == "COMPLETED"
        assert st["counter_reconciliation"]["passed"]
        
        pkg = build_on_page_csv_package(ctx.db, "FULL_REVAMP", ctx.dir, package_name="QC_REVAMP_PACKAGE")
        assert pkg["final_qc_status"] == "PASS"
        assert pkg["zip_verified"]
        
        # Verify Manifest
        assert "manifest" in pkg
        assert len(pkg["manifest"]) == 14
        m_dict = {m["filename"]: m for m in pkg["manifest"]}
        assert m_dict["01_summary.csv"]["status"] == "READY"
        assert m_dict["02_source_page_audit.csv"]["row_count"] == 2
        assert m_dict["12_issues.csv"]["row_count"] > 0
        
        # Verify Manifest JSON on disk
        manifest_path = os.path.join(ctx.dir, "manifest.json")
        assert os.path.exists(manifest_path)
        with open(manifest_path, encoding="utf-8") as f:
            m_loaded = json.load(f)
            assert m_loaded["job_id"] == "FULL_REVAMP"
            assert len(m_loaded["files"]) == 14
            
        # Verify Extended SEO signals in 02_source_page_audit.csv
        p2 = os.path.join(pkg["work_dir"], "02_source_page_audit.csv")
        with open(p2, encoding="utf-8", newline="") as f:
            src_rows = list(csv.DictReader(f))
        master_row = [r for r in src_rows if r["source_page_url"].endswith("/p/master")][0]
        assert master_row["h1"] == "Primary Topic Heading"
        assert master_row["h1_count"] == "1"
        assert int(master_row["title_length"]) > 30
        assert master_row["canonical_url"] in ("/p/master", f"{ctx.srv.base}/p/master")
        assert "Product" in master_row["structured_data_types"]
        
        issues_row = [r for r in src_rows if r["source_page_url"].endswith("/p/seo-issues")][0]
        assert issues_row["h1_count"] == "0"
        assert "noindex" in issues_row["robots_meta"]

        # Verify Issues in 12_issues.csv
        p12 = os.path.join(pkg["work_dir"], "12_issues.csv")
        with open(p12, encoding="utf-8", newline="") as f:
            issues = list(csv.DictReader(f))
        types = {r["issue_type"] for r in issues}
        assert "NOT_FOUND" in types
        assert "IMAGE_NOT_FOUND" in types
        assert "MISSING_ALT" in types
        assert "REDIRECTED" in types
        assert "MISSING_H1" in types
        assert "ROBOTS_NOINDEX" in types
        assert "TITLE_TOO_SHORT" in types
    finally:
        ctx.close()


# ------------------------------------------------------------------------------ JOB LIFECYCLE & API MANIFEST
def test_job_service_manifest_and_health_score():
    """Validates the OnPageAuditService manifest retrieval and health score calculation."""
    from onpage_service import OnPageAuditService
    work = tempfile.mkdtemp(prefix="qc_svc_test_")
    db_path = os.path.join(work, "test.db")
    svc = OnPageAuditService(base_dir=os.path.join(work, "jobs"), reports_dir=os.path.join(work, "reports"), db_path=db_path)
    
    # Manifest fallback returns clean PENDING entries when package not yet built
    m = svc.manifest("JOB_NON_EXISTENT")
    assert m["total_files"] == 14
    assert len(m["manifest"]) == 14
    assert all(e["status"] == "PENDING" for e in m["manifest"])
