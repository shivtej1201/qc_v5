"""
Full-pipeline validation on a deterministic local site (no external websites):

input list (with duplicates + invalid lines) -> crawl -> HTTP classification (200/301/302/403/404/410/500/timeout/
robots) -> redirect chains -> all-URL extraction (links, scripts, styles, forms, JSON-LD, social, CSS images, srcset,
lazy images) -> link + image validation -> issues in the DB (deduplicated, lifecycle) -> health score ->
18-file package (manifest + checksums) -> server-side explorers -> remediation -> second audit (fix verified,
regression re-opened) -> pause / resume / cancel -> crash recovery.
"""
import asyncio
import csv
import io
import json
import os
import shutil
import signal
import sqlite3
import tempfile
import threading
import time
import zipfile
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 200


class FixtureSite:
    """Deterministic site. `fixed` flips /missing to 200 (simulates the fix between two audits)."""

    def __init__(self, slow_pages: float = 0.0):
        self.hits = Counter()
        self.fixed = False
        self.slow_pages = slow_pages
        site = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                site.hits[path] += 1
                b = f"http://127.0.0.1:{site.port}"
                ext = f"http://127.0.0.2:{site.port}"
                if path.startswith("/p/") and site.slow_pages:
                    time.sleep(site.slow_pages)
                if path == "/robots.txt":
                    return self._send(200, "User-agent: *\nDisallow: /private/\n", "text/plain")
                if path == "/r301":
                    return self._redirect(301, "/p/target")
                if path == "/r302":
                    return self._redirect(302, "/chain1")
                if path == "/chain1":
                    return self._redirect(301, "/chain2")
                if path == "/chain2":
                    return self._redirect(301, "/p/target")
                if path == "/p/redirected":
                    return self._redirect(301, "/p/target")
                if path == "/missing":
                    return self._send(200, "<html><title>Back</title><body>ok</body></html>", "text/html") if site.fixed \
                        else self._send(404, "nf", "text/html")
                if path == "/forbidden":
                    return self._send(403, "no", "text/html")
                if path in ("/boom", "/p/boom"):
                    return self._send(500, "err", "text/html")
                if path == "/p/gone":
                    return self._send(410, "gone", "text/html")
                if path == "/slow":
                    time.sleep(3)
                    return self._send(200, "late", "text/html")
                if path == "/static/app.js":
                    return self._send(200, "var a=1;", "application/javascript")
                if path == "/static/site.css":
                    return self._send(404, "nf", "text/css")
                if path in ("/img/a.png", "/img/a-2x.png", "/img/lazy.png", "/img/bg.png", "/img/logo.png", "/img/pic.webp"):
                    return self._send(200, PNG, "image/png")
                if path == "/img/missing.png":
                    return self._send(404, "nf", "text/html")
                if path == "/ext":
                    return self._send(200, "<html><body>external</body></html>", "text/html")
                if path == "/p/target":
                    return self._send(200, page("Target page that is long enough here", "/p/target", b), "text/html")
                if path == "/p/ok":
                    return self._send(200, ok_page(b, ext), "text/html")
                if path == "/p/fr":
                    return self._send(200, page("Page française assez longue pour le titre", "/p/fr", b,
                                                head=f'<link rel="alternate" hreflang="fr" href="{b}/p/fr">', lang="fr"),
                                      "text/html")
                if path in ("/p/dup1", "/p/dup2"):
                    return self._send(200, page("Duplicate title shared by two pages", path, b, h1=False), "text/html")
                if path == "/p/notitle":
                    return self._send(200, page("", path, b), "text/html")
                if path == "/p/noindex":
                    return self._send(200, page("Noindex page with a long enough title", path, b,
                                                head='<meta name="robots" content="noindex">'), "text/html")
                return self._send(404, "nf", "text/html")

            def _redirect(self, code, loc):
                self.send_response(code)
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send(self, code, body, ctype):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype == "text/html" else ""))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), H)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


WORDS = " ".join(f"word{i}" for i in range(220))


def page(title, path, b, head="", h1=True, lang="en"):
    t = f"<title>{title}</title>" if title else ""
    return (f'<!doctype html><html lang="{lang}"><head>{t}<meta name="description" content="{"A description long enough to pass the seventy character minimum for this page."}">'
            f'<meta name="viewport" content="width=device-width"><link rel="canonical" href="{b}{path}">{head}</head>'
            f'<body>{"<h1>Heading " + path + "</h1>" if h1 else ""}<p>{WORDS} {path}</p><a href="/p/ok">home</a></body></html>')


