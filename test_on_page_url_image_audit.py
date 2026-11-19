"""
QC V5 on-page URL + IMAGE audit: extraction, three-level dedup (same page / cross page / in flight),
source-result reuse, validation outcomes, crash/restart recovery, streaming CSV + ZIP + trust gate.
Server-side request counts (RecordingServer) are the proof of "validated exactly once".
"""
import asyncio
import csv
import os
import time
import zipfile
from collections import Counter

import httpx

from html_parser import Parser, parse_srcset
from on_page_audit_engine import extract_on_page_images, extract_on_page_urls
from on_page_csv_packager import build_on_page_csv_package
from test_on_page_audit_performance import AKAMAI_403, Ctx, html_resp, make_engine, q, sources_for

ZIP_FILES = sorted([
    "01_summary.csv", "02_source_page_audit.csv", "03_url_audit.csv", "04_unique_url_validation.csv",
    "05_image_audit.csv", "06_unique_image_validation.csv", "07_broken_urls.csv", "08_redirect_urls.csv",
    "09_broken_images.csv", "10_external_urls.csv", "11_reconciliation.csv",
    "12_issues.csv", "13_issue_summary.csv", "14_page_issue_summary.csv", "15_seo_page_audit.csv",
    "16_sitemap_audit.csv", "17_input_exceptions.csv", "00_manifest.csv",
])


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------------------ extraction
def test_url_extraction_forms():
    html = """<html><head>
      <link rel="canonical" href="https://www.site.in/p/1">
      <link rel="alternate" hreflang="hi-IN" href="/hi/p/1">
    </head><body>
      <a href="rel-page">r</a><a href="/root-page">rr</a><a href="https://www.site.in/abs">a</a>
      <a href="//cdn.site.in/proto">p</a><a href="/root-page#frag">dup with fragment</a><a href="/root-page">dup</a>
      <a href="#top">f</a><a href="https://other.com/x">ext</a><a href="mailto:a@b.c">m</a><a href="http://">bad</a>
    </body></html>"""
    raw, rels, _ = extract_on_page_urls("https://www.site.in/dir/page", "https://www.site.in/dir/page", html)
    by = {r["normalized_found_url"]: r for r in rels}
    assert raw == 12
    assert by["https://www.site.in/dir/rel-page"]["url_syntax_type"] == "RELATIVE"
    assert by["https://www.site.in/root-page"]["url_syntax_type"] == "ROOT_RELATIVE"
    assert by["https://www.site.in/root-page"]["occurrence_count"] == 3          # same-page dedup incl. fragment
    assert by["https://www.site.in/abs"]["url_syntax_type"] == "ABSOLUTE"
    assert by["https://cdn.site.in/proto"]["url_syntax_type"] == "PROTOCOL_RELATIVE"
    assert by["https://cdn.site.in/proto"]["found_url_scope"] == "INTERNAL"     # same registrable domain
    assert by["#top"]["found_url_scope"] == "FRAGMENT"
    assert by["https://other.com/x"]["found_url_scope"] == "EXTERNAL"
    assert by["mailto:a@b.c"]["found_url_scope"] == "NON_HTTP"
    assert by["http://"]["found_url_scope"] == "INVALID"
    assert by["https://www.site.in/p/1"]["found_url_type"] == "CANONICAL"
    assert by["https://www.site.in/hi/p/1"]["found_url_type"] == "HREFLANG"


