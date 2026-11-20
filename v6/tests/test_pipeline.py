"""End to end on Postgres: coverage (V5 engine) + real visits + journeys -> fixes, lifecycle, score."""
import asyncio
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402

JOURNEY = """
name: Check loan eligibility
device: mobile
steps:
  - open: /apply-broken
  - fill: { "label=Monthly income": "85000" }
  - click: "role=button[name='Continue']"
    expect: [ { visible: "text=/You are eligible/" } ]
  - stop_before: "role=button[name='Submit application']"
"""


@pytest.fixture(scope="module")
def site_srv():
    s = FixtureSite()
    yield s
    s.close()


def test_full_run_creates_human_fixes(pg_pool, site_srv, tmp_path, monkeypatch):
    monkeypatch.setenv("QC6_DATA_DIR", str(tmp_path))
    from qc6 import settings as S
    monkeypatch.setattr(S.settings, "data_dir", str(tmp_path))

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Fixture Co", "priya@example.com", "Priya Nair")
        sid = await services.create_site(pool, ws["workspace_id"], "Fixture", site_srv.base, {"rate_rps": 40, "visitors": 4})
        await services.save_journey(pool, sid, JOURNEY)
        urls = site_srv.urls(per_template=8)
        rid = await services.start_run(pool, sid, "full", {"urls": urls, "fast_visits": True, "fast_journeys": True,
                                                          "per_template": 3, "images": False}, ws["member_id"])
        with pytest.raises(services.NotAllowed):
            await services.start_run(pool, sid, "coverage", {"urls": urls})            # one run per site at a time
        summary = await execute_run(pool, rid, fast_journeys=True)
        run = await pool.fetchrow("SELECT status, error, summary FROM runs WHERE id = $1", rid)
        fixes = await pool.fetch("SELECT rule, title, pages_affected, source, template_id, texts, status FROM fixes WHERE site_id = $1", sid)
        templates = await pool.fetch("SELECT name, page_count FROM templates WHERE site_id = $1 ORDER BY page_count DESC", sid)
        visits = await pool.fetch("SELECT status, device FROM visits WHERE run_id = $1", rid)
        jr = await pool.fetchrow("SELECT status, failed_step, failure_text FROM journey_runs WHERE run_id = $1", rid)
        score = await pool.fetchrow("SELECT score, dimensions FROM score_snapshots WHERE run_id = $1", rid)

        # second run after "fixing" nothing: open fixes stay open; mark one fixed -> run disagrees -> back to open
        fid = await pool.fetchval("SELECT id FROM fixes WHERE site_id = $1 AND rule = 'BROKEN_LINK'", sid)
        await services.update_fix(pool, fid, ws["member_id"], status="fixed_pending_check")
        with pytest.raises(services.NotAllowed):
            await services.update_fix(pool, fid, ws["member_id"], status="verified")
        rid2 = await services.start_run(pool, sid, "coverage", {"urls": urls, "images": False})
        await execute_run(pool, rid2)
        after = await pool.fetchrow("SELECT status FROM fixes WHERE id = $1", fid)
        # nothing changed on the site, so the second check must not "verify" anything (templates keep their identity)
        verified = await pool.fetchval("SELECT COUNT(*) FROM fixes WHERE site_id = $1 AND status = 'verified' AND source = 'coverage'", sid)
        tpl_count = await pool.fetchval("SELECT COUNT(*) FROM templates WHERE site_id = $1", sid)
        assert verified == 0 and tpl_count == len(templates), (verified, tpl_count, len(templates))
        events = [r["kind"] for r in await pool.fetch("SELECT kind FROM fix_events WHERE fix_id = $1 ORDER BY id", fid)]
        await pool.close()
        return run, summary, fixes, templates, visits, jr, score, after, events

    run, summary, fixes, templates, visits, jr, score, after, events = asyncio.run(go())
    assert run["status"] == "completed", run["error"]
    assert summary["trust_gate"] == "PASS"
    assert len(templates) >= 3 and sorted(t["page_count"] for t in templates)[-3:] == [8, 8, 8]
    rules = {f["rule"]: f for f in fixes}
    # coverage
    assert rules["BROKEN_LINK"]["pages_affected"] == 24 and rules["BROKEN_LINK"]["source"] == "coverage"
    assert rules["H1_MISSING"]["title"].endswith("have no main heading")
    # real visits (sampled 3 per template, projected to the template's 8 pages)
    assert "CONTENT_ONLY_AFTER_JS" in rules and rules["CONTENT_ONLY_AFTER_JS"]["pages_affected"] == 8
    assert "CONSOLE_ERRORS" in rules and "A11Y_SERIOUS" in rules
    assert {v["status"] for v in visits} == {"ok"} and len(visits) == 9
    # journeys
    assert jr["status"] == "failed" and jr["failed_step"] == 3
    assert rules["JOURNEY_FAILED"]["title"] == 'The journey "Check loan eligibility" fails at step 3'
    # every fix speaks like a person
    for f in fixes:
        for k in ("title", "why", "fix", "verify"):
            assert "{" not in f["texts"][k] and "_" not in f["texts"][k].split("http")[0].replace("<h1>", ""), (f["rule"], k, f["texts"][k])
    assert score["score"] is not None and 0 < score["score"] < 100
    # lifecycle: someone said fixed, the next check disagreed
    assert after["status"] == "open" and events[:2] == ["found", "status"] and events[-1] == "status"