def ok_page(b, ext):
    head = (f'<link rel="alternate" hreflang="en" href="{b}/p/ok"><link rel="alternate" hreflang="fr" href="{b}/p/fr">'
            f'<link rel="alternate" hreflang="x-default" href="{b}/p/ok"><link rel="stylesheet" href="/static/site.css">'
            f'<script src="/static/app.js"></script><meta property="og:url" content="{b}/p/ok">'
            '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization",'
            f'"url":"{b}/p/target","logo":"{b}/img/logo.png"' + '}</script>')
    body = (f'<a href="/p/target">t</a><a href="/r301">r1</a><a href="/r302">r2</a><a href="/missing">m</a>'
            f'<a href="/forbidden">f</a><a href="/boom">b</a><a href="/slow">s</a><a href="/private/x">p</a>'
            f'<a href="{ext}/ext">external</a><a href="mailto:a@b.c">mail</a><a href="/p/target">again</a>'
            f'<form action="/search" method="post"></form>'
            f'<img src="/img/a.png" srcset="/img/a.png 1x, /img/a-2x.png 2x" alt="A">'
            f'<img data-src="/img/lazy.png" src="/img/lazy.png" loading="lazy">'
            f'<img src="/img/missing.png" alt="">'
            f'<picture><source srcset="/img/pic.webp"><img src="/img/a.png" alt="A"></picture>'
            f'<div style="background-image:url(/img/bg.png)"></div>')
    return page("Home page title that is long enough", "/p/ok", b, head=head).replace("</body>", body + "</body>")


# ---------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def env():
    site = FixtureSite()
    work = tempfile.mkdtemp(prefix="qc_platform_")
    import qcv5.api.server as server
    from qcv5.core.db import Database
    from qcv5.services.onpage_service import OnPageAuditService
    from qcv5.audit.remediation_engine import RemediationEngine
    saved = (server.db, server.onpage, server.remediation_engine)
    server.db = Database(os.path.join(work, "api.db"))
    asyncio.run(server.db.connect())
    server.remediation_engine = RemediationEngine(server.db)
    server.onpage = OnPageAuditService(server.db.db_path, base_dir=os.path.join(work, "jobs"),
                                       reports_dir=os.path.join(work, "reports"), allow_local=True)
    yield {"server": server, "site": site, "work": work}
    asyncio.run(server.db.close())
    server.db, server.onpage, server.remediation_engine = saved
    site.close()
    shutil.rmtree(work, ignore_errors=True)


FAST = {"target_rps": 60, "max_rps": 60, "read_timeout": 1.0, "max_retries": 0}


def _client(srv):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=srv.app), base_url="http://t", timeout=120)


