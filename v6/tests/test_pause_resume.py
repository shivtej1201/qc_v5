"""Pause a running check mid-crawl, resume it, and get the same result as an uninterrupted check."""
import asyncio
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402


@pytest.fixture(scope="module")
def site_srv():
    s = FixtureSite()
    yield s
    s.close()


def test_pause_then_resume_completes_without_redoing_work(pg_pool, site_srv, tmp_path, monkeypatch):
    from qc6.settings import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Pause Co", "p@pause.test", "Pia")
        sid = await services.create_site(pool, ws["workspace_id"], "Pause", site_srv.base, {"rate_rps": 6, "visitors": 2})
        urls = site_srv.urls(per_template=10)                       # 30 pages at ~5 req/s for coverage
        rid = await services.start_run(pool, sid, "full", {"urls": urls, "fast_visits": True, "per_template": 2,
                                                          "images": False})
        task = asyncio.create_task(execute_run(pool, rid))
        for _ in range(100):                                        # wait until the crawl is under way
            await asyncio.sleep(0.2)
            prog = await pool.fetchval("SELECT progress FROM runs WHERE id = $1", rid) or {}
            if 3 <= int(prog.get("pages_done", 0)) < 25:
                break
        await pool.execute("UPDATE runs SET status = 'pausing' WHERE id = $1", rid)     # what POST /pause does
        out1 = await asyncio.wait_for(task, 60)
        st1 = await pool.fetchrow("SELECT status, progress FROM runs WHERE id = $1", rid)
        with pytest.raises(services.NotAllowed, match="paused"):
            await services.start_run(pool, sid, "coverage", {"urls": urls})
        pages_before = await pool.fetchval("SELECT COUNT(*) FROM pages WHERE run_id = $1", rid)

        await pool.execute("UPDATE runs SET status = 'queued' WHERE id = $1", rid)      # what POST /resume does
        out2 = await execute_run(pool, rid)
        st2 = await pool.fetchrow("SELECT status, progress, summary FROM runs WHERE id = $1", rid)
        pages = await pool.fetchval("SELECT COUNT(*) FROM pages WHERE run_id = $1", rid)
        dup_visits = await pool.fetchval("SELECT COUNT(*) - COUNT(DISTINCT (page_url, device)) FROM visits WHERE run_id = $1", rid)
        tpl = await pool.fetchval("SELECT COUNT(*) FROM templates WHERE site_id = $1", sid)
        await pool.close()
        return out1, st1, pages_before, out2, st2, pages, dup_visits, tpl

    out1, st1, pages_before, out2, st2, pages, dup_visits, tpl = asyncio.run(go())
    assert out1.get("paused") and st1["status"] == "paused" and st1["progress"].get("stages_done", []) == []
    assert pages_before == 0                                      # nothing imported half-way
    assert st2["status"] == "completed", st2
    assert st2["progress"]["stages_done"] == ["coverage", "visits", "journeys"]
    assert pages == 30 and st2["summary"]["trust_gate"] == "PASS"
    assert tpl == 3                                               # fingerprints from before the pause were kept
    assert dup_visits == 0
