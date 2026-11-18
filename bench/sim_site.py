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


CHROME_IMAGES = 15        # site chrome (logo, icons, badges) shared by every page
GALLERY_IMAGES = 3        # product images unique to the page, each with a 2-candidate srcset


def image_html(i: int, cdn_base: str, primary_base: str) -> str:
    """Realistic PDP image markup: chrome icons (shared), a <picture> hero, a srcset gallery,
    lazy-loaded thumbnails, og:image/twitter:image, a few broken / mislabelled images."""
    chrome = "".join(f'<img src="{cdn_base}/img/chrome/icon-{k}.svg" alt="icon {k}" width="24" height="24">'
                     for k in range(CHROME_IMAGES))
    gallery = "".join(
        f'<img src="{cdn_base}/img/p/{i}/g{g}-400.webp" srcset="{cdn_base}/img/p/{i}/g{g}-400.webp 400w, '
        f'{cdn_base}/img/p/{i}/g{g}-800.webp 800w" alt="Product {i} view {g}" loading="lazy">'
        for g in range(GALLERY_IMAGES))
    picture = (f'<picture><source srcset="{cdn_base}/img/p/{i}/g0-800.webp 1x" type="image/webp">'
               f'<img src="{cdn_base}/img/p/{i}/g0-400.webp" alt=""></picture>')
    lazy = f'<img data-src="{cdn_base}/img/p/{i}/g1-400.webp" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
    extra = ""
    if i % 50 == 0:
        extra += f'<img src="{cdn_base}/img/missing/{i}.jpg" alt="missing">'     # 404 image
    if i % 97 == 0:
        extra += f'<img src="{primary_base}/content/dam/fake-{i}.png">'          # served as text/html
    return chrome + picture + gallery + lazy + extra


def image_meta(i: int, cdn_base: str) -> str:
    return (f'<meta property="og:image" content="{cdn_base}/img/p/{i}/g0-800.webp">'
            f'<meta name="twitter:image" content="{cdn_base}/img/p/{i}/g0-800.webp">')


def page_html(i: int, n_sources: int, primary_base: str, external_base: str, cdn_base: str = "") -> str:
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
        + (image_meta(i, cdn_base) if cdn_base else "") +
        f"<script>{script_blob}</script>"
        "</head><body>"
        f"<header><nav>{nav_html}</nav></header>"
        f'<main><h1>Product {i}</h1>{"".join(blocks)}<section class="related">{rel_html}{extra_html}{ext_html}</section>'
        f'<iframe src="{external_base}/widget" title="widget"></iframe>'
        + (image_html(i, cdn_base, primary_base) if cdn_base else "") + "</main>"
        f"<footer>{nav_html[:200]}</footer>"
        "</body></html>"
    )
