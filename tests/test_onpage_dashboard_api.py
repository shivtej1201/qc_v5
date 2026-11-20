"""
Dashboard API for the on-page URL + image audit service: start / guard / stop / resume / reports /
insights, driven through the real FastAPI app against the simulated edge (bench/sim_edge.py).
"""
import asyncio
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # project root


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def env():
    port = _free_port()
    sim = subprocess.Popen([sys.executable, os.path.join(HERE, "bench", "sim_edge.py"), "--sources", "400",
                            "--port", str(port), "--latency-ms", "20", "--images", "--cdn-latency-ms", "5"])
    end = time.time() + 20
    for host in ("127.0.0.1", "127.0.0.2", "127.0.0.3"):
        while time.time() < end:
            try:
                socket.create_connection((host, port), 0.3).close()
                break
            except OSError:
                time.sleep(0.1)
    work = tempfile.mkdtemp(prefix="qc_dash_api_")
    import qcv5.api.server as server
    from qcv5.core.db import Database
    from qcv5.services.onpage_service import OnPageAuditService
    saved = (server.db, server.onpage)
    server.db = Database(os.path.join(work, "api.db"))
    asyncio.run(server.db.connect())
    server.onpage = OnPageAuditService(server.db.db_path, base_dir=os.path.join(work, "jobs"),
                                       reports_dir=os.path.join(work, "reports"), allow_local=True)
    yield {"server": server, "base": f"http://127.0.0.1:{port}", "work": work}
    asyncio.run(server.db.close())
    server.db, server.onpage = saved
    sim.terminate()
    sim.wait()
    shutil.rmtree(work, ignore_errors=True)


async def _client(server):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t", timeout=60)


