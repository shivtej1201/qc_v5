"""Free SEO integrations: every check runs against an httpx.MockTransport, so no network is used and results are
deterministic. Live calls to Google / Bing / archive.org / IndexNow are not exercised here."""
import asyncio
import json
import os

import httpx
import pytest

from qcv5.core.config import settings
from qcv5.integrations import base, catalogue, run_integration
from qcv5.integrations import ai_access, google_updates, indexnow, pagespeed, security_headers, structured_data, wayback


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mock(monkeypatch):
    """Install a router: {(method, url-without-query) or url-prefix: handler(request) -> httpx.Response}."""
    routes = {}
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        key = f"{request.method} {request.url.copy_with(query=None)}"
        for k, fn in routes.items():
            if key == k or key.startswith(k):
                return fn(request)
        return httpx.Response(404, text="not found")

    base.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "ALLOW_LOCAL_TESTING", True, raising=False)
    monkeypatch.setattr(base.URLNormalizer, "is_safe_url", staticmethod(lambda url, allow_local=False: True))
    yield routes, seen
    base.set_transport(None)


# ----------------------------------------------------------------------------- robots.txt (RFC 9309)
def test_robots_parser_rfc9309_semantics():
    p = ai_access.parse_robots("User-agent: *\nDisallow: /private\nAllow: /private/ok$\n\n"
                               "User-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n"
                               "Content-Signal: search=yes, ai-train=no\nSitemap: https://ex.com/s.xml\n")
    star = ai_access.select_group(p, "ClaudeBot")
    gpt = ai_access.select_group(p, "gptbot")                    # case-insensitive token match
    assert star["matched"] == "star" and gpt["matched"] == "named"
    assert ai_access.is_allowed(star["rules"], "/") and not ai_access.is_allowed(star["rules"], "/private/x")
    assert ai_access.is_allowed(star["rules"], "/private/ok")     # longest match + $ anchor
    assert not ai_access.is_allowed(gpt["rules"], "/")
    assert gpt["content_signal"] == ["search=yes, ai-train=no"]
    assert p["sitemap"] == ["https://ex.com/s.xml"]
    assert ai_access.select_group(p, "GPTBot-Image")["matched"] == "star"   # whole-token match only
    # pathological wildcard rule is evaluated without regex backtracking
    evil = [("disallow", "*" * 5000 + "x")]
    assert ai_access.is_allowed(evil, "/" + "a" * 5000)                 # no 'x' -> rule does not match
    assert not ai_access.is_allowed(evil, "/" + "a" * 5000 + "x")
    assert ai_access.parse_content_signal("search=maybe, foo=yes")["issues"] == [
        "value for 'search' must be yes or no", "unknown key 'foo'"]


def test_ai_access_flags_blocked_search_crawlers_and_llms(mock):
    routes, _ = mock
    routes["GET https://site.test/robots.txt"] = lambda r: httpx.Response(
        200, text="User-agent: *\nAllow: /\n\nUser-agent: OAI-SearchBot\nDisallow: /\n")
    routes["GET https://site.test/llms.txt"] = lambda r: httpx.Response(200, text="# Site\n> summary\n- [Docs](/docs)\n")
    out = run(ai_access.run("https://site.test/page"))
    assert out["status"] == "ok"
    ids = {f["id"]: f for f in out["findings"]}
    assert ids["search-crawlers-blocked"]["severity"] == "MEDIUM"
    assert "OAI-SearchBot" in ids["search-crawlers-blocked"]["detail"]
    assert "no-sitemap-line" in ids
    assert out["data"]["llms_txt"]["present"] and out["data"]["llms_txt"]["h1"] == "Site"
    gptbot = next(a for a in out["data"]["agents"] if a["token"] == "GPTBot")
    assert gptbot["root_allowed"] and gptbot["group"] == "star"


def test_ai_access_robots_5xx_is_high(mock):
    routes, _ = mock
    routes["GET https://site.test/robots.txt"] = lambda r: httpx.Response(503, text="down")
    out = run(ai_access.run("site.test"))
    assert any(f["id"] == "robots-5xx" and f["severity"] == "HIGH" for f in out["findings"])


# ----------------------------------------------------------------------------- security headers
def test_security_header_grading():
    h = security_headers.analyze_headers({
        "Strict-Transport-Security": "max-age=300",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'",
        "X-Frame-Options": "DENY", "Referrer-Policy": "unsafe-url"},
        ["sid=secret-value; Path=/", "pref=1; Secure; HttpOnly; SameSite=Lax"], https=True)
    assert h["hsts"]["max_age"] == 300 and "max-age is below one year." in h["hsts"]["issues"]
    assert "'unsafe-inline' in script-src." in h["csp"]["issues"]
    assert h["framing"]["ok"] and not h["referrer_policy"]["ok"] and not h["x_content_type_options"]["ok"]
    assert [c["name"] for c in h["cookies"]] == ["sid", "pref"]
    assert "secret-value" not in json.dumps(h)                    # cookie values are never returned
    ids = {f["id"] for f in security_headers.header_findings(h, True)}
    assert {"hsts-weak", "csp-weak", "nosniff-missing", "referrer-policy-unsafe", "cookie-insecure"} <= ids


