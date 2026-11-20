"""
SEO / technical-SEO deep dive + sitemap configuration.

Unit level: single-pass parser signals, page rules, hreflang reciprocity, lastmod hygiene, robots.txt
sitemap discovery, selection filters, profiles.
End to end (real FastAPI app + audit worker + local site): domain -> robots.txt -> sitemap index ->
urlsets (one gzipped) -> filtered selection -> audit -> 15_seo_page_audit / 16_sitemap_audit /
SEO issues in 12-14 -> trust gate PASS -> saved profile re-run.
"""
import asyncio
import csv
import gzip
import io
import os
import shutil
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from qcv5.crawler.html_parser import Parser
from qcv5.audit.seo_audit import hreflang_reciprocity, lastmod_state, page_facts, page_issues, url_structure_issues
from qcv5.crawler.sitemap_config import SitemapConfigStore, _normalize_input, parse_robots_sitemaps

WORDS = " ".join(f"word{i}" for i in range(260))


def _page(title, desc="", h1="Heading", body=WORDS, head_extra="", body_extra="", lang="en", viewport=True):
    return (f"<!doctype html><html{' lang=' + chr(34) + lang + chr(34) if lang else ''}><head><title>{title}</title>"
            + (f'<meta name="description" content="{desc}">' if desc else "")
            + ('<meta name="viewport" content="width=device-width">' if viewport else "")
            + head_extra + f"</head><body><h1>{h1}</h1><p>{body}</p>{body_extra}</body></html>")


GOOD_TITLE = "A perfectly sized page title for testing"            # 40 chars
GOOD_DESC = "A meta description that is long enough to pass the seventy character minimum check."
OG = ('<meta property="og:title" content="t"><meta property="og:description" content="d">'
      '<meta property="og:image" content="/img.png">'
      '<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>')


def _site_pages(base):
    nav = ('<a href="/about">About</a><a href="/dup-a">A</a><a href="/dup-b">B</a><a href="/en">EN</a>'
           '<a href="/fr">FR</a><a href="/hidden">Hidden</a><a href="/noindex">N</a><a href="/xrobots">X</a>'
           '<a href="/bad-schema">S</a><a href="/Upper_Case">U</a><a href="/blog/1">B1</a><a href="/thin">T</a>'
           '<a href="/">Home</a>')
    canon = lambda p: f'<link rel="canonical" href="{base}{p}">'
    return {
        "/": _page(GOOD_TITLE, GOOD_DESC, head_extra=OG + canon("/"), body_extra=nav),
        "/about": _page("About us — " + GOOD_TITLE, GOOD_DESC, head_extra=OG + canon("/about"), body_extra=nav),
        "/dup-a": _page("Duplicate title shared by two pages ok", "Same description " * 5, head_extra=OG + canon("/dup-a"),
                        body_extra=nav),
        "/dup-b": _page("Duplicate title shared by two pages ok", "Same description " * 5, head_extra=OG + canon("/dup-b"),
                        body_extra=nav),
        "/noindex": _page(GOOD_TITLE + " n", GOOD_DESC, head_extra=OG + canon("/noindex") +
                          '<meta name="robots" content="noindex,follow">', body_extra=nav),
        "/xrobots": _page(GOOD_TITLE + " x", GOOD_DESC, head_extra=OG + canon("/xrobots"), body_extra=nav),
        "/bad-schema": _page(GOOD_TITLE + " s", GOOD_DESC, head_extra=canon("/bad-schema") +
                             '<script type="application/ld+json">{"@type": "Product",}</script>', body_extra=nav),
        "/Upper_Case": _page(GOOD_TITLE + " u", GOOD_DESC, head_extra=OG + canon("/Upper_Case"), body_extra=nav),
        "/en": _page(GOOD_TITLE + " en", GOOD_DESC, head_extra=OG + canon("/en") +
                     f'<link rel="alternate" hreflang="en" href="{base}/en">'
                     f'<link rel="alternate" hreflang="fr" href="{base}/fr">'
                     f'<link rel="alternate" hreflang="x-default" href="{base}/en">', body_extra=nav),
        "/fr": _page(GOOD_TITLE + " fr", GOOD_DESC, head_extra=OG + canon("/fr") +
                     f'<link rel="alternate" hreflang="fr" href="{base}/fr">', body_extra=nav, lang="fr"),
        "/thin": _page("Short", "", h1="", body="tiny page", head_extra=canon("/thin"), body_extra=nav,
                       lang="", viewport=False),
        "/blog/1": _page(GOOD_TITLE + " b1", GOOD_DESC, head_extra=OG + canon("/blog/1"), body_extra=nav),
        "/blog/2": _page(GOOD_TITLE + " b2", GOOD_DESC, head_extra=OG + canon("/blog/2")),   # nobody links here
        "/hidden": _page(GOOD_TITLE + " h", GOOD_DESC, head_extra=OG + canon("/hidden"), body_extra=nav),
        "/private/x": _page("private"),
    }