def test_image_extraction_forms():
    html = """<html><head>
      <meta property="og:image" content="/og.jpg"><meta name="twitter:image" content="https://cdn.x.in/tw.png">
    </head><body>
      <img src="/a.png" alt="Product A" width="40" height="30" loading="lazy">
      <img src="/a.png" alt="Product A">
      <img src="/a.png" alt="Different alt">
      <img srcset="/s-400.webp 400w,
                   /s-800.webp 800w, /c_fill,w_1200/s.webp 1200w" src="/s-400.webp">
      <picture><source srcset="/pic-1x.avif 1x, /pic-2x.avif 2x" type="image/avif"><img src="/pic.jpg" alt=""></picture>
      <img data-src="/lazy.jpg" src="data:image/gif;base64,R0lGOD"><img data-lazy-src="/lazy2.jpg">
      <img data-original="/orig.jpg"><img data-image="/dimg.jpg"><img src="http://">
    </body></html>"""
    p = Parser("https://www.x.in/page")
    p.feed(html)
    raw, imgs = extract_on_page_images(p, "www.x.in")
    by = Counter()
    rec = {}
    for i in imgs:
        rec[(i["normalized_image_url"], i["alt_text"], i["alt_present"])] = i
        by[i["normalized_image_url"]] += 1
    a1 = rec[("https://www.x.in/a.png", "Product A", True)]
    assert a1["occurrence_count"] == 2 and a1["width"] == "40" and a1["height"] == "30" and a1["loading"] == "lazy"
    assert ("https://www.x.in/a.png", "Different alt", True) in rec            # alt kept per SEO relationship
    assert by["https://www.x.in/a.png"] == 2                                   # ...but one URL to validate
    s400 = [i for i in imgs if i["normalized_image_url"] == "https://www.x.in/s-400.webp"][0]
    assert set(s400["attribute_sources"]) == {"img.srcset", "img.src"} and s400["occurrence_count"] == 2
    assert "https://www.x.in/s-800.webp" in by and "https://www.x.in/c_fill,w_1200/s.webp" in by  # comma inside URL kept
    srcs = {i["normalized_image_url"]: i["attribute_sources"] for i in imgs}
    assert srcs["https://www.x.in/pic-1x.avif"] == ["source.srcset"] and srcs["https://www.x.in/pic-2x.avif"] == ["source.srcset"]
    assert srcs["https://www.x.in/pic.jpg"] == ["img.src"]
    assert srcs["https://www.x.in/lazy.jpg"] == ["img.data-src"]
    assert srcs["https://www.x.in/lazy2.jpg"] == ["img.data-lazy-src"]
    assert srcs["https://www.x.in/orig.jpg"] == ["img.data-original"]
    assert srcs["https://www.x.in/dimg.jpg"] == ["img.data-image"]
    assert srcs["https://www.x.in/og.jpg"] == ["og:image"] and srcs["https://cdn.x.in/tw.png"] == ["twitter:image"]
    og = [i for i in imgs if i["normalized_image_url"] == "https://www.x.in/og.jpg"][0]
    assert og["alt_present"] is None and og["alt_text"] == ""                  # never fabricated
    types = {i["normalized_image_url"][:10]: i["image_type"] for i in imgs if not i["is_http"]}
    assert "NON_HTTP_IMAGE" in types.values() and "INVALID_IMAGE" in types.values()
    tw = [i for i in imgs if i["normalized_image_url"] == "https://cdn.x.in/tw.png"][0]
    assert tw["image_type"] == "HTTPS_IMAGE" and tw["internal_external"] == "INTERNAL_IMAGE"
    assert parse_srcset("a.jpg 1x,b.jpg 2x") == ["a.jpg", "b.jpg"]


# ------------------------------------------------------------------------------ end to end
N_SRC = 12


