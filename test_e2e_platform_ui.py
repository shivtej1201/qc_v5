"""
Playwright E2E for the platform UI on the frozen API (real server, real worker, local fixture site):
New audit -> From sitemap -> discover -> launch -> live lifecycle -> completed -> health score -> URL explorer ->
URL detail -> Link audit -> Image audit -> Issue explorer (detail + lifecycle change) -> Remediation -> Reports
(manifest, ZIP) and a 300,000-row virtualized grid. Skipped if Playwright/Chromium is unavailable.
"""
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

pw = pytest.importorskip("playwright.async_api")
HERE = os.path.dirname(os.path.abspath(__file__))


def _port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def live():
    from test_seo_sitemap_audit import Site
    site = Site()
    work = tempfile.mkdtemp(prefix="qc_e2e_")
    port = _port()
    env = dict(os.environ, DB_FILE=os.path.join(work, "e2e.db"), ALLOW_LOCAL_TESTING="true",
               QC_ONPAGE_DATA_DIR=os.path.join(work, "jobs"), QC_ONPAGE_REPORTS_DIR=os.path.join(work, "reports"))
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port)],
                           cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    end = time.time() + 60
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/api/on-page/presets", timeout=2)
            break
        except Exception:
            time.sleep(0.3)
    yield {"base": base, "site": site, "work": work, "db": env["DB_FILE"], "jobs": env["QC_ONPAGE_DATA_DIR"]}
    api.terminate(); api.wait(); site.close()
    shutil.rmtree(work, ignore_errors=True)


def _make_large_link_job(db_path: str, jobs_dir: str, src_job: str, rows: int = 300_000) -> str:
    """Clone a finished audit's metadata and add `rows` synthetic link relationships (SQL only, no crawl)."""
    new = "onpage_" + "f" * 12
    conn = sqlite3.connect(db_path)
    with conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(on_page_link_relationships)") if r[1] != "id"]
        sel = ", ".join("? AS job_id" if c == "job_id" else
                        f"normalized_found_url || '/gen/' || n.v AS {c}" if c == "normalized_found_url" else
                        f"found_url || '/gen/' || n.v AS {c}" if c == "found_url" else c for c in cols)
        conn.execute(f"""INSERT INTO on_page_link_relationships ({', '.join(cols)})
                         WITH RECURSIVE n(v) AS (SELECT 1 UNION ALL SELECT v + 1 FROM n WHERE v < ?)
                         SELECT {sel} FROM n, (SELECT * FROM on_page_link_relationships WHERE job_id = ? LIMIT 1)""",
                     (rows, new, src_job))
    conn.close()
    meta = json.load(open(os.path.join(jobs_dir, src_job, "config.json")))
    meta.update(job_id=new, name="Synthetic 300K links", events=[])
    os.makedirs(os.path.join(jobs_dir, new), exist_ok=True)
    json.dump(meta, open(os.path.join(jobs_dir, new, "config.json"), "w"))
    return new


