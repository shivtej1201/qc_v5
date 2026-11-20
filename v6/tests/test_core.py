import asyncio
import random
import time

import pytest

from qc6.crawl import challenge
from qc6.crawl.pacing import Pacer, Persona
from qc6.domain import fixes, rules, score, templates


# ------------------------------------------------------------------ templates (SimHash clustering)
def _page(kind: str, i: int) -> str:
    rnd = random.Random(i)
    items = "".join(f'<li class="tile"><a href="/p/{rnd.randint(1, 9999)}">Item {k}</a><img src="/i/{k}.jpg"></li>'
                    for k in range(rnd.randint(3, 40)))
    head = '<html><head><title>T</title><script>var x = 1;</script></head><body><header class="top"><nav class="menu"><a>a</a><a>b</a></nav></header>'
    foot = '<footer class="site-foot"><p>c</p></footer></body></html>'
    if kind == "product":
        main = (f'<main class="pdp"><div class="gallery"><img src="/g/{i}.jpg"></div><div class="buy"><h1 class="name">P{i}</h1>'
                f'<span class="price">₹{rnd.randint(100, 9000)}</span><button class="add">Add</button></div>'
                f'<section class="specs"><table class="spec"><tr><td>a</td><td>b</td></tr></table></section></main>')
    elif kind == "article":
        paras = "".join("<p>text</p>" for _ in range(rnd.randint(4, 25)))
        main = f'<main class="post"><article class="story"><h1 class="headline">A{i}</h1><div class="byline">x</div>{paras}</article><aside class="related"><ul>{items}</ul></aside></main>'
    else:
        main = f'<main class="listing"><div class="filters"><select class="sort"><option>x</option></select></div><ul class="grid">{items}</ul><div class="pager"><a>1</a><a>2</a></div></main>'
    return head + main + foot


def test_template_clustering_purity():
    kinds = ["product", "article", "category"]
    pages = [(f"/{k}/{i}", _page(k, i), k) for i in range(60) for k in kinds]
    idx = templates.TemplateIndex(radius=10)
    assigned = {}
    for path, html, kind in pages:
        assigned[path] = (idx.assign(templates.simhash(html), path), kind)
    by_tpl = {}
    for tid, kind in assigned.values():
        by_tpl.setdefault(tid, []).append(kind)
    purity = sum(max(v.count(k) for k in set(v)) for v in by_tpl.values()) / len(pages)
    assert purity >= 0.95
    assert len(by_tpl) <= 6                       # 3 real templates; a little splitting is tolerated
    big = [t for t, v in by_tpl.items() if len(v) >= 20]
    assert len(big) == 3
    assert templates.distance(templates.simhash(_page("product", 1)), templates.simhash(_page("article", 1))) > 10


def test_template_naming_and_patterns():
    assert templates.path_pattern("/loans/personal-loan/pune-123") == "/loans/personal-loan/*"
    assert templates.path_pattern("/") == "/"
    assert templates.template_name("/loans/personal-loan/*") == "Personal loan pages"
    assert templates.from_signed(templates.to_signed(2 ** 64 - 5)) == 2 ** 64 - 5


# ------------------------------------------------------------------ fixes
def test_fix_keys_group_by_root_cause():
    a = fixes.key_for("H1_MISSING", 7, "", "/p/1")
    b = fixes.key_for("H1_MISSING", 7, "", "/p/2")
    c = fixes.key_for("H1_MISSING", 8, "", "/p/1")
    assert a == b != c                                              # template scope: one fix per template
    l1 = fixes.key_for("BROKEN_LINK", 7, "https://x.test/gone", "/p/1")
    l2 = fixes.key_for("BROKEN_LINK", 9, "https://x.test/gone", "/q/5")
    assert l1 == l2                                                 # subject scope: one fix per broken target
    p1 = fixes.key_for("PAGE_NOT_FOUND", 7, "", "/p/1")
    p2 = fixes.key_for("PAGE_NOT_FOUND", 7, "", "/p/2")
    assert p1 != p2                                                 # page scope


def test_fix_lifecycle_after_run():
    assert fixes.after_run("verified", True) == ("open", True)            # regression
    assert fixes.after_run("fixed_pending_check", True) == ("open", False)
    assert fixes.after_run("open", False) == ("verified", False)
    assert fixes.after_run("wont_fix", False) == ("wont_fix", False)
    assert fixes.can_move("open", "in_progress") and not fixes.can_move("open", "verified")


