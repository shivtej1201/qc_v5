import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from qcv5.crawler.http_pool import PerHostPoolTransport


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_one_pool_per_host_and_keepalive_limits():
    srv = _serve()
    port = srv.server_address[1]
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=40, keepalive_expiry=30)
    t = PerHostPoolTransport(limits=limits, other_host_keepalive=3, shard_keepalive=8,
                             is_heavy_host=lambda h: h == "127.0.0.1")

    async def main():
        async with httpx.AsyncClient(transport=t, trust_env=False) as c:
            for host in ("127.0.0.1", "localhost", "127.0.0.1"):
                r = await c.get(f"http://{host}:{port}/x")
                assert r.status_code == 200 and r.text == "/x"
            assert t.pool_count() == 2                      # same host reuses its pool
            heavy = t.shards(b"http", b"127.0.0.1", port)
            other = t.shards(b"http", b"localhost", port)
            assert len(heavy) == 5                          # 40 keep-alive split into shards of <= 8
            assert all(s._pool._max_keepalive_connections == 8 for s in heavy)
            assert all(s._pool._max_connections == 40 for s in heavy)   # a shard never caps concurrency
            assert len(other) == 1 and other[0]._pool._max_keepalive_connections == 3
            rs = await asyncio.gather(*(c.get(f"http://127.0.0.1:{port}/p{i}") for i in range(30)))
            assert [r.text for r in rs] == [f"/p{i}" for i in range(30)]
            assert sum(len(s._pool.connections) > 0 for s in heavy) == 5   # round-robin spreads connections
        assert t.pool_count() == 0                          # closed with the client

    try:
        asyncio.run(main())
    finally:
        srv.shutdown()


def test_engine_heavy_host_rules(tmp_path):
    from qcv5.crawler.on_page_audit_engine import OnPageAuditEngine
    e = OnPageAuditEngine(db_path=str(tmp_path / "a.db"), job_id="T", primary_domain="example.com",
                          host_budgets={"cdn.other.net": 10})
    assert e._is_heavy_host("example.com") and e._is_heavy_host("www.example.com")
    assert e._is_heavy_host("cdn.other.net")
    assert not e._is_heavy_host("ads.thirdparty.io") and not e._is_heavy_host("")
