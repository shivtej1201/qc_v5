"""
Security validation for the on-page platform:
SSRF (direct links, redirect hops, encoded/numeric forms, sitemap discovery), CSV/Excel formula injection in every
report, path traversal and input validation on the API, SQL-injection-shaped filter values, issue status input.
"""
import asyncio
import csv
import io
import os
import shutil
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from url_normalizer import URLNormalizer


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://LOCALHOST./", "http://10.0.0.5/", "http://192.168.1.1/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://0x7f000001/", "http://2130706433/", "http://0177.0.0.1/", "http://0.0.0.0/", "http://%31%32%37.0.0.1/",
    "http://[fe80::1]/", "http://224.0.0.1/",
])
def test_ssrf_private_and_obfuscated_targets_blocked(url):
    assert URLNormalizer.is_safe_url(url, allow_local=False) is False


def test_metadata_and_link_local_blocked_even_in_local_test_mode():
    for u in ("http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/", "http://224.0.0.1/"):
        assert URLNormalizer.is_safe_url(u, allow_local=True) is False


def test_public_ip_allowed():
    assert URLNormalizer.is_safe_url("http://93.184.216.34/", allow_local=False) is True


class Site:
    def __init__(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                p = self.path.split("?")[0]
                if p == "/robots.txt":
                    return self._send(200, "User-agent: *\n", "text/plain")
                if p == "/to-metadata":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if p == "/evil":
                    body = ('<html><head><title>=HYPERLINK("http://x","click")</title></head><body>'
                            '<a href="http://169.254.169.254/latest/meta-data/">=cmd|\'/c calc\'!A1</a>'
                            '<a href="/to-metadata">+SUM(1,2)</a><a href="http://[::ffff:169.254.169.254]/">-2+3</a>'
                            '<a href="/ok">@import</a><img src="/img.png" alt="=1+1"></body></html>')
                    return self._send(200, body, "text/html")
                if p == "/ok":
                    return self._send(200, "<html><title>ok page</title><body>ok</body></html>", "text/html")
                if p == "/img.png":
                    return self._send(200, b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")
                return self._send(404, "nf", "text/html")

            def _send(self, code, body, ctype):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture(scope="module")
def env():
    site = Site()
    work = tempfile.mkdtemp(prefix="qc_sec_")
    import server
    from db import Database
    from onpage_service import OnPageAuditService
    saved = (server.db, server.onpage)
    server.db = Database(os.path.join(work, "api.db"))
    asyncio.run(server.db.connect())
    server.onpage = OnPageAuditService(server.db.db_path, base_dir=os.path.join(work, "jobs"),
                                       reports_dir=os.path.join(work, "reports"), allow_local=True)
    yield {"server": server, "site": site, "work": work}
    asyncio.run(server.db.close())
    server.db, server.onpage = saved
    site.close()
    shutil.rmtree(work, ignore_errors=True)


def _client(srv):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=srv.app), base_url="http://t", timeout=60)


def test_ssrf_in_crawl_and_csv_injection_guard(env):
    srv, site = env["server"], env["site"]

    async def go():
        async with _client(srv) as c:
            r = await c.post("/api/on-page/audits", json={"name": "sec", "urls": [site.base + "/evil"],
                                                          "preset": "conservative", "primary_domain": "127.0.0.1",
                                                          "overrides": {"target_rps": 40, "max_rps": 40, "read_timeout": 2,
                                                                        "max_retries": 0}})
            jid = r.json()["job_id"]
            end = time.time() + 120
            while time.time() < end:
                d = (await c.get(f"/api/on-page/audits/{jid}")).json()
                if d["package_available"]:
                    break
                await asyncio.sleep(0.3)
            assert d["final_qc_status"] == "PASS", d.get("log_tail")
            return (await c.get(f"/api/on-page/audits/{jid}/package")).content, jid
    zbytes, jid = asyncio.run(go())
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    links = {r["normalized_url"]: r for r in csv.DictReader(io.StringIO(zf.read("03_url_audit.csv").decode()))}
    meta = [r for u, r in links.items() if "169.254.169.254" in u]
    assert meta and all(r["status_class"] == "ssrf_blocked" for r in meta), [(r["normalized_url"], r["status_class"]) for r in meta]
    hop = [r for u, r in links.items() if u.endswith("/to-metadata")][0]
    assert "SSRF" in (hop["status_classification"] + hop["error_type"]) or hop["status_class"] in ("ssrf_blocked", "302")
    issues = list(csv.DictReader(io.StringIO(zf.read("12_issues.csv").decode())))
    assert any(i["issue_type"] == "UNSAFE_DESTINATION" for i in issues)
    # formula injection: every cell that starts with = + - @ (and is not a URL) is neutralized in EVERY report
    for name in zf.namelist():
        for row in csv.reader(io.StringIO(zf.read(name).decode("utf-8"))):
            for cell in row:
                assert not (cell[:1] in ("=", "+", "@") or (cell[:1] == "-" and not cell[1:2].isdigit() and cell[1:2] != "")), (name, cell)
    anchors = [r["anchor_text"] for r in links.values()]
    assert "'=cmd|'/c calc'!A1" in anchors and "'+SUM(1,2)" in anchors and "'@import" in anchors