class Site:
    def __init__(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                b = site.base
                if path == "/robots.txt":
                    return self._send(200, f"User-agent: *\nDisallow: /private/\nSitemap: {b}/sitemap_index.xml\n",
                                      "text/plain")
                if path == "/sitemap_index.xml":
                    return self._send(200, f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                           f"<sitemap><loc>{b}/sm-main.xml</loc></sitemap>"
                                           f"<sitemap><loc>{b}/sm-blog.xml.gz</loc></sitemap>"
                                           f"<sitemap><loc>{b}/sm-missing.xml</loc></sitemap></sitemapindex>",
                                      "application/xml")
                if path == "/sm-main.xml":
                    def u(p, lm=None):
                        return f"<url><loc>{b}{p}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm is not None else "") + "</url>"
                    xml = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                           + u("/", "2026-01-02") + u("/about", "2026-01-02T10:00:00+00:00") + u("/dup-a", "2026-01-02")
                           + u("/dup-b", "19/11/2026") + u("/noindex", "2099-01-01") + u("/xrobots", "2026-01-02")
                           + u("/bad-schema") + u("/Upper_Case", "2026-01-02") + u("/en", "2026-01-02")
                           + u("/fr", "2026-01-02") + u("/thin", "2026-01-02") + u("/private/x", "2026-01-02")
                           + u("/about", "2026-01-02") + "</urlset>")
                    return self._send(200, xml, "application/xml")
                if path == "/sm-blog.xml.gz":
                    xml = (f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                           f"<url><loc>{b}/blog/1</loc><lastmod>2026-01-02</lastmod></url>"
                           f"<url><loc>{b}/blog/2</loc><lastmod>2026-01-02</lastmod></url>"
                           f"<url><loc>{b}/about</loc></url></urlset>")
                    return self._send(200, gzip.compress(xml.encode()), "application/x-gzip")
                pages = _site_pages(b)
                if path in pages:
                    extra = {"X-Robots-Tag": "noindex"} if path == "/xrobots" else {}
                    return self._send(200, pages[path], "text/html; charset=utf-8", extra)
                if path == "/img.png":
                    return self._send(200, b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")
                return self._send(404, "<html><head><title>Not found</title></head><body>nf</body></html>", "text/html")

            def _send(self, code, body, ctype, extra=None):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


# ---------------------------------------------------------------------------------------------- unit tests
def test_parser_collects_seo_signals_in_the_same_pass():
    html = ('<html lang="en-GB"><head><title>T1</title><title>T2</title><meta name="description" content="d1">'
            '<meta name="description" content="d2"><meta name="viewport" content="w"><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="0;url=/x"><meta name="googlebot" content="nofollow">'
            '<script type="application/ld+json">{"@graph":[{"@type":"Organization"},{"@type":["FAQPage"]}]}</script>'
            '<link rel="stylesheet" href="http://cdn.example.com/a.css"></head><body>'
            '<h1>One <em>two</em></h1><h2>x</h2><h4>skip</h4><script>var a = "no words here";</script>'
            '<style>.a{}</style><p>Alpha beta gamma</p><link rel="canonical" href="/c"></body></html>')
    p = Parser("https://ex.com/p")
    p.feed(html)
    assert (p.title_count, p.meta_desc_count, p.meta_desc) == (2, 2, "d1")
    assert p.html_lang == "en-GB" and p.has_viewport and p.has_charset and p.meta_refresh == "0;url=/x"
    assert p.h1s == ["One two"] and p.heading_skips == ["h2->h4"] and p.canonical_outside_head == 1
    assert p.word_count == 7            # "One two" + "x" + "skip" + "Alpha beta gamma"; script/style excluded
    assert p.insecure_resources == ["stylesheet:http://cdn.example.com/a.css"]
    f = page_facts("j", "https://ex.com/p", "https://ex.com/p", p, [], "", "now")
    assert f["nofollow"] == 1 and f["noindex"] == 0 and f["schema_types"] == "Organization,FAQPage"
    types = {t for _, t, _, _ in page_issues(f, {"https://ex.com/p"})}
    assert {"MULTIPLE_TITLE_TAGS", "MULTIPLE_META_DESCRIPTIONS", "META_REFRESH", "PAGE_NOFOLLOW",
            "CANONICAL_OUTSIDE_HEAD", "MIXED_CONTENT", "HEADING_LEVEL_SKIPPED", "THIN_CONTENT"} <= types
    assert "VIEWPORT_MISSING" not in types and "HTML_LANG_MISSING" not in types


def test_robots_header_tokens_and_content_hash():
    a, b = Parser("https://ex.com/a"), Parser("https://ex.com/b")
    a.feed("<html><body><p>Same   TEXT here</p></body></html>")
    b.feed("<html><body><div>same text</div> <span>HERE</span></body></html>")
    assert a.content_hash and a.content_hash == b.content_hash
    f = page_facts("j", "https://ex.com/a", "https://ex.com/a", a, [], "googlebot: noindex, nofollow", "now")
    assert f["noindex"] == 1 and f["nofollow"] == 1 and f["indexable"] == 0
    det = [d for _, t, _, d in page_issues(f, set()) if t == "NOINDEX_IN_SITEMAP"]
    assert det == ["Sitemap page is noindex (X-Robots-Tag header)"]


def test_hreflang_rules_and_reciprocity():
    import json
    hl = json.dumps([["en", "https://ex.com/en"], ["en-UK", "https://ex.com/uk"], ["fr", "https://ex.com/fr"],
                     ["fr", "https://ex.com/fr2"], ["english", "https://ex.com/x"]])
    f = {"hreflang_json": hl, "hreflang_x_default": 0, "title_length": 40, "meta_description_length": 100,
         "h1_count": 1, "h1_text": "h", "has_viewport": 1, "html_lang": "en", "word_count": 500,
         "og_title": "t", "og_description": "d", "og_image": "i", "jsonld_blocks": 1}
    got = {t: d for _, t, _, d in page_issues(f, {"https://ex.com/en"})}
    assert "en-UK" in got["HREFLANG_INVALID_CODE"] and "english" in got["HREFLANG_INVALID_CODE"]
    assert "HREFLANG_MISSING_X_DEFAULT" in got and "fr" in got["HREFLANG_CONFLICT"]
    assert "HREFLANG_MISSING_SELF_REFERENCE" not in got
    rows = [("A", "https://ex.com/en", "", json.dumps([["en", "https://ex.com/en"], ["fr", "https://ex.com/fr"],
                                                       ["de", "https://ex.com/de-not-audited"]])),
            ("B", "https://ex.com/fr", "", json.dumps([["fr", "https://ex.com/fr"]])),
            ("C", "https://ex.com/es", "", json.dumps([["es", "https://ex.com/es"], ["en", "https://ex.com/en"]]))]
    # A->B not returned; C->A not returned (A lists only en/fr/de); targets outside the audit are not judged
    assert sorted(hreflang_reciprocity(rows)) == [("A", "https://ex.com/fr", "fr"), ("C", "https://ex.com/en", "en")]


def test_lastmod_and_url_structure():
    now = "2026-11-19T10:00:00Z"
    assert lastmod_state("", now) == "MISSING"
    assert lastmod_state("19/11/2026", now) == "INVALID"
    assert lastmod_state("2026-11-19", now) == "" and lastmod_state("2026-11-20", now) == ""   # 1-day tolerance
    assert lastmod_state("2026-11-21T00:00:00+05:30", now) == "FUTURE"
    assert lastmod_state("2026-11-19T09:00:00.123Z", now) == ""
    t = {i[1] for i in url_structure_issues("https://ex.com/Some_Path//x?utm=1")}
    assert t == {"URL_HAS_UPPERCASE", "URL_HAS_UNDERSCORES", "URL_DOUBLE_SLASH", "SITEMAP_URL_HAS_PARAMETERS"}
    assert url_structure_issues("https://ex.com/clean-path/") == []


def test_input_normalization_and_robots_parsing():
    assert _normalize_input("example.com") == ("site", "https://example.com/")
    assert _normalize_input("https://example.com/robots.txt")[0] == "robots"
    assert _normalize_input("https://example.com/sitemap.xml")[0] == "sitemap"
    with pytest.raises(ValueError):
        _normalize_input("ftp://example.com/x")
    txt = "User-agent: *\nSitemap: https://a/s1.xml # main\nsitemap:https://a/s2.xml\nSitemap: https://a/s1.xml\n"
    assert parse_robots_sitemaps(txt) == ["https://a/s1.xml", "https://a/s2.xml"]


def test_selection_filters_and_profiles(tmp_path):
    store = SitemapConfigStore(str(tmp_path))
    snap = {"import_id": "smi_000000000000", "sources": [
        {"sitemap_url": "S1", "kind": "URLSET"}, {"sitemap_url": "S2", "kind": "URLSET"}],
        "urls": [{"u": f"https://ex.com/{p}", "sm": sm, "lastmod": "", "changefreq": "", "priority": None,
                  "robots": p != "blocked"}
                 for p, sm in (("a", "S1"), ("blog/1", "S1"), ("blog/2", "S2"), ("tag/x", "S1"), ("blocked", "S1"),
                               ("c", "S1"))]}
    sel = store.select(snap, selected_sitemaps=["S1"], include=["/blog/*", "a"], exclude=["tag"], max_pages=0)
    assert [r["u"] for r in sel["urls"]] == ["https://ex.com/a", "https://ex.com/blog/1"]
    assert sel["excluded"] == {"sitemap": 1, "robots": 1, "include": 1, "exclude": 1, "cap": 0}
    capped = store.select(snap, max_pages=2)
    assert capped["selected_count"] == 2 and capped["excluded"]["cap"] == 3   # 6 URLs - 1 robots-blocked - 2 kept
    with pytest.raises(ValueError):
        store.select(snap, selected_sitemaps=["NOPE"])
    meta = store.audit_meta(snap, sel)
    assert [s["selected"] for s in meta["sources"]] == [1, 0] and len(meta["entries"]) == 2

    p = store.save_profile({"name": "Weekly", "inputs": ["ex.com"], "include": "/blog/\n/a", "max_pages": 50})
    assert p["include"] == ["/blog/", "/a"] and store.get_profile(p["id"])["max_pages"] == 50
    store.save_profile({"name": "Weekly v2", "inputs": ["ex.com"]}, p["id"])
    assert [x["name"] for x in store.list_profiles()] == ["Weekly v2"]
    with pytest.raises(ValueError):
        store.save_profile({"name": "x", "inputs": []})
    store.delete_profile(p["id"])
    assert store.list_profiles() == []


# ---------------------------------------------------------------------------------------------- end to end
@pytest.fixture(scope="module")
def env():
    site = Site()
    work = tempfile.mkdtemp(prefix="qc_seo_")
    import qcv5.api.server as server
    from qcv5.core.db import Database
    from qcv5.services.onpage_service import OnPageAuditService
    saved = (server.db, server.onpage)
    server.db = Database(os.path.join(work, "api.db"))
    asyncio.run(server.db.connect())
    server.onpage = OnPageAuditService(server.db.db_path, base_dir=os.path.join(work, "jobs"),
                                       reports_dir=os.path.join(work, "reports"), allow_local=True)
    yield {"server": server, "base": site.base, "work": work}
    asyncio.run(server.db.close())
    server.db, server.onpage = saved
    site.close()
    shutil.rmtree(work, ignore_errors=True)


async def _wait(c, job_id, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        d = (await c.get(f"/api/on-page/audits/{job_id}")).json()
        if d["run_state"] not in ("RUNNING", "QUEUED", "STOPPING"):
            return d
        await asyncio.sleep(0.4)
    raise AssertionError(f"audit did not finish: {d.get('log_tail')}")


def _csv(zf, name):
    return list(csv.DictReader(io.StringIO(zf.read(name).decode("utf-8"))))


def test_sitemap_config_to_seo_reports_end_to_end(env):
    srv, base = env["server"], env["base"]

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=srv.app), base_url="http://t", timeout=120) as c:
            # 1. discover from the bare host: robots.txt -> index -> 2 urlsets (one gzipped) + 1 missing sitemap
            r = await c.post("/api/on-page/sitemaps/discover", json={"inputs": [base]})
            assert r.status_code == 200, r.text
            imp = r.json()
            assert imp["discovery"][0]["method"] == "robots.txt"
            by = {s["sitemap_url"]: s for s in imp["sources"]}
            assert by[f"{base}/sitemap_index.xml"]["kind"] == "INDEX"
            main, blog, missing = by[f"{base}/sm-main.xml"], by[f"{base}/sm-blog.xml.gz"], by[f"{base}/sm-missing.xml"]
            assert main["urls_listed"] == 13 and main["duplicates"] == 1 and main["robots_blocked"] == 1
            assert (main["lastmod_missing"], main["lastmod_invalid"], main["lastmod_future"]) == (1, 1, 1)
            assert blog["unique_urls"] == 2 and blog["duplicates"] == 1         # /about already in sm-main
            assert missing["status"] == "FAILED" and missing["http_status"] == 404
            assert imp["summary"]["unique_urls"] == 14 and imp["summary"]["robots_blocked"] == 1

            # 2. preview a selection: exclude /thin, robots-blocked dropped by default
            pv = (await c.post(f"/api/on-page/sitemaps/{imp['import_id']}/preview",
                               json={"import_id": imp["import_id"], "exclude": ["/thin"]})).json()
            assert pv["selected_count"] == 12 and pv["excluded"]["robots"] == 1 and pv["excluded"]["exclude"] == 1
            assert "urls" not in pv and pv["estimate"]["conservative"]["estimated_minutes"] > 0
            bad = await c.post(f"/api/on-page/sitemaps/{imp['import_id']}/preview",
                               json={"import_id": imp["import_id"], "selected_sitemaps": ["https://evil/x.xml"]})
            assert bad.status_code == 400
            assert (await c.get("/api/on-page/sitemaps/smi_ffffffffffff")).status_code == 404
            assert (await c.get("/api/on-page/sitemaps/..%2Fetc")).status_code in (400, 404)

            # 3. launch from the import (all sitemaps, no filters) and wait
            r = await c.post("/api/on-page/audits", json={
                "name": "SEO e2e", "preset": "conservative", "primary_domain": "127.0.0.1",
                "overrides": {"target_rps": 40, "max_rps": 40},
                "sitemap": {"import_id": imp["import_id"]}})
            assert r.status_code == 200, r.text
            job = r.json()
            assert job["url_count"] == 13 and job["source"]["type"] == "sitemap"
            d = await _wait(c, job["job_id"])
            assert d["run_state"] == "COMPLETED", d.get("log_tail")
            assert d["final_qc_status"] == "PASS", d.get("log_tail")

            zbytes = (await c.get(f"/api/on-page/audits/{job['job_id']}/package")).content
            seo = (await c.get(f"/api/on-page/audits/{job['job_id']}/seo")).json()
            return job, zbytes, seo

    job, zbytes, seo = asyncio.run(go())
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    recon = _csv(zf, "11_reconciliation.csv")
    assert all(r["status"] == "PASS" for r in recon), [r for r in recon if r["status"] != "PASS"]
    names = {r["check"] for r in recon}
    assert {"seo_csv_rows_eq_db_seo_pages", "every_parsed_2xx_html_page_has_seo_row",
            "sitemap_csv_pages_eq_source_pages"} <= names

    seo_rows = {r["source_page_url"].replace(base, ""): r for r in _csv(zf, "15_seo_page_audit.csv")}
    assert len(seo_rows) == 13
    assert seo_rows["/noindex"]["noindex"] == "1" and seo_rows["/xrobots"]["x_robots_tag"] == "noindex"
    assert seo_rows["/"]["schema_types"] == "WebPage" and seo_rows["/"]["word_count"] == "274"   # h1 + 260 body words + 13 nav labels

    issues = _csv(zf, "12_issues.csv")
    found = {}
    for r in issues:
        found.setdefault(r["issue_type"], set()).add(r["source_page_url"].replace(base, "") or r["found_url"])

    def has(t, *pages):
        assert t in found, (t, sorted(found))
        for p in pages:
            assert p in found[t], (t, p, found[t])

    has("NOINDEX_IN_SITEMAP", "/noindex", "/xrobots")
    has("DUPLICATE_TITLE", "/dup-a", "/dup-b")
    has("DUPLICATE_META_DESCRIPTION", "/dup-a", "/dup-b")
    has("STRUCTURED_DATA_INVALID", "/bad-schema")
    has("URL_HAS_UPPERCASE", "/Upper_Case")
    has("URL_HAS_UNDERSCORES", "/Upper_Case")
    has("HREFLANG_NOT_RECIPROCAL", "/en")
    has("HREFLANG_MISSING_X_DEFAULT", "/fr")
    has("THIN_CONTENT", "/thin")
    has("TITLE_TOO_SHORT", "/thin")
    has("META_DESCRIPTION_MISSING", "/thin")
    has("H1_EMPTY", "/thin")
    has("VIEWPORT_MISSING", "/thin")
    has("HTML_LANG_MISSING", "/thin")
    has("ORPHAN_PAGE", "/blog/2")
    has("SITEMAP_FETCH_FAILED", "/sm-missing.xml")
    has("SITEMAP_DUPLICATE_URLS", "/sm-main.xml", "/sm-blog.xml.gz")
    has("SITEMAP_LASTMOD_MISSING", "/sm-main.xml")
    has("SITEMAP_LASTMOD_INVALID", "/sm-main.xml")
    has("SITEMAP_LASTMOD_FUTURE", "/sm-main.xml")
    nis = [r for r in issues if r["issue_type"] == "NOT_IN_SITEMAP"]
    assert [r["found_url"].replace(base, "") for r in nis] == ["/hidden"]
    # dedup pages linked by many others are never orphans; healthy pages raise no SEO content issues
    assert "/about" not in found.get("ORPHAN_PAGE", set()) and "/" not in found.get("THIN_CONTENT", set())
    assert "/dup-a" not in found.get("DUPLICATE_CONTENT", set()) or "/dup-b" in found["DUPLICATE_CONTENT"]
    for t in ("DUPLICATE_TITLE", "NOINDEX_IN_SITEMAP", "ORPHAN_PAGE", "SITEMAP_LASTMOD_INVALID"):
        assert all(r["recommended_action"] != "Review this item." for r in issues if r["issue_type"] == t)

    sm = {r["sitemap_url"].replace(base, ""): r for r in _csv(zf, "16_sitemap_audit.csv")}
    assert sm["/sitemap_index.xml"]["kind"] == "INDEX" and sm["/sm-missing.xml"]["health"] == "FAILED"
    assert sm["/sm-main.xml"]["pages_audited"] == "11" and sm["/sm-blog.xml.gz"]["pages_audited"] == "2"
    assert sm["/sm-main.xml"]["noindex_pages"] == "2" and sm["/sm-main.xml"]["lastmod_invalid"] == "1"
    assert sm["/sm-main.xml"]["health"] == "ISSUES"
    src = _csv(zf, "02_source_page_audit.csv")
    assert {r["source_sitemap"].replace(base, "") for r in src} == {"/sm-main.xml", "/sm-blog.xml.gz"}

    assert seo["pages"] == 13 and 0 < seo["score"] <= 100
    assert seo["counts"]["noindex"] == 2 and seo["coverage"]["indexable"] == round(100 * 11 / 13, 1)
    cats = {c["category"] for c in seo["categories"]}
    assert {"ON_PAGE_SEO", "TECH_SEO", "SITEMAP", "INTERNATIONAL", "URL_STRUCTURE", "CONTENT"} <= cats
    assert len(seo["sitemaps"]) == 4


