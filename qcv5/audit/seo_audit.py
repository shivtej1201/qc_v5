"""
QC V5 — SEO and technical-SEO deep-dive checks.

Two layers, both free of extra HTTP requests (validation speed is unchanged):

* page_facts(): turns signals the single-pass Parser already collected (title, meta, headings,
  robots directives, JSON-LD, Open Graph, hreflang, visible text, insecure resources) plus the
  fetch result (X-Robots-Tag, final URL) into ONE compact row per sitemap page
  (table on_page_seo_pages, written in the same transaction as the page itself).
* page_issues() / cross-page checks in the CSV packager: issue_type + severity + detail, with the
  recommended action in on_page_issue_rules.ACTIONS.

Thresholds follow common search-engine guidance (titles ~30-60 chars, descriptions ~70-160 chars,
one H1, self-referencing hreflang with x-default, no noindex pages in a sitemap, no mixed content).
"""
import json
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

from qcv5.crawler.url_normalizer import URLNormalizer

TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 160
THIN_CONTENT_WORDS = 200
URL_MAX_LEN = 115
SEO_CATEGORIES = ("ON_PAGE_SEO", "TECH_SEO", "CONTENT", "INTERNATIONAL", "URL_STRUCTURE", "SITEMAP")

# BCP 47 subset that search engines accept: language (ISO 639-1/2), optional script, optional region.
HREFLANG_RE = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.IGNORECASE)
COMMON_BAD_REGIONS = {"uk": "gb"}      # en-UK is invalid (ISO 3166 uses GB)

SEO_COLUMNS = [
    "job_id", "source_page_url", "final_url", "title", "title_length", "title_count", "meta_description",
    "meta_description_length", "meta_description_count", "h1_count", "h1_text", "h2_count", "heading_skips",
    "meta_robots", "x_robots_tag", "indexable", "noindex", "nofollow", "html_lang", "has_viewport", "has_charset",
    "meta_refresh", "canonical_outside_head", "jsonld_blocks", "jsonld_invalid", "schema_types", "og_title",
    "og_description", "og_image", "twitter_card", "word_count", "content_hash", "hreflang_count", "hreflang_json",
    "hreflang_x_default", "insecure_resources", "insecure_examples", "internal_links", "external_links",
    "internal_nofollow_links", "url_length", "checked_at", "script_count", "js_rendering_suspected",
]
JS_SHELL_MAX_WORDS = 50


def js_rendering_suspected(word_count: int, script_count: int, spa_root: str, internal_links: int) -> bool:
    """HTML that is an app shell: almost no visible text or links, but an app mount point or many scripts.
    Such pages need a rendered (browser) check; the HTTP crawl alone under-reports their content and links."""
    return word_count < JS_SHELL_MAX_WORDS and internal_links <= 2 and (bool(spa_root) or script_count >= 5)


def _robots_tokens(values: Iterable[str]) -> set:
    """Directive tokens from meta robots values and X-Robots-Tag (which may be 'googlebot: noindex')."""
    toks = set()
    for v in values:
        for part in str(v or "").lower().split(","):
            part = part.strip()
            if ":" in part and not part.startswith(("max-", "unavailable_after")):
                part = part.split(":", 1)[1].strip()   # 'googlebot: noindex' -> 'noindex'
            if part:
                toks.add(part)
    if "none" in toks:
        toks |= {"noindex", "nofollow"}
    return toks


def _schema_types(blocks: List[str]) -> Tuple[List[str], int, str]:
    """(@type values, invalid block count, first parse error) for JSON-LD blocks."""
    types: List[str] = []
    invalid, first_err = 0, ""

    def walk(node: Any, depth: int = 0):
        if depth > 6 or len(types) >= 30:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            t = node.get("@type")
            for x in (t if isinstance(t, list) else [t]):
                if isinstance(x, str) and x and x not in types:
                    types.append(x[:60])
            if "@graph" in node:
                walk(node["@graph"], depth + 1)

    for b in blocks:
        txt = (b or "").strip()
        if not txt:
            invalid += 1
            first_err = first_err or "empty JSON-LD block"
            continue
        try:
            walk(json.loads(txt))
        except ValueError as e:
            invalid += 1
            first_err = first_err or f"{type(e).__name__}: {str(e)[:120]}"
    return types, invalid, first_err