def test_platform_ui_end_to_end(live):
    base, site = live["base"], live["site"]

    async def go():
        async with pw.async_playwright() as p:
            try:
                b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            except Exception as e:
                pytest.skip(f"Chromium unavailable: {e}")
            errors = []
            ctx = await b.new_context(viewport={"width": 1440, "height": 1000})
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror {e}"))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "404" not in m.text else None)
            assert await pg.evaluate("1") == 1
            await pg.goto(base + "/dashboard/#/", wait_until="networkidle")
            assert await pg.evaluate("typeof window.gsap === 'object' && window.gsap.version.startsWith('3.12')")

            # ---- create audit from a sitemap (domain -> robots.txt -> index -> urlsets)
            await pg.click("text=New audit")
            await pg.click("text=From sitemap")
            await pg.fill("textarea[aria-label^='Sitemap URLs']", site.base)
            await pg.click("text=Discover sitemaps")
            await pg.wait_for_selector(".sm-tree", timeout=30000)
            await pg.wait_for_function("document.querySelector('.estimate b')?.textContent === '13'", timeout=10000)
            await pg.click("text=Continue")
            await pg.fill("input[aria-label='Requests per second']", "30")
            await pg.click("text=Continue")
            await pg.fill("#wiz-name", "E2E sitemap audit")
            await pg.fill("#wiz-domain", "127.0.0.1")
            await pg.click("text=Launch audit")
            await pg.wait_for_url("**/#/audit/onpage_*", timeout=15000)
            job = pg.url.rsplit("/", 1)[1]

            # ---- live lifecycle from the backend until COMPLETED (page polls; we only read the DOM)
            await pg.wait_for_selector(".pill.st-COMPLETED", timeout=180000)
            await pg.wait_for_selector("[aria-label='SEO health score'] .bars", timeout=30000)
            body = (await pg.inner_text("main")).lower()
            assert "qcv5-health-1.0" in body and "processed" in body and "throughput" in body, body[:1500]

            # ---- URL explorer -> sort -> row -> URL detail
            await pg.click(".tabs >> text=URLs")
            await pg.wait_for_selector(".vgrid-row", timeout=15000)
            assert "13 row(s)" in await pg.inner_text(".grid-status")
            await pg.click(".vgrid-th >> text=Status")
            await pg.wait_for_selector(".vgrid-row", timeout=10000)
            await pg.click(f".vgrid-row:has-text('{site.base}/noindex')")
            await pg.wait_for_url("**/url?u=*", timeout=10000)
            await pg.wait_for_selector("text=Canonical & robots", timeout=10000)
            detail = await pg.inner_text("main")
            assert "noindex" in detail and "NOINDEX_IN_SITEMAP" in detail and "Raw evidence" in detail

            # ---- Link audit with a server-side filter
            await pg.click(".tabs >> text=Links")
            await pg.wait_for_selector(".vgrid-row", timeout=15000)
            await pg.select_option("select[aria-label='URL type']", "CANONICAL")
            await pg.wait_for_function("document.querySelector('.grid-status').textContent.startsWith('13 row')", timeout=10000)

            # ---- Image audit
            await pg.click(".tabs >> text=Images")
            await pg.wait_for_selector(".vgrid-row", timeout=15000)

            # ---- Issues: open detail, change lifecycle status
            await pg.click(".tabs >> text=Issues")
            await pg.wait_for_selector(".vgrid-row", timeout=15000)
            await pg.click(".vgrid-row >> nth=0")
            await pg.wait_for_selector("[aria-labelledby='issue-title']", timeout=10000)
            assert "Recommended fix" in await pg.inner_text(".modal")
            await pg.select_option("select[aria-label='Issue status']", "IN_PROGRESS")
            await pg.fill("input[aria-label='Status note']", "SEO-42")
            await pg.click("text=Save status")
            await pg.wait_for_selector(".toast:has-text('IN_PROGRESS')", timeout=10000)
            await pg.select_option("select[aria-label='Status']", "IN_PROGRESS")
            await pg.wait_for_function("document.querySelector('.grid-status').textContent.startsWith('1 row')", timeout=10000)

            # ---- Remediation
            await pg.click(".tabs >> text=Remediation")
            await pg.click("text=Generate from issues")
            await pg.wait_for_selector(".toast:has-text('remediation item')", timeout=20000)
            await pg.wait_for_selector("main table tbody tr", timeout=10000)

            # ---- Reports: manifest + ZIP
            await pg.click(".tabs >> text=Reports")
            await pg.wait_for_selector("text=Trust gate PASS", timeout=15000)
            assert "Manifest 17/17 OK" in await pg.inner_text("main")
            href = await pg.get_attribute("a:has-text('Download ZIP')", "href")
            with urllib.request.urlopen(base + href) as r:
                assert r.status == 200 and r.read(2) == b"PK"
            assert not await pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")

            # ---- 300,000-row link dataset: virtualized (DOM stays small) and the last row is reachable
            big = _make_large_link_job(live["db"], live["jobs"], job)
            t0 = time.time()
            await pg.goto(base + f"/dashboard/#/audit/{big}/links", wait_until="networkidle")
            await pg.wait_for_function("document.querySelector('.grid-status')?.textContent.includes('row')", timeout=30000)
            assert time.time() - t0 < 15
            status = await pg.inner_text(".grid-status")
            assert status.replace(",", "").replace("\u202f", "").startswith("300000 row"), status
            n_rows = await pg.evaluate("document.querySelectorAll('.vgrid-row').length")
            assert 0 < n_rows < 80, n_rows
            await pg.evaluate("document.querySelector('.vgrid-viewport').scrollTop = 1e9")
            await pg.wait_for_function("[...document.querySelectorAll('.vgrid-row')].some(r => Number(r.getAttribute('aria-rowindex')) >= 300000)", timeout=20000)
            assert await pg.evaluate("document.querySelectorAll('.vgrid-row').length") < 80
            await b.close()
            assert not errors, errors
    asyncio.run(go())