def site_handler(path):
    if path == "/robots.txt":
        return 200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private\n", 0.0
    if path.startswith("/p/"):
        i = int(path.split("/")[-1])
        nxt, prv = (i + 1) % N_SRC, (i - 1) % N_SRC
        body = f"""<html><head><link rel="canonical" href="/p/{i}"><meta property="og:image" content="/img/og.jpg"></head><body>
          <img src="/img/logo.webp" alt="Logo {i % 2}"><img src="/img/logo.webp" alt="Logo {i % 2}">
          <img srcset="/img/hero-{i}.webp 400w, /img/hero-{i}-2x.webp 800w" alt="">
          <img data-src="/img/broken.png" alt="broken"><img src="/img/fake.png"><img src="/img/My Photo.webp" alt="space">
          <a href="/product/123">p</a><a href="/product/123">p again</a><a href="/p/{nxt}">next</a><a href="/p/{prv}">prev</a>
          <a href="/r301">a</a><a href="/r302">b</a><a href="/nf">c</a><a href="/gone">d</a><a href="/err">e</a>
          <a href="/slow">f</a><a href="/private/x">g</a><a href="/waf">h</a><a href="https://bad host.example/x">bad</a>
          <a href="mailto:x@y.z">m</a><a href="https://ext.example.invalid/x">ext</a>
        </body></html>"""
        return html_resp(body)
    if path == "/product/123":
        return html_resp("<html>ok</html>", delay=0.6)                 # slow -> concurrent discoveries overlap
    if path == "/img/logo.webp":
        return 200, {"Content-Type": "image/webp"}, b"RIFF....WEBP", 0.6
    if path.startswith("/img/hero") or path in ("/img/og.jpg", "/img/My%20Photo.webp"):
        return 200, {"Content-Type": "image/webp"}, b"x" * 100, 0.0
    if path == "/img/broken.png":
        return html_resp("missing", 404)
    if path == "/img/fake.png":
        return html_resp("<html>not an image</html>", 200)
    if path == "/r301":
        return 301, {"Location": "/product/123"}, b"", 0.0
    if path == "/r302":
        return 302, {"Location": "/nf"}, b"", 0.0
    if path == "/nf":
        return html_resp("nf", 404)
    if path == "/gone":
        return html_resp("gone", 410)
    if path == "/err":
        return html_resp("err", 500)
    if path == "/slow":
        return html_resp("late", delay=2.5)
    if path == "/waf":
        return 403, {"Content-Type": "text/html", "Server": "AkamaiGHost"}, AKAMAI_403, 0.0
    return html_resp("nf", 404)


def _engine(ctx, job="E2E", **kw):
    params = dict(source_concurrency=N_SRC, dest_concurrency=8, image_concurrency=8, read_timeout=1.0,
                  primary_hosts=["127.0.0.1"], target_rps=200, min_rps=100, max_rps=200, waf_cooldown_threshold=1000)
    params.update(kw)
    return make_engine(ctx.db, job, **params)