def test_sitemap_profiles_api_and_rerun(env):
    srv, base = env["server"], env["base"]

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=srv.app), base_url="http://t", timeout=120) as c:
            bad = await c.post("/api/on-page/sitemap-profiles", json={"name": "x", "inputs": []})
            assert bad.status_code == 400
            r = await c.post("/api/on-page/sitemap-profiles", json={
                "name": "Blog weekly", "inputs": [f"{base}/robots.txt"], "include": ["/blog/"],
                "preset": "conservative", "overrides": {"target_rps": 40, "max_rps": 40}})
            assert r.status_code == 200, r.text
            prof = r.json()
            assert (await c.get("/api/on-page/sitemap-profiles")).json()["profiles"][0]["id"] == prof["id"]
            r = await c.put(f"/api/on-page/sitemap-profiles/{prof['id']}",
                            json={**{k: prof[k] for k in ("name", "inputs", "include", "preset", "overrides")},
                                  "max_pages": 1})
            assert r.json()["max_pages"] == 1
            assert (await c.post("/api/on-page/sitemap-profiles/smp_0000000000/run")).status_code == 404
            run = await c.post(f"/api/on-page/sitemap-profiles/{prof['id']}/run")
            assert run.status_code == 200, run.text
            job = run.json()["audit"]
            assert job["url_count"] == 1 and job["source"]["profile_id"] == prof["id"]
            d = await _wait(c, job["job_id"])
            assert d["final_qc_status"] == "PASS", d.get("log_tail")
            again = (await c.get("/api/on-page/sitemap-profiles")).json()["profiles"][0]
            assert again["last_run"]["job_id"] == job["job_id"]
            assert (await c.delete(f"/api/on-page/sitemap-profiles/{prof['id']}")).status_code == 200
            assert (await c.get("/api/on-page/sitemap-profiles")).json()["profiles"] == []

    asyncio.run(go())
