"""Real-visitor review against the fixture site, in real Chromium."""
import asyncio
import os
import time

import pytest

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

from fixture_site import FixtureSite, Handler  # noqa: E402
from qc6.crawl.pacing import Persona  # noqa: E402
from qc6.workers import visit as V  # noqa: E402
from qc6.workers.visit_stage import findings_from  # noqa: E402

FAST = Persona(think_median_s=0.2, think_min_s=0.05, think_max_s=0.4, scroll_pause_min_s=0.05, scroll_pause_max_s=0.1)


@pytest.fixture(scope="module")
def site():
    s = FixtureSite()
    yield s
    s.close()


def _site_dict(s):
    return {"id": 1, "primary_host": "127.0.0.1", "access_token": "tok-abc", "settings": {}}


def test_real_visits_find_human_visible_problems(site, monkeypatch, tmp_path):
    from playwright.async_api import async_playwright
    monkeypatch.setattr(V.settings.__class__, "objects_dir", property(lambda self: str(tmp_path)))
    monkeypatch.setattr(V, "Persona", lambda *a, **k: FAST)
    raw = {"product": {"h1_count": 0, "internal_links": 9}, "article": {"h1_count": 1, "internal_links": 3},
           "city": {"h1_count": 0, "internal_links": 3}}
    targets = [V.VisitTarget(f"{site.base}/{t}/{i}", "mobile" if i % 2 else "desktop", tid, raw[t])
               for tid, t in enumerate(("product", "article", "city")) for i in range(3)]

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            orig = V.Visitor.__init__

            def fast_init(self, *a, **k):
                k["persona"] = FAST
                orig(self, *a, **k)
            V.Visitor.__init__ = fast_init
            try:
                t0 = time.perf_counter()
                res = await V.run_visits(b, _site_dict(site), targets, budget=None, visitors=3)
                return res, time.perf_counter() - t0
            finally:
                V.Visitor.__init__ = orig
                await b.close()
    results, took = asyncio.run(go())
    assert len(results) == 9 and all(r.status == "ok" for r in results), [(r.url, r.status, r.error) for r in results]

    # honest identification on every request
    assert "QC-Audit/6.0" in Handler.hits["__ua__"] and Handler.hits["__token__"] == "tok-abc"
    # visitors behaved like people: consent clicked, read, scrolled, screenshot taken
    prod = [r for r in results if "/product/" in r.url]
    assert all(r.consent.get("clicked") == "accept" for r in prod), [(r.url, r.device, r.consent, r.timeline) for r in prod]
    steps = [s["step"] for s in prod[0].timeline]
    assert steps[:2] == ["open", "settled"] and "read" in steps and "scrolled" in steps and steps[-1] == "done"
    assert all(r.screenshot and os.path.exists(tmp_path / r.screenshot) for r in results)
    assert all("CLS" in r.vitals for r in results)

    f = findings_from(results, "127.0.0.1")
    by_rule = {}
    for it in f["items"]:
        by_rule.setdefault(it.rule, set()).add(it.template_id)
    assert by_rule.get("CONTENT_ONLY_AFTER_JS") == {2}                        # city: h1 only after JS
    assert by_rule.get("CONSOLE_ERRORS") == {2}                               # city: offers is undefined
    assert 2 in by_rule.get("LINKS_ONLY_AFTER_JS", set())
    assert 0 in by_rule.get("A11Y_SERIOUS", set())                            # product: button without a name
    assert 0 in by_rule.get("LAYOUT_SHIFT", set()) or 0 in by_rule.get("CONSENT_SHIFTS_PAGE", set())
    assert 1 not in set().union(*by_rule.values())                            # the clean article template has nothing


def test_challenge_stops_visits(site, monkeypatch, tmp_path):
    from playwright.async_api import async_playwright
    monkeypatch.setattr(V.settings.__class__, "objects_dir", property(lambda self: str(tmp_path)))
    targets = [V.VisitTarget(f"{site.base}/challenge/{i}", "desktop", 5, {}) for i in range(6)]

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            try:
                return await V.run_visits(b, _site_dict(site), targets, budget=None, visitors=1)
            finally:
                await b.close()
    results = asyncio.run(go())
    assert results[0].status == "challenged" and results[0].error == "akamai"
    assert all(r.status == "skipped" for r in results[1:])                   # never retried, never pushed through
    f = findings_from(results, "127.0.0.1")
    assert [i.rule for i in f["items"]] == ["ACCESS_CHALLENGED"]