def page_facts(job_id: str, src_url: str, final_url: str, parser: Any, rels: List[Dict[str, Any]],
               x_robots_tag: str, checked_at: str) -> Dict[str, Any]:
    """One SEO row for a parsed 2xx HTML page. Uses only what the single parse already produced."""
    title = " ".join((parser.title or "").split())
    desc = " ".join((parser.meta_desc or "").split())
    toks = _robots_tokens(list(parser.meta_robots) + [x_robots_tag])
    noindex, nofollow = int("noindex" in toks), int("nofollow" in toks)
    types, invalid, jerr = _schema_types(parser.jsonld_blocks)

    # hreflang: (code, normalized absolute target)
    hl = []
    for h in parser.hreflangs:
        absu = parser.resolve_url(h.get("href") or "")
        hl.append([str(h.get("hreflang") or "").strip()[:35], URLNormalizer.normalize(absu) if absu else ""])

    insecure = list(parser.insecure_resources)
    if (final_url or src_url).lower().startswith("https://"):
        for im in parser.all_on_page_images:
            s = str(im.get("src") or "").strip()
            if s.lower().startswith("http://") and len(insecure) < 50:
                insecure.append("image:" + s[:300])
    else:
        insecure = []   # mixed content only applies to HTTPS pages

    internal = external = internal_nofollow = 0
    for r in rels:
        lt = r.get("link_types") or []
        if not ("ANCHOR" in lt or "AREA" in lt or r.get("found_url_type") in ("ANCHOR", "AREA")):
            continue
        if r.get("found_url_scope") == "INTERNAL":
            internal += 1
            if "nofollow" in str(r.get("rel") or "").lower().split():
                internal_nofollow += 1
        elif r.get("found_url_scope") == "EXTERNAL":
            external += 1

    mp = parser.meta_props
    return {
        "job_id": job_id, "source_page_url": src_url, "final_url": final_url,
        "title": title[:300], "title_length": len(title), "title_count": parser.title_count,
        "meta_description": desc[:400], "meta_description_length": len(desc),
        "meta_description_count": parser.meta_desc_count,
        "h1_count": parser.heading_counts[1], "h1_text": " | ".join(parser.h1s)[:400],
        "h2_count": parser.heading_counts[2], "heading_skips": ",".join(parser.heading_skips),
        "meta_robots": " ; ".join(parser.meta_robots)[:300], "x_robots_tag": (x_robots_tag or "")[:300],
        "indexable": int(not noindex), "noindex": noindex, "nofollow": nofollow,
        "html_lang": parser.html_lang if parser.html_lang is not None else None,
        "has_viewport": int(parser.has_viewport), "has_charset": int(parser.has_charset),
        "meta_refresh": parser.meta_refresh, "canonical_outside_head": parser.canonical_outside_head,
        "jsonld_blocks": len(parser.jsonld_blocks), "jsonld_invalid": invalid,
        "schema_types": ",".join(types) + ((" | error: " + jerr) if jerr else ""),
        "og_title": mp.get("og:title", ""), "og_description": mp.get("og:description", ""),
        "og_image": mp.get("og:image", ""), "twitter_card": mp.get("twitter:card", ""),
        "word_count": parser.word_count, "content_hash": parser.content_hash,
        "hreflang_count": len(hl), "hreflang_json": json.dumps(hl[:200], separators=(",", ":")),
        "hreflang_x_default": int(any(c.lower() == "x-default" for c, _ in hl)),
        "insecure_resources": len(insecure), "insecure_examples": " ".join(insecure[:5])[:900],
        "internal_links": internal, "external_links": external, "internal_nofollow_links": internal_nofollow,
        "url_length": len(src_url), "checked_at": checked_at,
        "script_count": parser.script_count,
        "js_rendering_suspected": int(js_rendering_suspected(parser.word_count, parser.script_count,
                                                             parser.spa_root, internal)),
    }


def url_structure_issues(url: str) -> List[Tuple[str, str, str, str]]:
    out = []
    p = urllib.parse.urlsplit(url)
    path = p.path or ""
    if any(c.isupper() for c in path):
        out.append(("URL_STRUCTURE", "URL_HAS_UPPERCASE", "LOW", f"Path '{path[:120]}' has uppercase letters"))
    if "_" in path:
        out.append(("URL_STRUCTURE", "URL_HAS_UNDERSCORES", "INFO", "Path uses underscores instead of hyphens"))
    if "//" in path:
        out.append(("URL_STRUCTURE", "URL_DOUBLE_SLASH", "LOW", "Path contains '//'"))
    if p.query:
        out.append(("URL_STRUCTURE", "SITEMAP_URL_HAS_PARAMETERS", "LOW", f"Query string '?{p.query[:100]}' in a sitemap URL"))
    if len(url) > URL_MAX_LEN:
        out.append(("URL_STRUCTURE", "URL_TOO_LONG", "LOW", f"{len(url)} characters (> {URL_MAX_LEN})"))
    return out


