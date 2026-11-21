import asyncio
import os

import pytest

TEST_DSN = os.environ.get("QC6_TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/qc6_test")
os.environ.setdefault("QC6_DATABASE_URL", TEST_DSN)


@pytest.fixture(scope="session")
def migrated():
    import asyncpg
    from qc6.db.migrate import migrate
    from qc6.settings import clean_dsn  # imported here: test modules set QC6_* before settings are read
    dsn = clean_dsn(TEST_DSN)

    async def reset():
        conn = await asyncpg.connect(dsn)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.close()
        await migrate(dsn)
    try:
        asyncio.run(reset())
    except OSError as e:
        pytest.skip(f"Postgres not reachable: {e}")
    return dsn


@pytest.fixture
def pg_pool(migrated):
    import asyncpg
    from qc6.db.pool import _init

    async def make():
        return await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
    return make