def test_end_to_end_url_image_dedup_validation_and_csv_zip():
    ctx = Ctx(site_handler)
    try:
        eng = _engine(ctx)
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(N_SRC)]))
        st = asyncio.run(eng.run())
        eng.close()
        assert st["status"] == "COMPLETED" and st["counter_reconciliation"]["passed"], st.get("counter_reconciliation")
        h = ctx.srv.hits
        # L1 same-page + L2 cross-page + L3 in-flight dedup, proven server-side
        assert h["/img/logo.webp"] == 1 and h["/product/123"] == 2      # 1 direct + 1 as /r301 redirect target
        for i in range(N_SRC):
            assert h[f"/p/{i}"] == 1, "source page also a destination must not be fetched twice"
        for p in ("/img/og.jpg", "/img/broken.png", "/img/fake.png", "/nf", "/gone", "/err"):
            assert h[p] == (2 if p == "/nf" else 1), (p, h[p])         # /nf: direct + /r302 redirect hop
        assert h["/private/x"] == 0 and h["/robots.txt"] == 1

        pkg = build_on_page_csv_package(ctx.db, "E2E", ctx.dir, package_name="QC_V5_TEST_AUDIT",
                                        performance=st["performance"])
        assert pkg["final_qc_status"] == "PASS", [r for r in pkg["reconciliation"] if r["status"] != "PASS"]
        assert pkg["zip_verified"]
        with zipfile.ZipFile(pkg["zip_path"]) as z:
            assert sorted(z.namelist()) == ZIP_FILES
        w = pkg["work_dir"]
        urls = read_csv(os.path.join(w, "03_url_audit.csv"))
        imgs = read_csv(os.path.join(w, "05_image_audit.csv"))
        uu = {r["normalized_url"]: r for r in read_csv(os.path.join(w, "04_unique_url_validation.csv"))}
        ui = {r["normalized_image_url"]: r for r in read_csv(os.path.join(w, "06_unique_image_validation.csv"))}
        b = ctx.srv.base
        # relationship preservation: every page -> product (once per page, occurrence 2) and -> logo
        prod = [r for r in urls if r["normalized_url"] == f"{b}/product/123"]
        assert len(prod) == N_SRC and all(r["occurrence_count"] == "2" for r in prod)
        logo = [r for r in imgs if r["normalized_image_url"] == f"{b}/img/logo.webp"]
        assert len(logo) == N_SRC and all(r["occurrence_count"] == "2" for r in logo)
        assert {r["alt_text"] for r in logo} == {"Logo 0", "Logo 1"} and all(r["alt_present"] == "true" for r in logo)
        valsrc = Counter(r["validation_source"] for r in urls)
        assert valsrc["SOURCE_PAGE_REUSED"] >= N_SRC * 2                 # next/prev + canonical self links
        assert valsrc["IN_FLIGHT_REUSED"] + Counter(r["validation_source"] for r in imgs)["IN_FLIGHT_REUSED"] > 0
        assert valsrc["INVALID_SKIPPED"] == N_SRC and valsrc["NON_HTTP_SKIPPED"] == N_SRC
        # validation outcomes
        exp = {"/r301": ("3xx", "200"), "/r302": ("3xx", "404"), "/nf": ("4xx", "404"), "/gone": ("4xx", "410"),
               "/err": ("5xx", "500"), "/slow": ("Timeout", ""), "/private/x": ("Robots blocked", ""),
               "/waf": ("WAF blocked", "403")}
        for path, (bucket, code) in exp.items():
            r = uu[f"{b}{path}"]
            assert (r["status_bucket"], r["http_status"]) == (bucket, code), (path, r)
        assert uu[f"{b}/p/0"]["validation_source"] == "SOURCE_PAGE_REUSED"
        assert uu[f"{b}/product/123"]["validation_source"] == "NEW_CHECK"
        assert ui[f"{b}/img/fake.png"]["image_issue"].startswith("CONTENT_TYPE_MISMATCH")
        sp = ui[f"{b}/img/My Photo.webp"]                        # space in path: validated, not INVALID
        assert (sp["http_status"], sp["validation_state"], sp["image_type"]) == ("200", "VALIDATED", "HTTP_IMAGE")
        assert h["/img/My%20Photo.webp"] == 1
        assert ui[f"{b}/img/broken.png"]["http_status"] == "404"
        assert ui[f"{b}/img/logo.webp"]["reuse_count"] == str(N_SRC - 1)
        broken_img = {r["normalized_image_url"] for r in read_csv(os.path.join(w, "09_broken_images.csv"))}
        assert broken_img == {f"{b}/img/broken.png", f"{b}/img/fake.png"}
        assert len(read_csv(os.path.join(w, "09_broken_images.csv"))) == 2 * N_SRC   # every relationship kept
        broken = Counter(r["normalized_url"] for r in read_csv(os.path.join(w, "07_broken_urls.csv")))
        assert broken[f"{b}/nf"] == N_SRC and broken[f"{b}/r302"] == N_SRC and broken[f"{b}/slow"] == N_SRC
        redirects = Counter(r["normalized_url"] for r in read_csv(os.path.join(w, "08_redirect_urls.csv")))
        assert redirects == Counter({f"{b}/r301": N_SRC, f"{b}/r302": N_SRC})
        assert len(read_csv(os.path.join(w, "10_external_urls.csv"))) == N_SRC
        summary = {r["metric"]: r["value"] for r in read_csv(os.path.join(w, "01_summary.csv"))}
        assert summary["final_qc_status"] == "PASS" and summary["database_integrity_status"] == "PASS"
        assert int(summary["total_image_relationships"]) == len(imgs)
        assert int(summary["unique_image_urls"]) == len(ui) == int(summary["new_image_validations"])
        assert int(summary["new_url_validations"]) + int(summary["reused_url_validations"]) == int(summary["http_url_relationships"])

        # ---- actionable issue layer (12/13/14)
        iss = read_csv(os.path.join(w, "12_issues.csv"))
        by = {}
        for r in iss:
            by.setdefault((r["normalized_url"], r["issue_type"]), []).append(r)
        def one(path, itype, sev, n=N_SRC):
            rows = by.get((f"{b}{path}", itype), [])
            assert len(rows) == n and all(r["severity"] == sev and r["recommended_action"] for r in rows), (path, itype, len(rows))
        one("/nf", "NOT_FOUND", "HIGH")
        one("/gone", "GONE", "HIGH")
        one("/err", "SERVER_ERROR", "HIGH")
        one("/r301", "REDIRECTED", "LOW")
        one("/r302", "REDIRECT_TO_BROKEN", "HIGH")
        one("/slow", "TIMEOUT", "MEDIUM")
        one("/private/x", "ROBOTS_BLOCKED", "INFO")
        one("/waf", "WAF_BLOCKED", "MEDIUM")
        one("/img/broken.png", "IMAGE_NOT_FOUND", "HIGH")
        one("/img/fake.png", "IMAGE_CONTENT_TYPE_MISMATCH", "HIGH")
        one("/img/fake.png", "MISSING_ALT", "MEDIUM")
        assert len([r for r in iss if r["issue_type"] == "INVALID_URL"]) == N_SRC
        healthy = [r for r in iss if r["normalized_url"] == f"{b}/product/123"]
        # a healthy link raises no link issue; it is a live internal page missing from the sitemap (one row per URL)
        assert [r["issue_type"] for r in healthy] == ["NOT_IN_SITEMAP"], healthy
        nf = by[(f"{b}/nf", "NOT_FOUND")][0]
        assert nf["anchor_or_alt_text"] == "c" and nf["source_page_url"].startswith(f"{b}/p/") and nf["http_status"] == "404"
        summ = {r["issue_type"]: r for r in read_csv(os.path.join(w, "13_issue_summary.csv"))}
        assert summ["NOT_FOUND"]["unique_urls"] == "1" and summ["NOT_FOUND"]["source_pages_affected"] == str(N_SRC)
        assert sum(int(r["relationships"]) for r in summ.values()) == len(iss)
        pages = read_csv(os.path.join(w, "14_page_issue_summary.csv"))
        assert len(pages) == N_SRC and sum(int(r["total_issues"]) for r in pages) == len(iss)
        assert int(summary["issues_total"]) == len(iss)
    finally:
        ctx.close()


