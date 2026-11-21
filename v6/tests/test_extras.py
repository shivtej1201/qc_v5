"""Extra free checks (Nu, LanguageTool mocked; Lighthouse real when installed) and GA4-led sampling (mocked)."""
import asyncio
import json
import os

import httpx
import pytest

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"
from fixture_site import FixtureSite  # noqa: E402
from qc6 import ga4, services  # noqa: E402
from qc6.workers import extras  # noqa: E402
from qc6.workers.visit_stage import plan  # noqa: E402

LH = os.environ.get("QC6_TEST_LIGHTHOUSE", "/tmp/claude-0/lh/node_modules/.bin/lighthouse")


@pytest.fixture(scope="module")
def site_srv():
    s = FixtureSite()
    yield s
    s.close()


def test_visible_text():
    t = extras.visible_text("<html><head><title>Cast iron pan</title><script>var x='no'</script></head><body>"
                            "<h1>Pan</h1><p>Seasoned  &amp; oven safe</p><a href=/x>Buy now</a></body></html>")
    assert t == "Cast iron pan. Pan. Seasoned & oven safe. Buy now."


def test_extras_stage(pg_pool, site_srv, monkeypatch):
    real = httpx.AsyncHTTPTransport()
    seen = []

    async def handler(req: httpx.Request):
        seen.append(req.url.host)
        if req.url.host == "nu.test":
            return httpx.Response(200, json={"messages": [{"type": "error", "lastLine": 12, "message": "Stray end tag “div”."},
                                                          {"type": "info", "message": "fine"}]})
        if req.url.host == "lt.test":
            body = req.content.decode()
            assert "language=en-IN" in body
            return httpx.Response(200, json={"matches": [{"rule": {"category": {"id": "TYPOS"}}, "context": {"text": "We recieve it", "offset": 3, "length": 7},
                                                          "replacements": [{"value": "receive"}]}]})
        return await real.handle_async_request(req)
    extras.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("QC6_NU_URL", "http://nu.test")
    monkeypatch.setenv("QC6_LANGUAGETOOL_URL", "http://lt.test")
    if os.path.exists(LH):
        monkeypatch.setenv("QC6_LIGHTHOUSE_CMD", LH)
    else:
        monkeypatch.delenv("QC6_LIGHTHOUSE_CMD", raising=False)

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Extra Co", "e@extra.test", "Eve")
        sid = await services.create_site(pool, ws["workspace_id"], "Extra", site_srv.base, {"language": "en-IN"})
        rid = await pool.fetchval("INSERT INTO runs (site_id, kind, status) VALUES ($1,'full','running') RETURNING id", sid)
        for name, path in (("Product pages", "/product/1"), ("City pages", "/city/1")):
            tid = await pool.fetchval("INSERT INTO templates (site_id, fingerprint, name, page_count) VALUES ($1,1,$2,30) RETURNING id", sid, name)
            await pool.execute("INSERT INTO pages (run_id, url, path, template_id, http_status) VALUES ($1,$2,$3,$4,200)",
                               rid, site_srv.base + path, path, tid)
        site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", sid))
        out = await extras.run_extras(pool, rid, site)
        fixes = await pool.fetch("SELECT rule, title, pages_affected, texts FROM fixes WHERE site_id = $1 ORDER BY rule, title", sid)
        await pool.close()
        return out, fixes
    try:
        out, fixes = asyncio.run(go())
    finally:
        extras.set_transport(None)
    rules = [f["rule"] for f in fixes]
    assert rules.count("HTML_INVALID") == 2 and rules.count("SPELLING") == 2
    sp = next(f for f in fixes if f["rule"] == "SPELLING")
    assert sp["pages_affected"] == 30 and '"recieve" → "receive"' in sp["texts"]["fix"]
    hv = next(f for f in fixes if f["rule"] == "HTML_INVALID")
    assert hv["title"].endswith("have 1 markup errors") and "line 12" in hv["texts"]["fix"]
    if os.path.exists(LH):
        lh = out["extras_report"]["pages"][0]["lighthouse"]
        assert set(lh["scores"]) == {"performance", "accessibility", "seo"} and lh["version"]


def test_ga4_traffic_leads_the_visit_plan(pg_pool, tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sa = {"type": "service_account", "client_email": "qc@proj.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token",
          "private_key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
    p = tmp_path / "sa.json"
    p.write_text(json.dumps(sa))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(p))

    def handler(req: httpx.Request):
        if req.url.host == "oauth2.googleapis.com":
            assert b"grant_type=urn" in req.content
            return httpx.Response(200, json={"access_token": "ya29.x"})
        body = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer ya29.x" and "/properties/123456:runReport" in str(req.url)
        if body["dimensions"][0]["name"] == "landingPage":
            return httpx.Response(200, json={"rows": [{"dimensionValues": [{"value": f"/product/{n}"}], "metricValues": [{"value": str(1000 - n)}]}
                                                      for n in (17, 3, 9)]})
        return httpx.Response(200, json={"rows": [{"dimensionValues": [{"value": "mobile"}], "metricValues": [{"value": "780"}]},
                                                  {"dimensionValues": [{"value": "desktop"}], "metricValues": [{"value": "220"}]}]})
    ga4.set_transport(httpx.MockTransport(handler))

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "GA Co", "g@ga.test", "Gus")
        sid = await services.create_site(pool, ws["workspace_id"], "GA", "https://ga.test", {"ga4_property_id": "123456"})
        rid = await pool.fetchval("INSERT INTO runs (site_id, kind, status) VALUES ($1,'full','running') RETURNING id", sid)
        tid = await pool.fetchval("INSERT INTO templates (site_id, fingerprint, name, page_count) VALUES ($1,1,'Product pages',30) RETURNING id", sid)
        for n in range(30):
            await pool.execute("INSERT INTO pages (run_id, url, path, template_id, http_status) VALUES ($1,$2,$3,$4,200)",
                               rid, f"https://ga.test/product/{n}", f"/product/{n}", tid)
        site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", sid))
        tr = await ga4.traffic(site)
        targets = await plan(pool, rid, per_template=6, traffic=tr["paths"], mobile_share=tr["mobile_share"])
        await pool.close()
        return tr, targets
    try:
        tr, targets = asyncio.run(go())
    finally:
        ga4.set_transport(None)
    assert tr["mobile_share"] == 0.78 and tr["paths"]["/product/17"] == 983
    urls = [t.url for t in targets]
    assert urls[:3] == ["https://ga.test/product/3", "https://ga.test/product/9", "https://ga.test/product/17"] or \
        set(urls[:3]) == {"https://ga.test/product/3", "https://ga.test/product/9", "https://ga.test/product/17"}
    assert len(urls) == 6 and len(set(urls)) == 6