# ------------------------------------------------------------------ copy
def test_rule_copy_is_human():
    assert rules.lint_copy() == []
    t = rules.render("H1_MISSING", n_pages=41200, template="Product", example="https://x.test/p/1")
    assert t["title"] == "41,200 Product pages have no main heading"
    assert rules.render("PAGE_CHECK", n_pages=1, template="Product", value="not in sitemap")["title"] == "1 Product page: not in sitemap"
    assert rules.format_count(300000) == "3,00,000" and rules.format_count(300000, "intl") == "300,000"
    assert rules.format_count(12345678) == "1,23,45,678"


# ------------------------------------------------------------------ pacing
def test_pacing_is_human_scale_and_replayable():
    p1, p2 = Pacer(seed=42), Pacer(seed=42)
    t = [p1.think() for _ in range(500)]
    assert t == [p2.think() for _ in range(500)]
    assert min(t) >= 1.0 and max(t) <= 8.0
    med = sorted(t)[250]
    assert 2.0 <= med <= 3.0
    plan = Pacer(seed=1).scroll_plan(5000, 800)
    assert plan[-1][0] == 4200 and all(0.3 <= s <= 0.9 for _, s in plan)
    assert all(b - a <= 0.9 * 800 + 1 for (a, _), (b, _) in zip([(0, 0)] + plan, plan))
    delays = Pacer(Persona(typing_wpm=40), seed=3).key_delays("85000")
    assert len(delays) == 5 and all(0.19 <= d <= 0.41 for d in delays)        # 0.3 s per char at 40 wpm, +-35%
    path = Pacer(seed=1).mouse_path(0, 0, 100, 50)
    assert path[-1] == (100, 50)


# ------------------------------------------------------------------ challenge
def test_challenge_detection():
    assert challenge.detect(403, {"Server": "AkamaiGHost"}, "Access Denied") == "akamai"
    assert challenge.detect(200, {}, "<title>Just a moment...</title>") == "interstitial"
    assert challenge.detect(503, {"cf-ray": "x"}, "") == "cloudflare"
    assert challenge.detect(200, {}, "<p>Access denied to admins only</p>") is None          # ordinary page
    assert challenge.detect(404, {"Server": "AkamaiGHost"}, "not found") is None
    rule = challenge.allowlist_rule("akamai", "X-QC-Audit", "tok123", ["203.0.113.24"])
    assert "X-QC-Audit equals tok123" in rule and "203.0.113.24" in rule


# ------------------------------------------------------------------ score
def test_score():
    s = score.compute(1000, [{"severity": "high", "dimension": "content", "pages_affected": 500},
                             {"severity": "critical", "dimension": "experience", "pages_affected": 2000}])
    assert s["dimensions"]["content"] == 65.0 and s["dimensions"]["experience"] == 0.0
    assert s["score"] == round(100 * 0.30 + 100 * 0.20 + 65 * 0.15 + 0 + 100 * 0.10 + 100 * 0.05, 1)
    assert score.compute(0, [])["score"] is None


# ------------------------------------------------------------------ host budget (real Postgres)
def test_host_budget_enforces_rate(pg_pool):
    from qc6.crawl.budget import HostBudget, Paused

    async def go():
        pool = await pg_pool()
        await HostBudget.ensure(pool, "rate.test", rate_rps=20, burst=1)
        b = HostBudget(pool, "rate.test", share=1.0)
        t0 = time.perf_counter()
        await asyncio.gather(*(b.take() for _ in range(41)))
        took = time.perf_counter() - t0
        assert 1.6 <= took <= 3.5, took                         # 40 tokens beyond the burst at 20/s ~ 2 s
        await HostBudget.ensure(pool, "share.test", rate_rps=20, burst=4)
        low = HostBudget(pool, "share.test", share=0.25)         # a 25% class pays 4 tokens per request
        t0 = time.perf_counter()
        for _ in range(5):
            await low.take()
        took = time.perf_counter() - t0
        assert 0.7 <= took <= 1.6, took                          # (5*4 - 4 burst) / 20 per s = 0.8 s -> 5 req/s
        await HostBudget.ensure(pool, "tiny.test", rate_rps=20, burst=1)
        await asyncio.wait_for(HostBudget(pool, "tiny.test", share=0.25).take(), 2)   # never stuck when burst < cost
        await b.pause(60, "challenged by akamai")
        with pytest.raises(Paused):
            await b.take()
        await pool.close()
    asyncio.run(go())
