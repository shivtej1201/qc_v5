"""Per-host token bucket in Postgres, shared by the coverage crawl, real visitors and journeys (blueprint §10.2).

take() is one UPDATE ... RETURNING. A worker class with share s pays 1/s tokens per request, so each class stays
inside its share of the host's rate without separate buckets. A charge is capped at the bucket's burst: otherwise
a low-share class on a host with a small burst could never collect enough tokens and would wait forever."""
from __future__ import annotations

import asyncio
import random
from typing import Optional

TAKE_SQL = """
UPDATE host_budget SET
  tokens = LEAST(burst, tokens + rate_rps * EXTRACT(EPOCH FROM (clock_timestamp() - updated_at))) - LEAST($2, burst),
  updated_at = clock_timestamp()
WHERE host = $1 AND (paused_until IS NULL OR paused_until < clock_timestamp())
  AND LEAST(burst, tokens + rate_rps * EXTRACT(EPOCH FROM (clock_timestamp() - updated_at))) >= LEAST($2, burst)
RETURNING tokens"""

UPSERT_SQL = """
INSERT INTO host_budget (host, rate_rps, burst, tokens) VALUES ($1, $2, $3, $3)
ON CONFLICT (host) DO UPDATE SET rate_rps = EXCLUDED.rate_rps, burst = EXCLUDED.burst"""


class Paused(Exception):
    def __init__(self, host: str, reason: str):
        super().__init__(f"{host} paused: {reason}")
        self.host, self.reason = host, reason


class HostBudget:
    def __init__(self, pool, host: str, share: float = 1.0, max_wait_s: float = 300.0) -> None:
        if not 0 < share <= 1:
            raise ValueError("share must be in (0, 1]")
        self.pool, self.host, self.share, self.max_wait_s = pool, host, share, max_wait_s

    @staticmethod
    async def ensure(pool, host: str, rate_rps: float, burst: Optional[float] = None) -> None:
        await pool.execute(UPSERT_SQL, host, rate_rps, burst or max(1.0, rate_rps))

    async def take(self, n: float = 1.0) -> None:
        cost = n / self.share
        waited = 0.0
        while True:
            row = await self.pool.fetchrow(TAKE_SQL, self.host, cost)
            if row is not None:
                return
            st = await self.pool.fetchrow("SELECT paused_until > clock_timestamp() AS paused, pause_reason, rate_rps "
                                          "FROM host_budget WHERE host = $1", self.host)
            if st is None:
                raise KeyError(f"no budget for host {self.host}")
            if st["paused"]:
                raise Paused(self.host, st["pause_reason"] or "paused")
            delay = min(0.5, cost / max(st["rate_rps"], 0.01)) * (0.8 + 0.4 * random.random())
            await asyncio.sleep(delay)
            waited += delay
            if waited > self.max_wait_s:
                raise TimeoutError(f"waited {waited:.0f}s for a token on {self.host}")

    async def pause(self, seconds: float, reason: str) -> None:
        await self.pool.execute("UPDATE host_budget SET paused_until = clock_timestamp() + make_interval(secs => $2), "
                                "pause_reason = $3 WHERE host = $1", self.host, float(seconds), reason)