def test_image_inflight_single_flight():
    def handler(path):
        if path == "/robots.txt":
            return 200, {"Content-Type": "text/plain"}, b"", 0.0
        return 200, {"Content-Type": "image/png"}, b"\x89PNG", 0.4

    ctx = Ctx(handler)
    try:
        eng = make_engine(ctx.db, "IMG1")
        url = f"{ctx.srv.base}/img/logo.png"

        async def go():
            async with httpx.AsyncClient() as client:
                return await asyncio.gather(*[eng.validate_image(client, url) for _ in range(6)])

        res = asyncio.run(go())
        assert ctx.srv.hits["/img/logo.png"] == 1 and all(r is res[0] for r in res)
        assert res[0]["http_status"] == 200 and res[0]["content_type"] == "image/png" and res[0]["image_issue"] == ""
    finally:
        ctx.close()


def test_crash_restart_recovers_inflight_and_never_redoes_completed_work():
    """Hard kill (task cancel) while slow images/URLs are in flight -> restart: completed pages are
    skipped, persisted validations reused, in-flight PENDING work retried, nothing lost; a third run on
    the finished job makes zero requests (duplicate resume)."""
    ref = Ctx(site_handler)
    ctx = Ctx(site_handler)
    try:
        e0 = _engine(ref, job="R", source_concurrency=2)
        e0.initialize_inventory(sources_for(ref.srv.base, [f"/p/{i}" for i in range(N_SRC)]))
        st0 = asyncio.run(e0.run())
        e0.close()

        e1 = _engine(ctx, job="R", source_concurrency=2)
        e1.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(N_SRC)]))

        async def run_and_kill():
            t = asyncio.create_task(e1.run())
            while e1._run_sources_done < 5:
                await asyncio.sleep(0.01)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        asyncio.run(run_and_kill())
        e1.close()
        done_src = {r[0] for r in q(ctx.db, "SELECT source_page_url FROM on_page_source_pages WHERE job_id='R' AND crawl_state != 'PENDING'")}
        validated_urls = {r[0] for r in q(ctx.db, "SELECT normalized_url FROM on_page_validation_cache WHERE job_id='R' AND validation_state != 'PENDING'")}
        validated_imgs = {r[0] for r in q(ctx.db, "SELECT normalized_url FROM on_page_image_validation_cache WHERE job_id='R' AND validation_state != 'PENDING'")}
        assert 0 < len(done_src) < N_SRC
        before = Counter(ctx.srv.hits)

        e2 = _engine(ctx, job="R", source_concurrency=2)
        e2.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(N_SRC)]))
        st = asyncio.run(e2.run())
        e2.close()
        assert st["status"] == "COMPLETED" and st["counter_reconciliation"]["passed"]
        after = ctx.srv.hits
        b = ctx.srv.base
        for u in done_src:                                   # COMPLETED -> skip
            assert after[u[len(b):]] == before[u[len(b):]] == 1
        for u in validated_urls | validated_imgs:             # VALIDATED -> reuse (no new request)
            p = u[len(b):]
            if p.startswith(("/p/", "/r30")):
                continue                                      # source pages / redirect hops counted elsewhere
            assert after[p] == before[p], (p, before[p], after[p])
        for k in ("url_relationships_persisted", "unique_http_destinations", "new_http_validations",
                  "reused_http_validations", "image_relationships_persisted", "unique_image_urls",
                  "new_image_validations", "reused_image_validations"):
            assert st[k] == st0[k], (k, st[k], st0[k])
        assert q(ctx.db, "SELECT COUNT(*) FROM on_page_image_validation_cache WHERE job_id='R' AND validation_state='PENDING'")[0][0] == 0

        snapshot = sum(ctx.srv.hits.values())
        e3 = _engine(ctx, job="R")
        e3.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(N_SRC)]))
        st3 = asyncio.run(e3.run())
        e3.close()
        assert st3["status"] == "COMPLETED" and sum(ctx.srv.hits.values()) == snapshot   # duplicate resume = no work
        pkg = build_on_page_csv_package(ctx.db, "R", ctx.dir, package_name="R")
        assert pkg["final_qc_status"] == "PASS", [r for r in pkg["reconciliation"] if r["status"] != "PASS"]
    finally:
        ref.close()
        ctx.close()


