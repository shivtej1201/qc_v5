"""Apply SQL migrations in order (idempotent). `python -m qc6.db.migrate`."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from qc6.db.pool import connect

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


async def migrate(dsn: Optional[str] = None) -> list[str]:
    conn = await connect(dsn)
    applied = []
    try:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, "
                           "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for name in sorted(f for f in os.listdir(HERE) if f.endswith(".sql")):
            if name in done:
                continue
            sql = open(os.path.join(HERE, name), encoding="utf-8").read()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", name)
            applied.append(name)
    finally:
        await conn.close()
    return applied


if __name__ == "__main__":
    print("applied:", asyncio.run(migrate()) or "nothing (up to date)")
