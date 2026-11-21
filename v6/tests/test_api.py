"""API v2 on a real run of the fixture site."""
import asyncio
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.settings import settings  # noqa: E402

JOURNEY = """
name: Apply flow
device: desktop
steps:
  - open: /apply
  - click: "role=button[name='Continue']"
    expect: [ { visible: "text=/You are eligible/" } ]
"""


@pytest.fixture(scope="module")
def world(migrated, tmp_path_factory):
    site = FixtureSite()
    data = tmp_path_factory.mktemp("data")
    settings.data_dir = str(data)
    import asyncpg
    from qc6.db.pool import _init
    from qc6.workers.runner import execute_run

    async def seed():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "API Co", "lead@api.test", "Asha Rao")
        viewer = await services.add_member(pool, ws["workspace_id"], "view@api.test", "Vik Viewer", "viewer")
        dev = await services.add_member(pool, ws["workspace_id"], "dev@api.test", "Rahul Mehta", "editor")
        sid = await services.create_site(pool, ws["workspace_id"], "Fixture API", site.base, {"rate_rps": 40, "visitors": 3})
        await services.save_journey(pool, sid, JOURNEY)
        rid = await services.start_run(pool, sid, "full", {"urls": site.urls(4), "fast_visits": True, "fast_journeys": True,
                                                          "per_template": 2, "images": False})
        await execute_run(pool, rid, fast_journeys=True)
        other = await services.create_workspace(pool, "Other Co", "x@other.test", "Xavier")
        osid = await services.create_site(pool, other["workspace_id"], "Theirs", "https://other.test")
        await pool.close()
        return {"site": sid, "run": rid, "dev": dev, "viewer": viewer, "other_site": osid}
    w = asyncio.run(seed())
    from qc6.api.app import app
    with TestClient(app) as c:
        w["c"] = c
        yield w
    site.close()


H = {"X-QC-User": "lead@api.test"}


def test_today_and_sites(world):
    c = world["c"]
    t = c.get(f"/api/v2/sites/{world['site']}/today", headers=H).json()
    assert t["greeting"] == "Hello, Asha" and t["score"] is not None
    assert t["checked"].startswith("Checked 12 pages, visited") and "walked 1 journey" in t["checked"]
    kinds = [x["kind"] for x in t["cards"]]
    assert "journey" not in kinds and "unassigned" in kinds and len(t["cards"]) <= 5      # the journey passed
    s = c.get("/api/v2/sites", headers=H).json()["sites"]
    assert [x["name"] for x in s] == ["Fixture API"] and "access_token" not in s[0]


def test_fix_inbox_detail_and_changes(world):
    c = world["c"]
    data = c.get(f"/api/v2/sites/{world['site']}/fixes?view=needs", headers=H).json()
    assert data["counts"]["needs"] == len(data["fixes"]) > 0
    sev = [f["severity"] for f in data["fixes"]]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    assert sev == sorted(sev, key=order.get)
    broken = next(f for f in data["fixes"] if f["rule"] == "BROKEN_LINK")
    d = c.get(f"/api/v2/fixes/{broken['id']}", headers=H).json()
    assert set(d["fix"]["texts"]) == {"title", "why", "fix", "verify"} and len(d["pages"]) == 12
    assert d["events"][0]["kind"] == "found"
    r = c.patch(f"/api/v2/fixes/{broken['id']}", headers=H, json={"owner_id": world["dev"], "status": "in_progress",
                                                                  "comment": "Taking this one."})
    assert r.status_code == 200 and r.json()["owner_id"] == world["dev"]
    r = c.patch(f"/api/v2/fixes/{broken['id']}", headers=H, json={"status": "verified"})
    assert r.status_code == 409 and "Only a completed check" in r.json()["detail"]
    r = c.patch(f"/api/v2/fixes/{broken['id']}", headers={"X-QC-User": "view@api.test"}, json={"status": "open"})
    assert r.status_code == 403
    q = c.get(f"/api/v2/sites/{world['site']}/fixes?view=all&q=heading", headers=H).json()["fixes"]
    assert q and all("heading" in f["title"].lower() for f in q)


def test_page_journeys_templates_access_objects(world):
    c = world["c"]
    tpl = c.get(f"/api/v2/sites/{world['site']}/templates", headers=H).json()["templates"]
    assert len(tpl) >= 3 and all(t["visits"] >= 1 for t in tpl[:3])
    url = c.get(f"/api/v2/sites/{world['site']}/pages?q=city", headers=H).json()["pages"][0]["url"]
    p = c.get(f"/api/v2/sites/{world['site']}/page", params={"url": url}, headers=H).json()
    if p["visit"]:
        assert p["visit"]["render_diff"]["h1_only_after_js"] is True
        shot = c.get(f"/api/v2/objects/{p['visit']['screenshot']}", headers=H)
        assert shot.status_code == 200 and shot.headers["content-type"] == "image/jpeg"
    assert c.get("/api/v2/objects/..%2F..%2Fetc%2Fpasswd", headers=H).status_code == 404
    j = c.get(f"/api/v2/sites/{world['site']}/journeys", headers=H).json()["journeys"]
    assert j[0]["name"] == "Apply flow" and j[0]["recent"][0]["status"] == "passed"
    steps = c.get(f"/api/v2/journey-runs/{j[0]['recent'][0]['id']}", headers=H).json()["steps"]
    assert [s["label"] for s in steps] == ["Open /apply", 'Tap "Continue"']
    bad = c.post(f"/api/v2/sites/{world['site']}/journeys", headers=H, json={"yaml": "name: x\nsteps:\n  - click: \"role=button[name='Pay now']\""})
    assert bad.status_code == 400 and "irreversible" in bad.json()["detail"]
    acc = c.get(f"/api/v2/sites/{world['site']}/access", headers=H).json()
    assert acc["header"] == "X-QC-Audit" and acc["token"] in acc["rules"]["akamai"]
    assert c.get(f"/api/v2/sites/{world['site']}/access", headers={"X-QC-User": "view@api.test"}).status_code == 403


def test_workspace_isolation_and_runs(world):
    c = world["c"]
    assert c.get(f"/api/v2/sites/{world['other_site']}", headers=H).status_code == 404
    assert c.get(f"/api/v2/sites/{world['other_site']}/fixes", headers=H).status_code == 404
    r = c.post(f"/api/v2/sites/{world['site']}/runs", headers=H, json={"kind": "coverage", "options": {"urls": ["x"]}})
    assert r.status_code == 201
    r2 = c.post(f"/api/v2/sites/{world['site']}/runs", headers=H, json={"kind": "coverage"})
    assert r2.status_code == 409
    assert c.post(f"/api/v2/runs/{r.json()['run_id']}/stop", headers=H).json()["status"] == "cancelled"
    run = c.get(f"/api/v2/runs/{world['run']}", headers=H).json()
    assert run["run"]["status"] == "completed" and run["found"]


def test_security_headers(world):
    r = world["c"].get("/api/v2/me", headers=H)
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_links_explorer(world):
    c = world["c"]
    r = c.get(f"/api/v2/runs/{world['run']}/facts/links?filter=broken&limit=50", headers=H).json()
    assert r["total"] == 12 and all(x["url"].endswith("/gone") for x in r["rows"])    # every page links to /gone
    assert c.get(f"/api/v2/runs/{world['run']}/facts/links?filter=bogus", headers=H).status_code == 400
    csvr = c.get(f"/api/v2/runs/{world['run']}/export/links.csv?filter=broken", headers=H)
    assert csvr.status_code == 200 and csvr.text.count("\n") == 13