def test_trust_gate_fails_on_discrepancy():
    """A tampered DB (orphan relationship) must flip final_qc_status to FAILED, never hide it."""
    ctx = Ctx(site_handler)
    try:
        eng = _engine(ctx, job="T")
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(3)]))
        asyncio.run(eng.run())
        eng.close()
        import sqlite3
        c = sqlite3.connect(ctx.db)
        c.execute("DELETE FROM on_page_image_validation_cache WHERE job_id='T' AND normalized_url LIKE '%logo.webp'")
        c.commit()
        c.close()
        pkg = build_on_page_csv_package(ctx.db, "T", ctx.dir, package_name="T")
        failed = {r["check"] for r in pkg["reconciliation"] if r["status"] == "FAIL"}
        assert pkg["final_qc_status"] == "FAILED"
        assert "every_reused_image_result_points_to_real_validation" in failed
    finally:
        ctx.close()


def test_host_budget_rate_limits_cdn_and_isolates_its_waf():
    """Images on a separate host (localhost) get their OWN budget: requests never exceed its rate,
    a WAF burst there steps down/pauses only that budget (primary untouched), and requeued images
    still end VALIDATED."""
    import threading
    state = {"waf_left": 3, "lock": threading.Lock()}
    port_box = {}

    def handler(path):
        if path == "/robots.txt":
            return 200, {"Content-Type": "text/plain"}, b"", 0.0
        if path.startswith("/p/"):
            i = int(path.split("/")[-1])
            imgs = "".join(f'<img src="http://localhost:{port_box["p"]}/bimg/{i}-{k}.png" alt="x">' for k in range(5))
            return html_resp(f"<html><body>{imgs}</body></html>")
        if path.startswith("/bimg/"):
            with state["lock"]:
                if state["waf_left"] > 0:
                    state["waf_left"] -= 1
                    return 403, {"Content-Type": "text/html", "Server": "AkamaiGHost"}, AKAMAI_403, 0.0
            return 200, {"Content-Type": "image/png"}, b"\x89PNG", 0.0
        return html_resp("nf", 404)

    ctx = Ctx(handler)
    port_box["p"] = ctx.srv.port
    try:
        eng = _engine(ctx, job="HB", source_concurrency=6, image_concurrency=10, waf_cooldown_threshold=3,
                      target_rps=50, min_rps=40, max_rps=50,
                      host_budgets={"localhost": {"rps": 5}}, host_budget_cooldown_sec=0.5)
        eng.initialize_inventory(sources_for(ctx.srv.base, [f"/p/{i}" for i in range(6)]))
        t0 = time.time()
        st = asyncio.run(eng.run())
        eng.close()
        assert st["status"] == "COMPLETED" and st["counter_reconciliation"]["passed"]
        hb = eng.host_budgets["localhost"]
        assert hb.cooldowns >= 1 and hb.rps < 5, hb.snapshot()           # stepped down 15% on its own WAF
        assert eng.cooldown_count == 0 and eng.target_rps == 50           # primary budget untouched
        ts = sorted(t for p, v in ctx.srv.times.items() if p.startswith("/bimg/") for t in v)
        assert len(ts) == 30 + 3                                          # 30 unique images + 3 WAF retries
        worst = max(sum(1 for t in ts if s <= t < s + 1.0) for s in ts)
        assert worst <= 6, f"{worst} requests in 1s on a 5 req/s budget"
        assert ts[-1] - ts[0] >= (30 - 1) / 5 * 0.8                       # cannot finish faster than the budget
        states = q(ctx.db, "SELECT validation_state, COUNT(*) FROM on_page_image_validation_cache WHERE job_id='HB' GROUP BY 1")
        assert states == [("VALIDATED", 30)], states
        assert st["performance"]["host_budgets"]["localhost"]["requests"] >= 33
        assert time.time() - t0 < 60
    finally:
        ctx.close()