def page_issues(f: Dict[str, Any], self_urls: Iterable[str]) -> List[Tuple[str, str, str, str]]:
    """Single-page SEO issues for one on_page_seo_pages row. self_urls = normalized forms of the page."""
    out: List[Tuple[str, str, str, str]] = []
    tl = int(f.get("title_length") or 0)
    if int(f.get("title_count") or 0) > 1:
        out.append(("ON_PAGE_SEO", "MULTIPLE_TITLE_TAGS", "MEDIUM", f"{f['title_count']} <title> elements"))
    if tl and tl < TITLE_MIN:
        out.append(("ON_PAGE_SEO", "TITLE_TOO_SHORT", "LOW", f"{tl} characters (< {TITLE_MIN}): '{f.get('title')}'"))
    elif tl > TITLE_MAX:
        out.append(("ON_PAGE_SEO", "TITLE_TOO_LONG", "LOW", f"{tl} characters (> {TITLE_MAX}); may be truncated in results"))
    dl = int(f.get("meta_description_length") or 0)
    if not dl:
        out.append(("ON_PAGE_SEO", "META_DESCRIPTION_MISSING", "MEDIUM", "No meta description (or it is empty)"))
    elif dl < DESC_MIN:
        out.append(("ON_PAGE_SEO", "META_DESCRIPTION_TOO_SHORT", "LOW", f"{dl} characters (< {DESC_MIN})"))
    elif dl > DESC_MAX:
        out.append(("ON_PAGE_SEO", "META_DESCRIPTION_TOO_LONG", "LOW", f"{dl} characters (> {DESC_MAX})"))
    if int(f.get("meta_description_count") or 0) > 1:
        out.append(("ON_PAGE_SEO", "MULTIPLE_META_DESCRIPTIONS", "LOW", f"{f['meta_description_count']} meta descriptions"))
    h1 = int(f.get("h1_count") or 0)
    h1_text = str(f.get("h1_text") or "")
    if h1 == 0:
        out.append(("ON_PAGE_SEO", "H1_MISSING", "MEDIUM", "No <h1> on the page"))
    elif h1 > 1:
        out.append(("ON_PAGE_SEO", "MULTIPLE_H1", "LOW", f"{h1} <h1> elements: {h1_text[:150]}"))
    elif not h1_text.strip(" |"):
        out.append(("ON_PAGE_SEO", "H1_EMPTY", "LOW", "<h1> has no text"))
    if f.get("heading_skips"):
        out.append(("ON_PAGE_SEO", "HEADING_LEVEL_SKIPPED", "INFO", f"Heading order skips levels: {f['heading_skips']}"))
    miss_og = [k for k in ("og_title", "og_description", "og_image") if not f.get(k)]
    if miss_og:
        out.append(("ON_PAGE_SEO", "OPEN_GRAPH_INCOMPLETE", "LOW",
                    "Missing " + ", ".join(k.replace("_", ":") for k in miss_og)))

    # technical SEO
    if int(f.get("noindex") or 0):
        src = "meta robots" if "noindex" in _robots_tokens([f.get("meta_robots", "")]) else "X-Robots-Tag header"
        out.append(("TECH_SEO", "NOINDEX_IN_SITEMAP", "HIGH", f"Sitemap page is noindex ({src})"))
    if int(f.get("nofollow") or 0):
        out.append(("TECH_SEO", "PAGE_NOFOLLOW", "MEDIUM", "Page-level nofollow: links on this page pass no signals"))
    if int(f.get("canonical_outside_head") or 0):
        out.append(("TECH_SEO", "CANONICAL_OUTSIDE_HEAD", "MEDIUM", "rel=canonical found in <body>; search engines ignore it there"))
    if f.get("meta_refresh"):
        out.append(("TECH_SEO", "META_REFRESH", "MEDIUM", f"<meta http-equiv=refresh content='{f['meta_refresh'][:80]}'>"))
    if f.get("html_lang") is None or str(f.get("html_lang")).strip() == "":
        out.append(("TECH_SEO", "HTML_LANG_MISSING", "LOW", "<html> has no lang attribute"))
    if not int(f.get("has_viewport") or 0):
        out.append(("TECH_SEO", "VIEWPORT_MISSING", "MEDIUM", "No <meta name=viewport>: page is not mobile-friendly"))
    if int(f.get("insecure_resources") or 0):
        out.append(("TECH_SEO", "MIXED_CONTENT", "HIGH",
                    f"{f['insecure_resources']} http:// resource(s) on an HTTPS page, e.g. {str(f.get('insecure_examples') or '')[:200]}"))
    if int(f.get("jsonld_invalid") or 0):
        err = str(f.get("schema_types") or "").split("| error:", 1)[-1].strip()
        out.append(("TECH_SEO", "STRUCTURED_DATA_INVALID", "MEDIUM",
                    f"{f['jsonld_invalid']} of {f['jsonld_blocks']} JSON-LD block(s) do not parse: {err[:150]}"))
    elif int(f.get("jsonld_blocks") or 0) == 0:
        out.append(("TECH_SEO", "STRUCTURED_DATA_MISSING", "INFO", "No JSON-LD structured data"))
    if int(f.get("internal_nofollow_links") or 0):
        out.append(("TECH_SEO", "INTERNAL_NOFOLLOW_LINKS", "LOW",
                    f"{f['internal_nofollow_links']} internal link(s) with rel=nofollow"))

    if int(f.get("js_rendering_suspected") or 0):
        out.append(("TECH_SEO", "JS_RENDERING_SUSPECTED", "MEDIUM",
                    f"HTML is an app shell ({f.get('word_count')} words, {f.get('script_count')} scripts): content and "
                    f"links are rendered by JavaScript, so the HTTP crawl under-reports them"))
    # content
    wc = int(f.get("word_count") or 0)
    if wc < THIN_CONTENT_WORDS:
        out.append(("CONTENT", "THIN_CONTENT", "LOW", f"{wc} words of visible text (< {THIN_CONTENT_WORDS})"))

    # international (hreflang, single page)
    try:
        hl = json.loads(f.get("hreflang_json") or "[]")
    except ValueError:
        hl = []
    if hl:
        bad = [c for c, _ in hl if not HREFLANG_RE.match(c)]
        bad += [c for c, _ in hl if HREFLANG_RE.match(c) and c.lower().split("-")[-1] in COMMON_BAD_REGIONS
                and "-" in c]
        if bad:
            out.append(("INTERNATIONAL", "HREFLANG_INVALID_CODE", "MEDIUM", "Invalid hreflang value(s): " + ", ".join(sorted(set(bad)))[:150]))
        targets = {u for _, u in hl}
        if not targets.intersection(set(self_urls)):
            out.append(("INTERNATIONAL", "HREFLANG_MISSING_SELF_REFERENCE", "LOW", "hreflang set does not include this page"))
        if not int(f.get("hreflang_x_default") or 0):
            out.append(("INTERNATIONAL", "HREFLANG_MISSING_X_DEFAULT", "INFO", "No hreflang=x-default"))
        by_code: Dict[str, set] = {}
        for c, u in hl:
            by_code.setdefault(c.lower(), set()).add(u)
        conflict = sorted(c for c, us in by_code.items() if len(us) > 1)
        if conflict:
            out.append(("INTERNATIONAL", "HREFLANG_CONFLICT", "MEDIUM", "Same hreflang points to different URLs: " + ", ".join(conflict)[:150]))
    return out