async def _wait(c, job_id, done=("COMPLETED", "PARTIAL", "FAILED", "PAUSED", "CANCELLED", "INTERRUPTED"), timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        d = (await c.get(f"/api/on-page/audits/{job_id}")).json()
        if d["lifecycle"] in done and (d["package_available"] or d["lifecycle"] in ("PAUSED", "CANCELLED", "INTERRUPTED")):
            return d
        await asyncio.sleep(0.3)
    raise AssertionError(f"audit did not finish: {d.get('lifecycle')} {d.get('log_tail')}")


def _rows(zf, name):
    return list(csv.DictReader(io.StringIO(zf.read(name).decode("utf-8"))))


SITEMAP_PATHS = ["/p/ok", "/p/target", "/p/fr", "/p/dup1", "/p/dup2", "/p/notitle", "/p/noindex", "/p/redirected",
                 "/p/gone", "/p/boom"]


def test_full_pipeline_on_fixture_site(env):
    srv, site = env["server"], env["site"]
    b = site.base
    urls = [b + p for p in SITEMAP_PATHS] + [b + "/p/ok", b.upper().replace("HTTP://", "http://") + "/p/ok#top",
                                              "not a url", "ftp://127.0.0.1/x"]

    async def go():
        async with _client(srv) as c:
            r = await c.post("/api/on-page/audits", json={"name": "Fixture", "urls": urls, "preset": "conservative",
                                                          "overrides": FAST, "primary_domain": "127.0.0.1"})
            assert r.status_code == 200, r.text
            job = r.json()
            assert job["lifecycle"] in ("QUEUED", "RUNNING")
            d = await _wait(c, job["job_id"])
            assert d["lifecycle"] == "COMPLETED" and d["final_qc_status"] == "PASS", d.get("log_tail")
            states = [e["state"] for e in d["events"]]
            assert states[:3] == ["CREATED", "QUEUED", "RUNNING"] and states[-1] == "COMPLETED"
            assert d["progress"]["processed"] == d["progress"]["total"] == 10 and d["progress"]["source"] == "backend"
            assert d["score"]["formula_version"] == "qcv5-health-1.0" and 0 <= d["score"]["score"] <= 100
            jid = job["job_id"]
            zbytes = (await c.get(f"/api/on-page/audits/{jid}/package")).content

            # explorers (server-side paging / filters / sort)
            links4 = (await c.get(f"/api/on-page/audits/{jid}/links", params={"status_bucket": "4xx", "limit": 2})).json()
            assert links4["matched"] >= 3 and len(links4["items"]) == 2
            page2 = (await c.get(f"/api/on-page/audits/{jid}/links", params={"status_bucket": "4xx", "limit": 2, "offset": 2})).json()
            assert not {i["id"] for i in page2["items"]} & {i["id"] for i in links4["items"]}
            srt = (await c.get(f"/api/on-page/audits/{jid}/urls", params={"sort": "status", "order": "desc"})).json()
            codes = [i["http_status"] for i in srt["items"] if i["http_status"] is not None]
            assert codes == sorted(codes, reverse=True) and srt["matched"] == 10
            assert (await c.get(f"/api/on-page/audits/{jid}/urls", params={"sort": "nope"})).status_code == 400
            assert (await c.get(f"/api/on-page/audits/{jid}/links", params={"limit": 5000})).status_code == 400
            imgs = (await c.get(f"/api/on-page/audits/{jid}/images", params={"alt": "missing"})).json()
            assert any(i["normalized_image_url"].endswith("/img/lazy.png") for i in imgs["items"])
            iss = (await c.get(f"/api/on-page/audits/{jid}/issues", params={"severity": "HIGH", "limit": 500})).json()
            assert iss["source"] == "database" and all(i["severity"] == "HIGH" for i in iss["items"])
            nf = (await c.get(f"/api/on-page/audits/{jid}/issues", params={"issue_type": "NOT_FOUND", "limit": 500})).json()
            missing = [i for i in nf["items"] if i["normalized_url"].endswith("/missing")]
            assert len(missing) == 1 and missing[0]["status"] == "OPEN"

            # issue detail + URL detail (evidence for the detail views)
            det = (await c.get(f"/api/on-page/audits/{jid}/issues/{missing[0]['issue_key']}")).json()
            assert det["evidence"]["http_status"] == 404 and det["evidence"]["subject_url"].endswith("/missing")
            assert "404" in det["recommended_action"] and det["status"] == "OPEN"
            assert (await c.get(f"/api/on-page/audits/{jid}/issues/overview")).json()["total"] > 0   # not shadowed
            ud = (await c.get(f"/api/on-page/audits/{jid}/url-detail", params={"url": site.base + "/p/ok"})).json()
            assert ud["http"]["http_status"] == 200 and ud["canonical"]["self_referencing"] is True
            assert ud["metadata"]["title"].startswith("Home page") and ud["headings"]["h1_count"] == 1
            assert ud["structured_data"]["types"].startswith("Organization") and len(ud["hreflang"]) == 3
            assert ud["images"]["missing_alt"] >= 1 and any(t["url_type"] == "SCRIPT" for t in ud["links"]["by_type"])
            assert ud["issues"] and all(i["source_page_url"] == site.base + "/p/ok" for i in ud["issues"])
            red = (await c.get(f"/api/on-page/audits/{jid}/url-detail", params={"url": site.base + "/p/redirected"})).json()
            assert red["redirect"]["count"] == 1 and red["redirect"]["chain"]
            assert (await c.get(f"/api/on-page/audits/{jid}/url-detail", params={"url": "http://nope/"})).status_code == 404

            # lifecycle of one issue: mark /missing IN_PROGRESS and the 403 issue RESOLVED
            assert (await c.patch(f"/api/on-page/audits/{jid}/issues/{missing[0]['issue_key']}/status",
                                  json={"status": "IN_PROGRESS", "note": "ticket SEO-1"})).status_code == 200
            ad = (await c.get(f"/api/on-page/audits/{jid}/issues", params={"issue_type": "ACCESS_DENIED"})).json()["items"][0]
            assert (await c.patch(f"/api/on-page/audits/{jid}/issues/{ad['issue_key']}/status",
                                  json={"status": "RESOLVED"})).status_code == 200
            assert (await c.patch(f"/api/on-page/audits/{jid}/issues/{ad['issue_key']}/status",
                                  json={"status": "BOGUS"})).status_code == 400
            assert (await c.patch(f"/api/on-page/audits/{jid}/issues/zzzz/status", json={"status": "OPEN"})).status_code == 400

            # remediation from on-page issues (existing module + endpoints)
            g = (await c.post(f"/api/jobs/{jid}/remediations/generate")).json()
            assert g["generated_count"] > 0
            rems = (await c.get(f"/api/jobs/{jid}/remediations", params={"page_size": 500, "category": "ONPAGE_NOT_FOUND"})).json()
            items = rems["records"]
            nf_rem = [x for x in items if x["category"] == "ONPAGE_NOT_FOUND" and x["target_url"].endswith("/missing")]
            assert nf_rem and "404" in nf_rem[0]["recommended_action"]
            return jid, zbytes, ad["issue_key"], missing[0]["issue_key"]

    jid, zbytes, ad_key, missing_key = asyncio.run(go())
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    names = sorted(zf.namelist())
    assert len(names) == 18 and names[0] == "00_manifest.csv"
    man = {r["filename"]: r for r in _rows(zf, "00_manifest.csv")}
    assert set(man) == set(names) - {"00_manifest.csv"} and all(r["status"] == "OK" for r in man.values())
    import hashlib
    for n, r in man.items():
        assert hashlib.sha256(zf.read(n)).hexdigest() == r["sha256"] and int(r["row_count"]) == len(_rows(zf, n))
    recon = _rows(zf, "11_reconciliation.csv")
    assert all(r["status"] == "PASS" for r in recon), [r for r in recon if r["status"] != "PASS"]

    # input accounting: 14 lines -> 10 pages + 4 exceptions, none silently dropped
    exc = {r["input_url"]: r["reason"] for r in _rows(zf, "17_input_exceptions.csv")}
    assert sorted(exc.values()) == ["EXACT_DUPLICATE", "INVALID_URL", "INVALID_URL", "NORMALIZED_DUPLICATE"]
    summary = {r["metric"]: r["value"] for r in _rows(zf, "01_summary.csv")}
    assert summary["input_urls_total"] == "14" and summary["source_pages_total"] == "10"

    # URL audit: one row per page with fine-grained classes and SEO fields
    src = {r["source_page_url"].replace(b, ""): r for r in _rows(zf, "02_source_page_audit.csv")}
    assert src["/p/gone"]["status_class"] == "410" and src["/p/redirected"]["status_class"] == "301"
    assert src["/p/boom"]["status_class"] == "500" and src["/p/ok"]["status_class"] == "200"
    assert src["/p/ok"]["canonical_url"] == b + "/p/ok" and src["/p/ok"]["canonical_status"] == "200"
    assert src["/p/noindex"]["indexable"] == "0" and src["/p/dup1"]["h1_count"] == "0"

    # link audit: every URL category, classified
    links = _rows(zf, "03_url_audit.csv")
    ok_links = {r["normalized_url"].replace(b, ""): r for r in links if r["source_page_url"] == b + "/p/ok"}
    cls = {u: ok_links[u]["status_class"] for u in ("/r301", "/r302", "/missing", "/forbidden", "/boom", "/slow",
                                                     "/private/x", "/p/target", "/static/app.js", "/static/site.css")}
    assert cls == {"/r301": "301", "/r302": "302", "/missing": "404", "/forbidden": "403", "/boom": "500",
                   "/slow": "timeout", "/private/x": "robots_blocked", "/p/target": "200", "/static/app.js": "200",
                   "/static/site.css": "404"}
    assert ok_links["/search"]["validation_source"] == "NOT_REQUESTED" and ok_links["/search"]["url_category"] == "FORM"
    assert ok_links["/static/app.js"]["url_category"] == "RESOURCE" and ok_links["/p/ok"]["element_type"] in ("CANONICAL", "HREFLANG")
    ext = [r for r in links if r["internal_external"] == "EXTERNAL"]
    assert ext and ext[0]["status_class"] == "200"
    assert ok_links["/p/target"]["occurrence_count"] == "3"   # 2 anchors + JSON-LD url -> ONE relationship
    assert all(r["business"] == r["business"] for r in links)          # columns present for every row
    assert site.hits["/search"] == 0                                   # form action never requested
    for p in SITEMAP_PATHS:
        assert site.hits[p] == 1 or p == "/p/target", (p, site.hits[p])   # pages fetched once (target is also a redirect hop)
    assert site.hits["/missing"] == 1 and site.hits["/static/app.js"] == 1

    # image audit: srcset, lazy, picture, CSS background, JSON-LD logo, missing alt, broken image
    imgs = {r["normalized_image_url"].replace(b, ""): r for r in _rows(zf, "05_image_audit.csv")
            if r["source_page_url"] == b + "/p/ok"}
    assert {"/img/a.png", "/img/a-2x.png", "/img/lazy.png", "/img/missing.png", "/img/pic.webp", "/img/bg.png",
            "/img/logo.png"} <= set(imgs)
    assert imgs["/img/lazy.png"]["lazy_loaded"] == "true" and imgs["/img/a.png"]["image_format"] == "png"
    assert imgs["/img/missing.png"]["status_class"] == "404"

    # issues: deterministic, deduplicated, one row per (page, rule, subject)
    issues = _rows(zf, "12_issues.csv")
    keys = [r["issue_key"] for r in issues]
    assert len(keys) == len(set(keys))
    types = Counter(r["issue_type"] for r in issues)
    for t in ("NOT_FOUND", "ACCESS_DENIED", "SERVER_ERROR", "TIMEOUT", "ROBOTS_BLOCKED", "REDIRECT_CHAIN", "REDIRECTED",
              "MISSING_ALT", "IMAGE_NOT_FOUND", "MISSING_TITLE", "DUPLICATE_TITLE", "H1_MISSING", "NOINDEX_IN_SITEMAP",
              "SITEMAP_URL_REDIRECTS", "SOURCE_SERVER_ERROR", "HREFLANG_NOT_RECIPROCAL"):
        assert types[t] >= 1, (t, sorted(types))
    css_issue = [r for r in issues if r["issue_type"] == "NOT_FOUND" and r["normalized_url"].endswith("/site.css")]
    assert css_issue and css_issue[0]["category"] == "RESOURCE"

    # rebuilding the package is idempotent: same issues, no duplicates, same score
    from qcv5.reports.on_page_csv_packager import build_on_page_csv_package
    from qcv5.audit.issue_store import compute_score
    db_path = srv.onpage.db_path
    out = os.path.join(env["work"], "rebuild")
    p1 = build_on_page_csv_package(db_path, jid, out, package_name="R1", project_id="127.0.0.1")
    p2 = build_on_page_csv_package(db_path, jid, out, package_name="R2", project_id="127.0.0.1")
    assert p1["final_qc_status"] == p2["final_qc_status"] == "PASS"
    assert p1["files"]["12_issues.csv"] == p2["files"]["12_issues.csv"] == len(issues)
    conn = sqlite3.connect(db_path)
    s1 = compute_score(conn, jid)
    s2 = compute_score(conn, jid)
    assert (s1["score"], s1["technical"], s1["links"]) == (s2["score"], s2["technical"], s2["links"]) == \
        (p1["score"]["score"], p1["score"]["technical"], p1["score"]["links"])
    conn.close()
    env["first_job"] = (jid, ad_key, missing_key)


def test_second_audit_verifies_fix_and_reopens_regression(env):
    srv, site = env["server"], env["site"]
    jid1, ad_key, missing_key = env["first_job"]
    site.fixed = True        # /missing now returns 200; /forbidden still 403

    async def go():
        async with _client(srv) as c:
            r = await c.post("/api/on-page/audits", json={"name": "Fixture re-audit", "urls": [site.base + p for p in SITEMAP_PATHS],
                                                          "preset": "conservative", "overrides": FAST,
                                                          "primary_domain": "127.0.0.1"})
            jid2 = r.json()["job_id"]
            d = await _wait(c, jid2)
            assert d["final_qc_status"] == "PASS"
            all_items = (await c.get(f"/api/on-page/audits/{jid2}/issues", params={"status": "OPEN", "limit": 500})).json()["items"]
            reopened = [i for i in all_items if i["issue_key"] == ad_key]
            assert reopened and "Regression" in reopened[0]["status_note"]
            # the fixed issue is not present in the new audit; its project-level status is VERIFIED
            conn = sqlite3.connect(srv.onpage.db_path)
            st = conn.execute("SELECT status, note FROM on_page_issue_status WHERE issue_key = ?", (missing_key,)).fetchone()
            conn.close()
            assert st[0] == "VERIFIED" and jid2 in st[1]
            summary = (await c.get(f"/api/on-page/audits/{jid2}/files/01_summary.csv")).text
            lines = {l.split(",")[0]: l.split(",")[1] for l in summary.splitlines()[1:] if "," in l}
            assert (lines["issues_verified_fixed"], lines["issues_reopened_regressions"]) == ("1", "1"), \
                {k: v for k, v in lines.items() if k.startswith("issue")}
    asyncio.run(go())


def test_pause_resume_cancel_and_crash_recovery(env):
    srv = env["server"]
    slow = FixtureSite(slow_pages=0.4)
    paths = [f"/p/target?i={i}" for i in range(40)]
    try:
        async def go():
            async with _client(srv) as c:
                body = {"name": "Lifecycle", "urls": [slow.base + p for p in paths], "preset": "conservative",
                        "overrides": {"target_rps": 20, "max_rps": 20, "source_concurrency": 2, "read_timeout": 3,
                                      "max_retries": 0}, "primary_domain": "127.0.0.1"}
                jid = (await c.post("/api/on-page/audits", json=body)).json()["job_id"]
                await asyncio.sleep(2.0)
                assert (await c.post(f"/api/on-page/audits/{jid}/pause")).json()["lifecycle"] in ("PAUSING", "PAUSED")
                d = await _wait(c, jid, done=("PAUSED",))
                done1 = d["progress"]["processed"]
                assert 0 < done1 < 40
                assert (await c.post(f"/api/on-page/audits/{jid}/resume")).json()["lifecycle"] in ("RESUMED", "RUNNING")
                await asyncio.sleep(1.5)
                # crash: kill the worker without any chance to clean up
                pid = int(open(os.path.join(srv.onpage._dir(jid), "worker.pid")).read())
                os.kill(pid, signal.SIGKILL)      # the API keeps its process handle and reaps the dead worker
                d = await _wait(c, jid, done=("INTERRUPTED",), timeout=30)
                done2 = d["progress"]["processed"]
                assert done2 >= done1
                await c.post(f"/api/on-page/audits/{jid}/resume")
                d = await _wait(c, jid)
                assert d["lifecycle"] == "COMPLETED" and d["final_qc_status"] == "PASS", d.get("log_tail")
                assert d["progress"]["processed"] == 40
                # no page fetched more than twice (at most re-fetched once if it was in flight at the crash)
                assert max(slow.hits[p.split("?")[0]] for p in paths) >= 1
                conn = sqlite3.connect(srv.onpage.db_path)
                dup = conn.execute("SELECT COUNT(*) - COUNT(DISTINCT source_page_url) FROM on_page_source_pages WHERE job_id = ?",
                                   (jid,)).fetchone()[0]
                conn.close()
                assert dup == 0

                # cancel: stops for good, completed work kept, cannot resume
                jid2 = (await c.post("/api/on-page/audits", json={**body, "name": "Cancel me"})).json()["job_id"]
                await asyncio.sleep(1.5)
                await c.post(f"/api/on-page/audits/{jid2}/cancel")
                d = await _wait(c, jid2, done=("CANCELLED",))
                assert [e["state"] for e in d["events"]][-2:] == ["CANCELLING", "CANCELLED"]
                assert (await c.post(f"/api/on-page/audits/{jid2}/resume")).status_code == 409
                prog = (await c.get(f"/api/on-page/audits/{jid2}/progress")).json()
                assert prog["lifecycle"] == "CANCELLED" and prog["processed"] > 0
        asyncio.run(go())
    finally:
        slow.close()
