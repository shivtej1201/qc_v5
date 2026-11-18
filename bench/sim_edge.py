"""
Simulated primary edge + external host for end-to-end on-page audit benchmarks.

Run as its own process (so server CPU never competes with the engine's GIL):
    python bench/sim_edge.py --sources 3000 --port 8801 --latency-ms 300 --waf-rps 0

Primary host  = 127.0.0.1:<port>      (engine treats 127.0.0.1 as the primary domain)
External host = 127.0.0.2:<port>      (separate host, NOT charged to the primary budget)

Akamai model (--waf-rps > 0): if primary requests over the trailing --waf-window seconds exceed
waf-rps * window, every primary request (incl. robots.txt) gets an AkamaiGHost 403 for --waf-block
seconds. This is a MODEL for exercising the adaptive controller, not Bajaj's real threshold.

GET /__stats returns server-side truth: per-kind request counts, duplicate validations, max
requests in any 1s window, 403s served.
"""
import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter, deque

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sim_site import EXTRA_DEST_RATIO, page_html  # noqa: E402

AKAMAI_403 = (
    b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1>"
    b"You don't have permission to access this server.<P>Reference #18.6092e17.1794940412.deadbeef"
    b"<P>https://errors.edgesuite.net/18.6092e17</BODY></HTML>"
)


class Edge:
    def __init__(self, a):
        self.a = a
        self.hits = Counter()
        self.kind = Counter()
        self.primary_times = deque()
        self.all_primary = []
        self.block_until = 0.0
        self.waf_served = 0
        self.blocks = 0
        self.rnd = random.Random(7)
        self.n_extra = max(1, int(a.sources * EXTRA_DEST_RATIO))
        self.page_cache = {}

    async def send(self, send, status, body=b"", ctype="text/html; charset=utf-8", headers=()):
        hdrs = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]
        hdrs += [(k.encode(), v.encode()) for k, v in headers]
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        host = dict(scope["headers"]).get(b"host", b"").decode()
        primary = host.startswith("127.0.0.1")
        now = time.monotonic()
        if path == "/__stats":
            ts = self.all_primary
            worst, j = 0, 0
            for i in range(len(ts)):
                while ts[i] - ts[j] >= 1.0:
                    j += 1
                worst = max(worst, i - j + 1)
            dups = {p: c for p, c in self.hits.items() if c > 1 and not p.endswith("/robots.txt")}
            return await self.send(send, 200, json.dumps({
                "kind": self.kind, "total": sum(self.kind.values()), "max_primary_req_in_1s": worst,
                "waf_403_served": self.waf_served, "waf_blocks": self.blocks,
                "duplicate_paths": len(dups), "duplicate_examples": list(dups.items())[:10],
                "primary_first_ts": ts[0] if ts else 0, "primary_last_ts": ts[-1] if ts else 0,
            }).encode(), "application/json")

        key = ("P" if primary else "X") + path
        self.hits[key] += 1
        if primary:
            self.all_primary.append(now)
            if self.a.waf_rps > 0:
                self.primary_times.append(now)
                while self.primary_times and now - self.primary_times[0] > self.a.waf_window:
                    self.primary_times.popleft()
                if now >= self.block_until and len(self.primary_times) > self.a.waf_rps * self.a.waf_window:
                    self.block_until = now + self.a.waf_block
                    self.blocks += 1
                if now < self.block_until:
                    self.waf_served += 1
                    self.kind["waf_403"] += 1
                    return await self.send(send, 403, AKAMAI_403, headers=[("server", "AkamaiGHost")])

        if path == "/robots.txt":
            self.kind["robots"] += 1
            return await self.send(send, 200, b"User-agent: *\nDisallow: /private\n", "text/plain")

        lat = self.a.latency_ms / 1000.0
        await asyncio.sleep(max(0.0, self.rnd.lognormvariate(0, 0.35) * lat))

        if primary and path.startswith("/p/"):
            self.kind["source_page"] += 1
            i = int(path.rsplit("/", 1)[-1])
            body = self.page_cache.get(i)
            if body is None:
                body = page_html(i, self.a.sources, f"http://127.0.0.1:{self.a.port}", f"http://127.0.0.2:{self.a.port}").encode()
                if len(self.page_cache) < 2000:
                    self.page_cache[i] = body
            return await self.send(send, 200, body)
        if primary and path.startswith("/d/"):
            k = int(path.rsplit("/", 1)[-1])
            self.kind["extra_dest"] += 1
            if k % 40 == 0:
                return await self.send(send, 301, b"", headers=[("location", f"/p/{k % self.a.sources}")])
            if k % 50 == 1:
                return await self.send(send, 503, b"<html>busy</html>")
            return await self.send(send, 200, b"<html>" + b"x" * 60000 + b"</html>")
        self.kind["external" if not primary else "other"] += 1
        return await self.send(send, 200, b"<html>ext</html>")


async def main(a):
    app = Edge(a)
    servers = []
    for host in ("127.0.0.1", "127.0.0.2"):
        cfg = uvicorn.Config(app, host=host, port=a.port, log_level="error", access_log=False, lifespan="off")
        servers.append(uvicorn.Server(cfg))
    await asyncio.gather(*[s.serve() for s in servers])


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--sources", type=int, default=3000)
    p.add_argument("--port", type=int, default=8801)
    p.add_argument("--latency-ms", type=float, default=300.0)
    p.add_argument("--waf-rps", type=float, default=0.0)
    p.add_argument("--waf-window", type=float, default=10.0)
    p.add_argument("--waf-block", type=float, default=20.0)
    asyncio.run(main(p.parse_args()))