def hreflang_reciprocity(rows: Iterable[Tuple[str, str, str, str]]) -> List[Tuple[str, str, str]]:
    """rows: (source_page_url, normalized_source_url, normalized_final_url, hreflang_json).
    Returns (source_page_url, target_url, code) where the target is an audited page whose own
    hreflang set does not point back. Targets outside the audit are not judged."""
    rows = list(rows)
    page_of: Dict[str, int] = {}
    sets: List[set] = []
    pairs: List[List[Tuple[str, str]]] = []
    for i, (_, ns, nf, hj) in enumerate(rows):
        try:
            hl = [(c, u) for c, u in json.loads(hj or "[]") if u]
        except ValueError:
            hl = []
        pairs.append(hl)
        sets.append({u for _, u in hl})
        for k in (ns, nf):
            if k:
                page_of.setdefault(k, i)
    out = []
    for i, (src, ns, nf, _) in enumerate(rows):
        me = {ns, nf} - {""}
        for code, target in pairs[i]:
            j = page_of.get(target)
            if j is None or j == i:
                continue
            if not sets[j] or not (sets[j] & me):
                out.append((src, target, code))
    return out


LASTMOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?$")


def lastmod_state(value: str, now_iso: str) -> str:
    """'' (ok), 'MISSING', 'INVALID' (not W3C Datetime) or 'FUTURE' (more than a day ahead)."""
    v = (value or "").strip()
    if not v:
        return "MISSING"
    if not LASTMOD_RE.match(v):
        return "INVALID"
    return "FUTURE" if v[:10] > _add_day(now_iso[:10]) else ""


def _add_day(d: str) -> str:
    import datetime as _dt
    try:
        return (_dt.date.fromisoformat(d) + _dt.timedelta(days=1)).isoformat()
    except ValueError:
        return d