async def _wait_state(c, job_id, states, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        d = (await c.get(f"/api/on-page/audits/{job_id}")).json()
        if d["run_state"] in states:
            return d
        await asyncio.sleep(0.5)
    raise AssertionError(f"state never reached {states}: {d['run_state']} {d.get('log_tail')}")


def test_presets_and_estimates(env):
    async def go():
        async with await _client(env["server"]) as c:
            p = (await c.get("/api/on-page/presets")).json()["presets"]
            assert {"conservative", "target", "max_tested"} <= set(p)
            e8 = (await c.get("/api/on-page/estimate", params={"pages": 10000, "rps": 8})).json()
            e12 = (await c.get("/api/on-page/estimate", params={"pages": 10000, "rps": 12})).json()
            e_img = (await c.get("/api/on-page/estimate", params={"pages": 10000, "rps": 14, "images_on_primary": True})).json()
            assert not e8["meets_20_min_target"] and 22 < e8["estimated_minutes"] < 24
            assert e12["meets_20_min_target"] and 15 < e12["estimated_minutes"] < 16
            assert e8["rps_needed_for_20_min"] == e12["rps_needed_for_20_min"] and 8 < e8["rps_needed_for_20_min"] < 10
            assert e_img["estimated_minutes"] > 80                     # images under the primary budget
            assert (await c.get("/api/on-page/estimate", params={"pages": -1, "rps": 8})).status_code == 400
    asyncio.run(go())


def test_full_audit_via_api_guard_reports_insights(env):
    b = env["base"]
    urls = [f"{b}/p/{i}" for i in range(60)]

    async def go():
        async with await _client(env["server"]) as c:
            r = await c.post("/api/on-page/audits", json={"name": "API e2e", "urls": urls + urls[:5], "preset": "max_tested",
                                                          "overrides": {"target_rps": 60, "max_rps": 60},
                                                          "primary_domain": "127.0.0.1"})
            assert r.status_code == 200, r.text
            job = r.json()
            assert job["url_count"] == 60                                # duplicates in the request removed
            dup = await c.post("/api/on-page/audits", json={"name": "second", "urls": urls[:3], "primary_domain": "127.0.0.1"})
            assert dup.status_code == 409 and "one active crawl per domain" in dup.json()["detail"]
            d = await _wait_state(c, job["job_id"], ("COMPLETED", "PARTIAL", "FAILED", "STOPPED"))
            assert d["run_state"] == "COMPLETED" and d["final_qc_status"] == "PASS" and d["package_available"]
            assert d["stats"]["source_pages_completed"] == 60 and d["health"]["pages_total"] == 60
            jid = job["job_id"]
            ov = (await c.get(f"/api/on-page/audits/{jid}/issues/overview")).json()
            assert ov["total"] == sum(ov["by_severity"].values()) == int(ov["summary"]["issues_total"])
            assert ov["summary"]["final_qc_status"] == "PASS"
            hi = (await c.get(f"/api/on-page/audits/{jid}/issues", params={"severity": "HIGH", "limit": 5})).json()
            assert hi["matched"] == ov["by_severity"]["HIGH"] and len(hi["items"]) == min(5, hi["matched"])
            assert all(i["severity"] == "HIGH" and i["recommended_action"] for i in hi["items"])
            srch = (await c.get(f"/api/on-page/audits/{jid}/issues", params={"q": "/img/missing/"})).json()
            assert srch["matched"] >= 1 and all("/img/missing/" in i["normalized_url"] for i in srch["items"])
            pages = (await c.get(f"/api/on-page/audits/{jid}/pages", params={"limit": 3})).json()["pages"]
            assert len(pages) == 3 and int(pages[0]["high"]) >= int(pages[-1]["high"])
            ins = (await c.get(f"/api/on-page/audits/{jid}/insights")).json()["insights"]
            assert any(i["title"] == "Reconciliation PASS" for i in ins)
            assert any(i["title"].startswith("Fix first") for i in ins)
            z = await c.get(f"/api/on-page/audits/{jid}/package")
            assert z.status_code == 200
            with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
                assert len(zf.namelist()) == 18 and {"00_manifest.csv", "15_seo_page_audit.csv"} <= set(zf.namelist())
            f13 = await c.get(f"/api/on-page/audits/{jid}/files/13_issue_summary.csv")
            assert f13.status_code == 200 and f13.text.startswith("severity,category,issue_type")
            assert (await c.get(f"/api/on-page/audits/{jid}/files/..%2F..%2Fconfig.json")).status_code in (400, 404)
            assert (await c.get("/api/on-page/audits/not_a_job")).status_code == 400
            assert (await c.get("/api/on-page/audits/onpage_000000000000")).status_code == 404
            listing = (await c.get("/api/on-page/audits")).json()["audits"]
            assert any(a["job_id"] == jid and a["stats"]["source_pages_total"] == 60 for a in listing)
    asyncio.run(go())


def test_stop_then_resume_completes(env):
    b = env["base"]
    urls = [f"{b}/p/{i}" for i in range(100, 220)]

    async def go():
        async with await _client(env["server"]) as c:
            job = (await c.post("/api/on-page/audits", json={"name": "stop/resume", "urls": urls, "preset": "conservative",
                                                             "overrides": {"target_rps": 10, "max_rps": 10},
                                                             "primary_domain": "127.0.0.1"})).json()
            jid = job["job_id"]
            end = time.time() + 60
            while time.time() < end:
                st = (await c.get(f"/api/on-page/audits/{jid}")).json()["stats"]
                if st and st.get("source_pages_processed", 0) >= 15:
                    break
                await asyncio.sleep(0.3)
            assert (await c.post(f"/api/on-page/audits/{jid}/stop")).status_code == 200
            d = await _wait_state(c, jid, ("STOPPED", "COMPLETED"))
            assert d["run_state"] == "STOPPED" and not d["package_available"]
            assert 0 < d["stats"]["source_pages_processed"] < 120
            r = await c.post(f"/api/on-page/audits/{jid}/resume")
            assert r.status_code == 200
            assert (await c.post(f"/api/on-page/audits/{jid}/resume")).status_code == 409   # already running
            d = await _wait_state(c, jid, ("COMPLETED", "PARTIAL", "FAILED"))
            assert d["run_state"] == "COMPLETED" and d["final_qc_status"] == "PASS"
            assert d["stats"]["source_pages_completed"] == 120
    asyncio.run(go())
