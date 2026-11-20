"""asyncpg pool + JSONB codecs."""
from __future__ import annotations

import json
from typing import Optional

import asyncpg

from qc6.settings import settings

_pool: Optional[asyncpg.Pool] = None


async def _init(conn: asyncpg.Connection) -> None:
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool(dsn: Optional[str] = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn or settings.database_url, min_size=1, max_size=10, init=_init)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def connect(dsn: Optional[str] = None) -> asyncpg.Connection:
    conn = await asyncpg.connect(dsn or settings.database_url)
    await _init(conn)
    return conn
