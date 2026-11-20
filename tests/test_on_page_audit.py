import asyncio
import csv
import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import tempfile
import threading
import time
from collections import Counter

from qcv5.crawler.on_page_audit_engine import OnPageAuditEngine, extract_on_page_urls
from qcv5.reports.on_page_report_packager import build_on_page_audit_package


class ControlledAuditTestServer:
    """Real local HTTP server tracking exact request counts per path for Section 33 tests."""

    def __init__(self):
        self.hit_counts = Counter()
        self.lock = threading.Lock()
        self.server = None
        self.thread = None
        self.port = None

    def start(self):
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                with parent.lock:
                    parent.hit_counts[path] += 1

                if path == "/robots.txt":
                    body = b"User-agent: *\nDisallow: /robots-secret\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path.startswith("/source-"):
                    idx = path.split("-")[-1]
                    if idx == "404":
                        body = b"<html><body>Not Found</body></html>"
                        self.send_response(404)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    # Rich HTML page with all URL types, repeated destination, fragment, non-HTTP, canonical, hreflang, SSRF, robots, WAF, 404, 301, 302, timeout
                    extra_links = ""
                    if idx == "1":
                        extra_links = f"""
                        <a href="/shared-product#reviews">Shared Reviews</a>
                        <a href="http://127.0.0.1:{parent.port}/shared-product">Shared Duplicate 3</a>
                        <a href="/redir-ok">Redir OK</a>
                        <a href="/redir-broken">Redir Broken</a>
                        <a href="/broken-404">Broken Link</a>
                        <a href="/origin-403">Origin 403</a>
                        <a href="/akamai-waf-403">WAF 403</a>
                        <a href="/robots-secret">Robots Disallowed</a>
                        <a href="/slow-timeout">Slow Timeout</a>
                        <a href="http://169.254.169.254/latest/meta-data/">SSRF Link</a>
                        <a href="mailto:test@example.com">Email</a>
                        <a href="tel:+911234567890">Phone</a>
                        <a href="javascript:void(0)">JS</a>
                        <a href="data:text/plain;base64,SGVsbG8=">Data</a>
                        <a href="#section-top">Fragment Only</a>
                        <area href="/area-target" alt="Area">
                        <iframe src="/iframe-target" title="Frame"></iframe>
                        """
                    html = f"""<!doctype html>
                    <html>
                    <head>
                        <title>Source Page {idx}</title>
                        <meta name="tag-1" content="PDP" />
                        <meta name="tag-2" content="CD" />
                        <link rel="canonical" href="http://127.0.0.1:{parent.port}/source-{idx}" />
                        <link rel="alternate" hreflang="hi-IN" href="/hi/source-{idx}" />
                    </head>
                    <body>
                        <main>
                            <a href="/shared-product" rel="noopener">Shared Product</a>
                            <a href="relative-page-{idx}">Relative Page</a>
                            {extra_links}
                        </main>
                    </body>
                    </html>
                    """.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return

                if path in ("/shared-product", "/area-target", "/iframe-target") or path.startswith("/relative-page-") or path.startswith("/hi/source-"):
                    body = f"<html><body>OK {path}</body></html>".encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/redir-ok":
                    self.send_response(301)
                    self.send_header("Location", "/shared-product")
                    self.end_headers()
                    return

                if path == "/redir-broken":
                    self.send_response(302)
                    self.send_header("Location", "/broken-404")
                    self.end_headers()
                    return

                if path == "/broken-404":
                    body = b"Not Found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/origin-403":
                    body = b"<html><body>Access Denied by Origin App</body></html>"
                    self.send_response(403)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/akamai-waf-403":
                    body = (
                        b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>"
                        b"<H1>Access Denied</H1>You don't have permission to access this server."
                        b"<P>Reference #18.6092e17.1794940412.deadbeef"
                        b"<P>https://errors.edgesuite.net/18.6092e17</BODY></HTML>"
                    )
                    self.send_response(403)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Server", "AkamaiGHost")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/slow-timeout":
                    time.sleep(2.5)
                    body = b"Slow"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except Exception:
                        pass
                    return

                self.send_response(404)
                self.end_headers()

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = ThreadedTCPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def test_complete_on_page_audit_extraction_reuse_resume_and_zip():
    srv = ControlledAuditTestServer()
    srv.start()
    tmpdir = tempfile.mkdtemp(prefix="qc_v5_onpage_test_")
    db_path = os.path.join(tmpdir, "test_onpage.db")

    try:
        base = f"http://127.0.0.1:{srv.port}"
        sources = [
            {"original_url": f"{base}/source-1", "normalized_url": f"{base}/source-1", "source_sitemap": "sm1", "source_sitemap_number": 1},
            {"original_url": f"{base}/source-2", "normalized_url": f"{base}/source-2", "source_sitemap": "sm1", "source_sitemap_number": 1},
            {"original_url": f"{base}/source-3", "normalized_url": f"{base}/source-3", "source_sitemap": "sm1", "source_sitemap_number": 1},
            {"original_url": f"{base}/source-4", "normalized_url": f"{base}/source-4", "source_sitemap": "sm1", "source_sitemap_number": 1},
            {"original_url": f"{base}/source-5", "normalized_url": f"{base}/source-5", "source_sitemap": "sm1", "source_sitemap_number": 1},
            {"original_url": f"{base}/source-404", "normalized_url": f"{base}/source-404", "source_sitemap": "sm1", "source_sitemap_number": 1},
        ]

        job_id = "test-onpage-job-001"

        # 1. PHASE 1: Run ONLY 2 source pages then stop (simulate interruption / crash)
        eng1 = OnPageAuditEngine(
            db_path=db_path,
            job_id=job_id,
            job_name="Section 33 Controlled Test",
            allow_local=True,
            primary_domain="",
            source_concurrency=2,
            dest_concurrency=4,
            max_retries=0,
            connect_timeout=1.0,
            read_timeout=1.0,
            batch_size=1,
        )
        eng1.initialize_inventory(sources)
        mid_stats = asyncio.run(eng1.run(max_source_pages_this_run=2))
        eng1.close()

        assert mid_stats["source_pages_total"] == 6
        assert mid_stats["source_pages_processed"] == 2
        assert mid_stats["source_pages_pending"] == 4

        # 2. PHASE 2: Resume on exact same job_id -> completes remaining 4 source pages + Stage B validation
        eng2 = OnPageAuditEngine(
            db_path=db_path,
            job_id=job_id,
            job_name="Section 33 Controlled Test",
            allow_local=True,
            primary_domain="",
            source_concurrency=2,
            dest_concurrency=4,
            max_retries=0,
            connect_timeout=1.0,
            read_timeout=1.0,
            batch_size=2,
        )
        eng2.initialize_inventory(sources)  # Idempotent on resume
        final_stats = asyncio.run(eng2.run())
        eng2.close()

        assert final_stats["status"] == "COMPLETED"
        assert final_stats["source_pages_total"] == 6
        assert final_stats["source_pages_pending"] == 0
        assert final_stats["source_pages_completed"] == 6

        # 3. Verify EXACT cache reuse proof on server hit counts!
        # /shared-product was linked from ALL 5 source pages (3x on source-1, 1x each on source-2..5) + target of /redir-ok
        # Wait: /redir-ok redirects to /shared-product (1 hop), plus /shared-product itself validated ONCE = 2 hits total (1 direct + 1 from /redir-ok redirect hop),
        # NEVER 5 or 7 direct checks! And /robots-secret must have 0 server hits!
        assert srv.hit_counts["/robots-secret"] == 0, f"Robots disallowed URL was fetched! hits={srv.hit_counts['/robots-secret']}"
        assert srv.hit_counts["/shared-product"] <= 2, f"Expected 1 direct check (+1 redirect target hop), got {srv.hit_counts['/shared-product']}"
        assert srv.hit_counts["/source-1"] == 1, f"Source-1 fetched more than once (canonical self-link not reused!): {srv.hit_counts['/source-1']}"

        # 4. Inspect DB relationships & validation cache directly
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Check all 5 source pages preserved their relationship to /shared-product
        shared_url = f"{base}/shared-product"
        shared_rels = conn.execute(
            "SELECT * FROM on_page_link_relationships WHERE job_id = ? AND normalized_found_url = ? ORDER BY source_page_url",
            (job_id, shared_url),
        ).fetchall()
        assert len(shared_rels) == 5, f"Expected 5 source->shared-product relationships, got {len(shared_rels)}"
        val_srcs = Counter(r["validation_source"] for r in shared_rels)
        assert val_srcs["NEW_CHECK"] == 1
        assert val_srcs["REUSED"] == 4

        # Check per-page deduplication on source-1: 3 occurrences collapsed into 1 row with occurrence_count == 3
        s1_shared = [r for r in shared_rels if r["source_page_url"] == f"{base}/source-1"][0]
        assert s1_shared["occurrence_count"] == 3

        # Check canonical & hreflang extracted
        types_s1 = {
            r["found_url_type"]
            for r in conn.execute(
                "SELECT found_url_type FROM on_page_link_relationships WHERE job_id = ? AND source_page_url = ?",
                (job_id, f"{base}/source-1"),
            ).fetchall()
        }
        assert {"CANONICAL", "HREFLANG", "ANCHOR", "AREA", "IFRAME"}.issubset(types_s1)

        # Check non-HTTP & fragment schemes properly skipped (NOT sent to HTTP validation cache!)
        non_http_rows = conn.execute(
            "SELECT url_syntax_type, validation_source FROM on_page_link_relationships WHERE job_id = ? AND found_url_scope IN ('NON_HTTP', 'FRAGMENT')",
            (job_id,),
        ).fetchall()
        non_http_syntaxes = {r["url_syntax_type"] for r in non_http_rows}
        assert {"MAILTO", "TEL", "JAVASCRIPT", "DATA", "FRAGMENT"}.issubset(non_http_syntaxes)

        # Check distinct classifications: HTTP_403 vs CDN_WAF_BLOCKED_OR_CHALLENGED vs ROBOTS_BLOCKED vs Blocked (SSRF) vs Timeout vs 301->200 vs 302->404
        cache_by_url = {
            r["normalized_url"]: dict(r)
            for r in conn.execute("SELECT * FROM on_page_validation_cache WHERE job_id = ?", (job_id,)).fetchall()
        }
        assert cache_by_url[f"{base}/shared-product"]["status_bucket"] == "2xx"
        assert cache_by_url[f"{base}/redir-ok"]["status_bucket"] == "3xx"
        assert cache_by_url[f"{base}/redir-ok"]["http_status"] == 200
        assert cache_by_url[f"{base}/redir-broken"]["status_bucket"] == "3xx"
        assert cache_by_url[f"{base}/redir-broken"]["http_status"] == 404
        assert cache_by_url[f"{base}/broken-404"]["status_bucket"] == "4xx"
        assert cache_by_url[f"{base}/broken-404"]["http_status"] == 404
        assert cache_by_url[f"{base}/origin-403"]["status_bucket"] == "4xx"
        assert cache_by_url[f"{base}/origin-403"]["status_classification"] == "HTTP_403"
        assert cache_by_url[f"{base}/akamai-waf-403"]["status_bucket"] == "WAF blocked"
        assert cache_by_url[f"{base}/akamai-waf-403"]["status_classification"] == "CDN_WAF_BLOCKED_OR_CHALLENGED"
        assert cache_by_url[f"{base}/robots-secret"]["status_bucket"] == "Robots blocked"
        assert cache_by_url[f"{base}/robots-secret"]["status_classification"] == "ROBOTS_BLOCKED"
        assert cache_by_url[f"{base}/slow-timeout"]["status_bucket"] == "Timeout"
        assert cache_by_url["http://169.254.169.254/latest/meta-data/"]["status_classification"] == "Blocked (SSRF)"
        conn.close()

        # 5. Build complete CSV + 13-sheet Excel + ZIP + Section 35 Trust Gate
        pkg_res = build_on_page_audit_package(
            db_path=db_path,
            job_id=job_id,
            output_dir=tmpdir,
            package_name="TEST_ON_PAGE_AUDIT",
        )
        assert pkg_res["trust_gate_passed"] is True, f"Trust gate failed: {pkg_res['trust_gate']}"
        assert pkg_res["zip_verified"] is True, "ZIP post-extraction verification failed!"
        assert pkg_res["final_status"] == "PASS"
        assert os.path.exists(pkg_res["zip_path"])

        # Verify cache evidence invariant
        ce = pkg_res["cache_evidence"]
        assert ce["invariant_http_rels_eq_new_plus_reused"] is True
        assert ce["invariant_new_eq_unique_http"] is True
        assert ce["reused_validation_count"] >= 4
    finally:
        srv.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    test_complete_on_page_audit_extraction_reuse_resume_and_zip()
    print("SECTION 33 CONTROLLED TEST SUITE: ALL ASSERTIONS PASSED!")

