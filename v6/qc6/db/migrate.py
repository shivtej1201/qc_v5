"""Apply SQL migrations in order (idempotent). `python -m qc6.db.migrate`."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from qc6.db.pool import connect

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


async def ensure_database(dsn: str) -> bool:
    """Create the database named in the DSN if it doesn't exist yet (first start). True if it was created."""
    import urllib.parse
    import asyncpg
    try:
        conn = await asyncpg.connect(dsn)
        await conn.close()
        return False
    except asyncpg.InvalidCatalogNameError:
        pass
    u = urllib.parse.urlsplit(dsn)
    name = u.path.lstrip("/")
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"Database name {name!r} must be letters, digits and underscores.")
    admin = urllib.parse.urlunsplit((u.scheme, u.netloc, "/postgres", u.query, ""))
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    return True


async def migrate(dsn: Optional[str] = None) -> list[str]:
    from qc6.settings import settings
    if await ensure_database(dsn or settings.database_url):
        print(f"Created database {(dsn or settings.database_url).rsplit('/', 1)[-1]}")
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


def _explain(e: BaseException, dsn: str) -> str:
    import socket
    import urllib.parse
    import asyncpg
    u = urllib.parse.urlsplit(dsn)
    where = f"{u.hostname}:{u.port or 5432}"
    if isinstance(e, socket.gaierror):
        return (f"The database address points at a computer called {u.hostname!r}, which doesn't exist. "
                "Check QC6_DATABASE_URL, or set the password on its own with QC6_DB_PASSWORD.")
    if isinstance(e, (ConnectionRefusedError, ConnectionError)) or (isinstance(e, OSError) and not isinstance(e, asyncpg.PostgresError)):
        return f"Nothing answered at {where}. Is PostgreSQL installed and running (Windows: Services > postgresql)?"
    if isinstance(e, asyncpg.InvalidPasswordError):
        return f"PostgreSQL at {where} said the password for user {u.username!r} is wrong."
    return f"Could not use the database at {where}: {e}"


if __name__ == "__main__":
    import sys
    from qc6.settings import settings
    try:
        print("applied:", asyncio.run(migrate()) or "nothing (up to date)")
    except Exception as e:  # noqa: BLE001 - one plain sentence instead of a traceback on first start
        print("\n" + _explain(e, settings.database_url), file=sys.stderr)
        sys.exit(1)