def test_source_page_issues_and_validation_detail():
    """Page-level detail: soft-404, canonical problems, redirecting/404 sitemap URLs, empty pages,
    missing title, broken redirect (3xx without Location), truncated HTML; response_time excludes the
    rate-limit wait."""
    def handler(path):
        if path == "/robots.txt":
            return 200, {"Content-Type": "text/plain"}, b"", 0.0
        page = lambda head, body="<a href='/ok'>x</a>": html_resp(f"<html><head>{head}</head><body>{body}</body></html>")
        if path == "/good":
            return page("<title>Good</title><link rel='canonical' href='/good'>")
        if path == "/soft404":
            return page("<title>Page Not Found | Shop</title><link rel='canonical' href='/soft404'>")
        if path == "/nocanon":
            return page("<title>No canonical</title>")
        if path == "/twocanon":
            return page("<title>t</title><link rel='canonical' href='/a'><link rel='canonical' href='/b'>")
        if path == "/noncanon":
            return page("<title>t</title><link rel='canonical' href='/good'>")
        if path == "/moved":
            return 301, {"Location": "/good"}, b"", 0.0
        if path == "/dead":
            return html_resp("gone", 404)
        if path == "/empty":
            return page("<title>Empty</title><link rel='canonical' href='/empty'>", body="")
        if path == "/notitle":
            return page("<link rel='canonical' href='/notitle'>")
        if path == "/badredirect":
            return 302, {}, b"", 0.0
        if path == "/huge":
            return page("<title>Huge</title><link rel='canonical' href='/huge'>", body="<p>" + "x" * 200_000 + "</p><a href='/late'>late</a>")
        return html_resp("<html>ok</html>")

    ctx = Ctx(handler)
    try:
        paths = ["/good", "/soft404", "/nocanon", "/twocanon", "/noncanon", "/moved", "/dead", "/empty",
                 "/notitle", "/badredirect", "/huge"]
        eng = _engine(ctx, job="SRC", source_concurrency=2, target_rps=4, min_rps=4, max_rps=4,
                      max_source_bytes=64 * 1024)
        eng.initialize_inventory(sources_for(ctx.srv.base, paths))
        st = asyncio.run(eng.run())
        eng.close()
        assert st["status"] == "COMPLETED"
        b = ctx.srv.base
        rt = dict(q(ctx.db, "SELECT source_page_url, response_time_ms FROM on_page_source_pages WHERE job_id='SRC'"))
        assert rt[f"{b}/good"] < 500, "response_time must be network time only, not the 250 ms/req rate-limit wait"
        pkg = build_on_page_csv_package(ctx.db, "SRC", ctx.dir, package_name="SRC")
        assert pkg["final_qc_status"] == "PASS", [r for r in pkg["reconciliation"] if r["status"] != "PASS"]
        iss = read_csv(os.path.join(pkg["work_dir"], "12_issues.csv"))
        page = lambda p: {r["issue_type"] for r in iss if r["source_page_url"] == f"{b}{p}" and r["category"] == "SOURCE_PAGE"
                          and r["issue_type"] != "CLASSIFICATION_NEEDS_REVIEW"}   # fixtures carry no tag-1/tag-2 meta
        assert any(r["issue_type"] == "CLASSIFICATION_NEEDS_REVIEW" and r["severity"] == "INFO" for r in iss)
        assert page("/good") == set()
        assert "SOFT_404" in page("/soft404")
        assert "MISSING_CANONICAL" in page("/nocanon")
        assert "MULTIPLE_CANONICALS" in page("/twocanon")
        assert "SITEMAP_URL_NOT_CANONICAL" in page("/noncanon")
        assert "SITEMAP_URL_REDIRECTS" in page("/moved")
        assert page("/dead") == {"SOURCE_NOT_FOUND"}
        assert "NO_LINKS_EXTRACTED" in page("/empty")
        assert "MISSING_TITLE" in page("/notitle")
        assert page("/badredirect") == {"SOURCE_BROKEN_REDIRECT"}
        assert "PAGE_TRUNCATED" in page("/huge")
        src = {r["source_page_url"]: r for r in read_csv(os.path.join(pkg["work_dir"], "02_source_page_audit.csv"))}
        assert src[f"{b}/huge"]["html_truncated"] == "1" and src[f"{b}/soft404"]["page_title"] == "Page Not Found | Shop"
        assert src[f"{b}/badredirect"]["crawl_state"] == "FAILED"
    finally:
        ctx.close()
