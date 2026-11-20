"""One httpcore connection pool per host for the crawler's shared httpx.AsyncClient.

httpcore's AsyncConnectionPool re-checks EVERY pooled connection whenever a request is assigned or a response
closes (`has_expired()` -> a socket-readable probe with getpeername + poll, plus an O(n^2) idle count). With one
pool shared by the primary site, the image CDN and every external host, that bookkeeping grows with the total
number of connections and was measured at ~75 % of the event-loop thread at 25-30 req/s (py-spy, 20K benchmark).

Per-host pools keep each scan small, and a busy host's pool is further split into round-robin SHARDS of at most
`shard_keepalive` keep-alive connections, so every scan touches a handful of connections however many the host
uses. Request concurrency is still bounded by the engine's own semaphores and rate budgets (the pool limit never set
throughput); hosts the crawler leans on (primary hosts and hosts with their own budget) keep a large keep-alive
pool, every other host keeps up to its per-host concurrency of idle connections, which expire normally.
"""
from __future__ import annotations

import itertools
import math
from typing import Callable, Dict, List, Optional, Tuple

import httpx


class PerHostPoolTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, limits: httpx.Limits, other_host_keepalive: int = 8,
                 is_heavy_host: Optional[Callable[[str], bool]] = None, shard_keepalive: int = 8,
                 **transport_kwargs) -> None:
        self._limits = limits
        self._heavy_keepalive = limits.max_keepalive_connections or limits.max_connections or 20
        self._other_keepalive = max(1, min(other_host_keepalive, self._heavy_keepalive))
        self._shard_keepalive = max(1, int(shard_keepalive))
        self._is_heavy = is_heavy_host or (lambda host: False)
        self._kwargs = transport_kwargs
        self._pools: Dict[Tuple[bytes, bytes, Optional[int]], List[httpx.AsyncHTTPTransport]] = {}
        self._rr: Dict[Tuple[bytes, bytes, Optional[int]], "itertools.count[int]"] = {}
        self._closed = False

    def pool_count(self) -> int:
        """Number of hosts with a pool."""
        return len(self._pools)

    def shards(self, scheme: bytes, host: bytes, port: Optional[int]) -> List[httpx.AsyncHTTPTransport]:
        return self._pools.get((scheme, host, port), [])

    def _new_shards(self, host: str) -> List[httpx.AsyncHTTPTransport]:
        keepalive = self._heavy_keepalive if self._is_heavy(host) else self._other_keepalive
        n = max(1, math.ceil(keepalive / self._shard_keepalive))
        # every shard may open the host's full connection limit (a request never waits on a busy shard; the
        # engine's semaphores bound concurrency) but keeps at most its share of idle connections
        shard_limits = httpx.Limits(max_connections=self._limits.max_connections,
                                    max_keepalive_connections=math.ceil(keepalive / n),
                                    keepalive_expiry=self._limits.keepalive_expiry)
        return [httpx.AsyncHTTPTransport(limits=shard_limits, **self._kwargs) for _ in range(n)]

    def _pool_for(self, url: httpx.URL) -> httpx.AsyncHTTPTransport:
        key = (url.raw_scheme, url.raw_host, url.port)
        shards = self._pools.get(key)
        if shards is None:
            shards = self._pools[key] = self._new_shards((url.host or "").lower())
            self._rr[key] = itertools.count()
        return shards[0] if len(shards) == 1 else shards[next(self._rr[key]) % len(shards)]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise RuntimeError("transport is closed")
        return await self._pool_for(request.url).handle_async_request(request)

    async def aclose(self) -> None:
        self._closed = True
        pools, self._pools = [t for shards in self._pools.values() for t in shards], {}
        self._rr = {}
        for t in pools:
            await t.aclose()
