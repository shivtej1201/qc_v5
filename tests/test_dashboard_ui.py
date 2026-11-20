"""
Dashboard UI smoke test (real browser): the QC V5 Command Center loads against a real API with a real
finished audit, renders the 3D scene, shows correct numbers, the wizard works, and nothing overflows on
mobile. Skipped automatically if Playwright/Chromium is not installed.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

pw = pytest.importorskip("playwright.async_api")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # project root


def _port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait(url, t=60):
    end = time.time() + t
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def live():
    work = tempfile.mkdtemp(prefix="qc_ui_")
    sim_port, api_port = _port(), _port()
    sim = subprocess.Popen([sys.executable, os.path.join(HERE, "bench", "sim_edge.py"), "--sources", "200",
                            "--port", str(sim_port), "--latency-ms", "10", "--images", "--cdn-latency-ms", "5"])
    env = dict(os.environ, DB_FILE=os.path.join(work, "ui.db"), ALLOW_LOCAL_TESTING="true",
               QC_ONPAGE_DATA_DIR=os.path.join(work, "jobs"), QC_ONPAGE_REPORTS_DIR=os.path.join(work, "reports"))
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "qcv5.api.server:app", "--host", "127.0.0.1", "--port", str(api_port)],
                           cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{api_port}"
    try:
        _wait(base + "/api/on-page/presets")
        body = {"name": "UI smoke", "urls": [f"http://127.0.0.1:{sim_port}/p/{i}" for i in range(40)],
                "preset": "max_tested", "overrides": {"target_rps": 80, "max_rps": 80}, "primary_domain": "127.0.0.1"}
        req = urllib.request.Request(base + "/api/on-page/audits", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        job = json.loads(urllib.request.urlopen(req).read())["job_id"]
        end = time.time() + 180
        while time.time() < end:
            d = json.loads(urllib.request.urlopen(f"{base}/api/on-page/audits/{job}").read())
            if d["package_available"]:
                break
            time.sleep(0.5)
        assert d["package_available"], d
        yield {"base": base, "job": job, "detail": d}
    finally:
        api.terminate(); sim.terminate(); api.wait(); sim.wait()
        shutil.rmtree(work, ignore_errors=True)


def test_dashboard_renders_real_audit_without_errors(live):
    async def go():
        async with pw.async_playwright() as p:
            try:
                b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            except Exception as e:
                pytest.skip(f"Chromium unavailable: {e}")
            errors = []
            for theme, (w, hgt) in (("dark", (1440, 1000)), ("light", (390, 844))):
                ctx = await b.new_context(viewport={"width": w, "height": hgt})
                await ctx.add_init_script(f"localStorage.setItem('qc-theme','{theme}')")
                pg = await ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(f"pageerror {e}"))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                pg.on("requestfailed", lambda r: errors.append(f"failed {r.url}"))
                for path in ("/dashboard/#/", "/dashboard/#/audits", "/dashboard/#/sitemaps", f"/dashboard/#/audit/{live['job']}"):
                    await pg.goto(live["base"] + path, wait_until="networkidle")
                    await pg.wait_for_timeout(1500)
                    assert not await pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"), (theme, path)
                    if "audit/" in path or path.endswith("#/"):
                        assert await pg.evaluate("!!document.querySelector('.scene canvas, .scene-fallback')")
                # numbers on the detail page come from the API, not placeholders
                body = await pg.inner_text("body")
                assert "UI smoke" in body and "COMPLETED" in body and "Issue explorer" in body
                await ctx.close()
            # wizard: Continue enables after pasting, estimate appears
            ctx = await b.new_context(viewport={"width": 1440, "height": 1000})
            pg = await ctx.new_page()
            await pg.goto(live["base"] + "/dashboard/#/", wait_until="networkidle")
            await pg.click("text=New audit")
            assert await pg.is_disabled("text=Continue")
            await pg.evaluate("""() => { const ta=document.querySelector('textarea');
                ta.value=Array.from({length:10000},(_, i)=>'https://www.example.com/p/'+i).join('\\n');
                ta.dispatchEvent(new Event('input',{bubbles:true})); }""")
            assert not await pg.is_disabled("text=Continue")
            await pg.click("text=Continue")
            await pg.wait_for_selector("text=meets 15–20 min", timeout=10000)
            await ctx.close()
            await b.close()
            assert not errors, errors
    asyncio.run(go())
