"""Sitemaps are read with QC's honest identity, and a CDN 'no' is reported, never worked around."""
import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

SEEN = []


class H(BaseHTTPRequestHandler):
    token_ok = "tok-123"

    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append((self.path, self.headers.get("User-Agent", ""), self.headers.get("X-QC-Audit")))
        if self.headers.get("X-QC-Audit") != self.token_ok:
            self.send_response(403); self.end_headers(); self.wfile.write(b"Access Denied"); return
        if self.path == "/robots.txt":
            body = f"User-agent: *\nAllow: /\nSitemap: http://{self.headers['Host']}/sitemap.xml\n".encode()
            ctype = "text/plain"
        else:
            body = (f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f'<url><loc>http://{self.headers["Host"]}/a</loc></url><url><loc>http://{self.headers["Host"]}/b</loc></url></urlset>').encode()
            ctype = "application/xml"
        self.send_response(200); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _site(base, token):
    return {"id": 1, "base_url": base, "primary_host": "127.0.0.1", "access_token": token, "settings": {}}


def test_sitemap_carries_token_and_honest_ua(server):
    from qc6.workers.runner import discover_urls
    SEEN.clear()
    urls = asyncio.run(discover_urls(_site(server, "tok-123"), {}))
    assert sorted(u.rsplit("/", 1)[-1] for u in urls) == ["a", "b"]
    assert SEEN and all(tok == "tok-123" and "QC-Audit/6.0" in ua for _, ua, tok in SEEN)


def test_cdn_refusal_is_reported_not_bypassed(server):
    from qc6.workers.runner import discover_urls
    SEEN.clear()
    with pytest.raises(RuntimeError, match="refused to show QC its sitemap"):
        asyncio.run(discover_urls(_site(server, "wrong"), {"sitemap": f"{server}/sitemap.xml"}))
    assert all("QC-Audit/6.0" in ua for _, ua, _ in SEEN)          # no disguised browser retry