def test_security_headers_http_redirect_and_www(mock):
    routes, _ = mock
    routes["GET https://site.test/"] = lambda r: httpx.Response(200, text="ok", headers={
        "strict-transport-security": "max-age=31536000; includeSubDomains", "x-content-type-options": "nosniff"})
    routes["GET http://site.test/"] = lambda r: httpx.Response(200, text="plain")          # no redirect
    routes["GET https://www.site.test/"] = lambda r: httpx.Response(200, text="dup")       # duplicate host
    out = run(security_headers.run("https://site.test/"))
    ids = {f["id"]: f["severity"] for f in out["findings"]}
    assert ids["http-not-redirected"] == "HIGH" and ids["www-duplicate-host"] == "MEDIUM"
    assert "hsts-missing" not in ids and out["summary"]["https"] is True


# ----------------------------------------------------------------------------- structured data
PAGE = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Product","name":"Kit","offers":{"@type":"Offer","price":"₹1,499"}},
 {"@type":"ClaimReview","claimReviewed":"x"}]}</script>
<script type="application/ld+json">{"broken": </script></head><body></body></html>"""


def test_structured_data_validation(mock):
    routes, _ = mock
    routes["GET https://shop.test/p"] = lambda r: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    out = run(structured_data.run("https://shop.test/p"))
    ids = {f["id"] for f in out["findings"]}
    assert "jsonld-invalid" in ids                                   # second block is broken JSON
    assert "deprecated-ClaimReview" in ids
    assert "Offer-missing-required" in ids                            # priceCurrency missing
    assert "Product-missing-recommended" in ids
    assert out["data"]["types"] == ["ClaimReview", "Offer", "Product"]
    assert len(structured_data.template_list()) == 11


# ----------------------------------------------------------------------------- PageSpeed / CrUX
PSI = {"analysisUTCTimestamp": "2026-11-20T00:00:00Z",
       "lighthouseResult": {"lighthouseVersion": "13.0.0", "categories": {
           "performance": {"score": 0.42}, "seo": {"score": 0.95}, "accessibility": {"score": None}},
           "audits": {"largest-contentful-paint": {"numericValue": 5200, "displayValue": "5.2 s", "score": 0.1},
                      "render-blocking-resources": {"title": "Eliminate render-blocking resources",
                                                    "details": {"type": "opportunity", "overallSavingsMs": 900}}}},
       "loadingExperience": {"metrics": {"CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 31, "category": "SLOW"}}}}


def test_pagespeed_parsing_and_findings(mock, monkeypatch):
    routes, seen = mock
    monkeypatch.setenv("PAGESPEED_API_KEY", "k-123")
    routes["GET https://www.googleapis.com/pagespeedonline/v5/runPagespeed"] = lambda r: httpx.Response(200, json=PSI)
    routes["POST https://chromeuxreport.googleapis.com/v1/records:queryRecord"] = lambda r: httpx.Response(200, json={
        "record": {"metrics": {"largest_contentful_paint": {"percentiles": {"p75": 3100}}}}})
    out = run(pagespeed.run("https://site.test/", strategy="desktop"))
    assert out["status"] == "ok" and out["summary"]["scores"] == {"performance": 42, "seo": 95}
    assert out["data"]["field"]["url_cumulative_layout_shift"]["p75"] == 0.31
    assert out["data"]["crux"]["largest_contentful_paint"]["rating"] == "needs-improvement"
    assert any(f["id"] == "lighthouse-performance-poor" for f in out["findings"])
    psi_req = seen[0]
    assert psi_req.headers["x-goog-api-key"] == "k-123" and "k-123" not in str(psi_req.url)   # key in header only
    assert psi_req.url.params.get_list("category") == ["PERFORMANCE", "ACCESSIBILITY", "BEST_PRACTICES", "SEO"]
    assert "k-123" not in json.dumps(out)


def test_pagespeed_keyless_quota_needs_key(mock, monkeypatch):
    routes, _ = mock
    monkeypatch.delenv("PAGESPEED_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    routes["GET https://www.googleapis.com/pagespeedonline/v5/runPagespeed"] = lambda r: httpx.Response(429)
    out = run(pagespeed.run("https://site.test/"))
    assert out["status"] == "needs_key" and "PAGESPEED_API_KEY" in out["error"]


# ----------------------------------------------------------------------------- IndexNow / Wayback / updates
def test_indexnow_key_flow(mock, monkeypatch):
    routes, seen = mock
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    out = run(indexnow.run("https://site.test/"))
    assert out["status"] == "needs_key" and len(out["data"]["suggested_key"]) == 32
    key = "abcdef1234567890"
    routes[f"GET https://site.test/{key}.txt"] = lambda r: httpx.Response(200, text=key)
    routes["POST https://api.indexnow.org/indexnow"] = lambda r: httpx.Response(202)
    out = run(indexnow.run("https://site.test/", key=key, submit=True, urls=["https://site.test/a", "https://site.test/b"]))
    assert out["status"] == "ok" and out["data"]["key_published"] and out["summary"]["submitted"] == 2
    body = json.loads(seen[-1].content)
    assert body == {"host": "site.test", "key": key, "keyLocation": f"https://site.test/{key}.txt",
                    "urlList": ["https://site.test/a", "https://site.test/b"]}
    assert indexnow.validate_submission("site.test", key, f"https://site.test/{key}.txt",
                                        ["https://other.test/x"]).startswith("1 URL(s) are not on host")


def test_wayback_summary(mock):
    routes, _ = mock
    rows = [["timestamp", "statuscode", "digest"], ["20120101000000", "200", "A"], ["20150601000000", "200", "A"],
            ["20240101000000", "200", "B"]]
    routes["GET https://web.archive.org/cdx/search/cdx"] = lambda r: httpx.Response(200, json=rows)
    out = run(wayback.run("https://site.test/"))
    assert out["summary"]["captures"] == 3 and out["summary"]["content_changes"] == 1
    assert out["data"]["captures_per_year"] == {"2012": 1, "2015": 1, "2024": 1}
    assert out["summary"]["first_capture"].startswith("2012-01-01")


def test_google_updates_dataset():
    q = google_updates.query(since="2025-01-01", kind="core", limit=3)
    assert q["updates"] and all(u["date"] >= "2025-01-01" and "core" in u["kind"] for u in q["updates"])
    assert len(q["updates"]) <= 3 and all(u["source"].startswith("https://") for u in q["updates"])


# ----------------------------------------------------------------------------- registry + safety
def test_catalogue_and_ssrf_block(monkeypatch):
    c = catalogue()
    ids = [i["id"] for i in c["integrations"]]
    assert ids == ["ai_access", "security_headers", "structured_data", "pagespeed", "wayback", "indexnow",
                   "bing_webmaster", "google_search_console"]
    assert all("runner" not in i for i in c["integrations"])
    monkeypatch.setattr(settings, "ALLOW_LOCAL_TESTING", False, raising=False)
    out = run(run_integration("structured_data", "http://127.0.0.1:8002/"))
    assert out["status"] == "unreachable" and "blocked" in out["error"]
    out = run(run_integration("security_headers", "ftp://x"))
    assert out["status"] == "error"
    with pytest.raises(KeyError):
        run(run_integration("nope", "https://x.test"))


def test_redirect_to_private_address_is_blocked(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    base.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "ALLOW_LOCAL_TESTING", False, raising=False)
    # public.test resolves "public"; the redirect target is the cloud metadata address
    monkeypatch.setattr(base.URLNormalizer, "is_safe_url",
                        staticmethod(lambda url, allow_local=False: "169.254" not in url))
    try:
        async def go():
            async with base.SafeFetcher() as f:
                return await f.get("https://public.test/")
        r = run(go())
        assert r.error and r.error.startswith("blocked") and r.status is None
    finally:
        base.set_transport(None)


# ----------------------------------------------------------------------------- REST API
def test_integrations_api_endpoints(mock):
    from fastapi.testclient import TestClient
    from qcv5.api.server import app
    routes, _ = mock
    routes["GET https://site.test/robots.txt"] = lambda r: httpx.Response(200, text="User-agent: *\nAllow: /\n")
    c = TestClient(app)
    cat = c.get("/api/integrations").json()
    assert len(cat["integrations"]) == 8 and cat["not_included"]
    r = c.post("/api/integrations/ai_access/run", json={"url": "https://site.test/", "params": {"evil": {"x": 1}}})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert c.post("/api/integrations/nope/run", json={"url": "https://site.test/"}).status_code == 404
    assert c.post("/api/integrations/ai_access/run", json={"url": "x" * 3000}).status_code == 400
    u = c.get("/api/integrations/google-updates?since=2025&limit=2").json()
    assert len(u["updates"]) == 2 and u["total_matching"] >= 2
    assert c.get("/api/integrations/google-updates?since=junk").status_code == 400
    t = c.get("/api/integrations/schema-templates").json()["templates"]
    assert len(t) == 11 and all(x["template"].get("@context") for x in t)
