"""
Local target site for URL-status audit tests/benchmarks (run as a separate process).

    python bench/url_status_sim.py --port 8811

Paths (i = any integer):
  /ok/i 200 | /r/i 301->/ok/i | /rb/i 302->/nf/i | /nf/i 404 | /fb/i 403 | /rl/i 429 | /se/i 500
  /flaky/i 503 on first request then 200 | /slow/i sleeps --slow-sec | /loop/i redirects to itself
  /to-ssrf 302 -> http://169.254.169.254/latest/meta-data
  /__hits  JSON {path: count}   (server-side truth for "validated exactly once")
"""
import argparse
import asyncio
import json
from collections import Counter

import uvicorn


class App:
    def __init__(self, slow_sec: float):
        self.hits = Counter()
        self.slow_sec = slow_sec

    async def send(self, send, status, body=b"", headers=()):
        hdrs = [(b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode())]
        hdrs += [(k.encode(), v.encode()) for k, v in headers]
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        if path == "/__hits":
            return await self.send(send, 200, json.dumps(self.hits).encode())
        self.hits[path] += 1
        parts = path.strip("/").split("/")
        kind = parts[0]
        tail = parts[1] if len(parts) > 1 else "0"
        if kind == "ok":
            return await self.send(send, 200, b"<html>ok</html>")
        if kind == "r":
            return await self.send(send, 301, headers=[("location", f"/ok/{tail}")])
        if kind == "rb":
            return await self.send(send, 302, headers=[("location", f"/nf/{tail}")])
        if kind == "nf":
            return await self.send(send, 404, b"not found")
        if kind == "fb":
            return await self.send(send, 403, b"forbidden")
        if kind == "rl":
            return await self.send(send, 429, b"slow down", headers=[("retry-after", "0")])
        if kind == "se":
            return await self.send(send, 500, b"boom")
        if kind == "flaky":
            return await self.send(send, 503 if self.hits[path] == 1 else 200, b"x")
        if kind == "slow":
            await asyncio.sleep(self.slow_sec)
            return await self.send(send, 200, b"late")
        if kind == "loop":
            return await self.send(send, 302, headers=[("location", f"/loop/{tail}")])
        if kind == "to-ssrf":
            return await self.send(send, 302, headers=[("location", "http://169.254.169.254/latest/meta-data")])
        return await self.send(send, 404, b"unknown")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8811)
    p.add_argument("--slow-sec", type=float, default=5.0)
    a = p.parse_args()
    uvicorn.run(App(a.slow_sec), host="127.0.0.1", port=a.port, log_level="error", access_log=False,
                lifespan="off", backlog=4096, limit_concurrency=None)