def test_api_input_validation_and_injection_shaped_filters(env):
    srv = env["server"]

    async def go():
        async with _client(srv) as c:
            jobs = (await c.get("/api/on-page/audits")).json()["audits"]
            jid = jobs[0]["job_id"]
            # path traversal / malformed ids
            for bad in ("../etc", "onpage_..%2f..%2f", "onpage_XYZ", "onpage_123"):
                assert (await c.get(f"/api/on-page/audits/{bad}")).status_code in (400, 404)
            for bad in ("../../etc/passwd", "12_issues.csv;rm", "..%2F01_summary.csv", "99_x.txt"):
                assert (await c.get(f"/api/on-page/audits/{jid}/files/{bad}")).status_code in (400, 404)
            # whitelisted sort + bounded limit
            assert (await c.get(f"/api/on-page/audits/{jid}/links", params={"sort": "id; DROP TABLE x"})).status_code == 400
            assert (await c.get(f"/api/on-page/audits/{jid}/links", params={"order": "desc; --"})).status_code == 400
            assert (await c.get(f"/api/on-page/audits/{jid}/urls", params={"limit": 0})).status_code == 400
            assert (await c.get(f"/api/on-page/audits/{jid}/urls", params={"status_bucket": "9xx"})).status_code == 400
            # injection-shaped search values are bound parameters: no error, no extra rows, tables intact
            r = await c.get(f"/api/on-page/audits/{jid}/links", params={"q": "' OR 1=1 --"})
            assert r.status_code == 200 and r.json()["matched"] == 0
            r = await c.get(f"/api/on-page/audits/{jid}/issues", params={"q": "%' UNION SELECT 1 --", "status": "OPEN"})
            assert r.status_code == 200 and r.json()["matched"] == 0
            assert (await c.get(f"/api/on-page/audits/{jid}/urls")).json()["matched"] == 1
            # issue status input
            assert (await c.patch(f"/api/on-page/audits/{jid}/issues/{'a' * 20}/status", json={"status": "OPEN"})).status_code == 404
            assert (await c.patch(f"/api/on-page/audits/{jid}/issues/../../x/status", json={"status": "OPEN"})).status_code in (400, 404, 405)   # never served
            # start-audit validation
            assert (await c.post("/api/on-page/audits", json={"name": "x", "urls": [], "preset": "conservative"})).status_code == 400
            assert (await c.post("/api/on-page/audits", json={"name": "x", "urls": ["http://a.com"], "preset": "nope"})).status_code == 400
            # sitemap discovery input validation
            assert (await c.post("/api/on-page/sitemaps/discover", json={"inputs": ["ftp://x/y.xml"]})).status_code == 400
            assert (await c.post("/api/on-page/sitemaps/discover", json={"inputs": []})).status_code == 400
    asyncio.run(go())


def test_sitemap_discovery_blocks_private_targets_when_not_in_test_mode(tmp_path):
    from sitemap_config import SitemapConfigStore
    store = SitemapConfigStore(str(tmp_path), allow_local=False)
    res = asyncio.run(store.discover(["http://169.254.169.254/sitemap.xml", "http://127.0.0.1:9/sitemap.xml"]))
    assert res["summary"]["unique_urls"] == 0
    assert all(s["status"] == "FAILED" for s in res["sources"])
