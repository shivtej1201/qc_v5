"""
Deterministic synthetic site model shaped after the real Bajaj Mall on-page run
(docs/reports/bajaj_mall_on_page_final): ~36 raw URL occurrences per source page,
1 canonical (self), 1 external iframe shared by all pages, mostly-internal anchors
whose destinations are overwhelmingly other sitemap source pages (~97% reuse),
plus a small pool of non-source internal destinations (~6% of sources) and a few
external URLs. Page weight ~150-200 KB with a large inline script blob, similar to
a server-rendered PDP.
"""
import random

NAV_COUNT = 12          # global header/footer nav links (shared on every page)
RELATED_COUNT = 20      # links to other product (source) pages
EXTRA_DEST_RATIO = 0.06 # non-source internal destinations, relative to source count
EXTERNAL_POOL = 25


def source_path(i: int) -> str:
    return f"/p/{i}"


def extra_path(k: int) -> str:
    return f"/d/{k}"


def page_html(i: int, n_sources: int, primary_base: str, external_base: str) -> str:
    rnd = random.Random(i * 7919 + 13)
    n_extra = max(1, int(n_sources * EXTRA_DEST_RATIO))
    nav = [source_path(k) for k in range(min(NAV_COUNT, n_sources))]
    related = [source_path(rnd.randrange(n_sources)) for _ in range(RELATED_COUNT)]
    extras = [extra_path(rnd.randrange(n_extra)) for _ in range(2)]
    ext = [f"{external_base}/ext/{rnd.randrange(EXTERNAL_POOL)}"]

    blocks = []
    # Heavy, realistic markup: product grid / spec tables / inline JSON state
    for b in range(120):
        blocks.append(
            f'<div class="pdp-block block-{b}" data-idx="{b}"><span class="lbl">Spec {b}</span>'
            f'<span class="val">Value {rnd.randrange(10**6)}</span><ul>'
            + "".join(f'<li class="it">Feature {b}-{x} lorem ipsum dolor sit amet</li>' for x in range(6))
            + "</ul></div>"
        )
    script_blob = "window.__STATE__=" + "{" + ",".join(f'"k{x}":"{"v"*40}{x}"' for x in range(1500)) + "};"
    nav_html = "".join(f'<a href="{primary_base}{u}">Nav {j}</a>' for j, u in enumerate(nav))
    rel_html = "".join(f'<a href="{primary_base}{u}">Related {j}</a>' for j, u in enumerate(related))
    extra_html = "".join(f'<a href="{u}">More {j}</a>' for j, u in enumerate(extras))
    ext_html = "".join(f'<a href="{u}" rel="noopener">Partner</a>' for u in ext)
    return (
        "<!doctype html><html><head>"
        f"<title>Product {i}</title>"
        '<meta name="tag-1" content="PDP"><meta name="tag-2" content="CD">'
        f'<link rel="canonical" href="{primary_base}{source_path(i)}">'
        f"<script>{script_blob}</script>"
        "</head><body>"
        f"<header><nav>{nav_html}</nav></header>"
        f'<main><h1>Product {i}</h1>{"".join(blocks)}<section class="related">{rel_html}{extra_html}{ext_html}</section>'
        f'<iframe src="{external_base}/widget" title="widget"></iframe></main>'
        f"<footer>{nav_html[:200]}</footer>"
        "</body></html>"
    )
