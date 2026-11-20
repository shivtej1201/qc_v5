"""
QC V5 on-page audit — issue rules.

Turns validated data (source pages, URL relationships, image relationships) into ACTIONABLE issues:
issue_type + severity + plain-language detail + recommended action. Pure functions over values
already in SQLite: no extra HTTP requests, so detailed reporting never slows validation.

Severity: HIGH (broken for users/search engines, fix first) > MEDIUM (wrong or risky, fix soon)
          > LOW (hygiene / wasted hops) > INFO (needs a human look, may be intentional).
"""
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from qcv5.crawler.url_normalizer import URLNormalizer

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
SLOW_RESPONSE_MS = 3000.0
LARGE_IMAGE_BYTES = 300 * 1024
SOFT_404_TITLE = re.compile(
    r"(\b404\b|page not found|not found|does not exist|no longer available|page unavailable|"
    r"पृष्ठ नहीं मिला)", re.IGNORECASE)

Issue = Tuple[str, str, str]   # (issue_type, severity, detail)

# Status issue types whose destination counts as "broken" in 07/09 (must mirror BROKEN_SQL exactly)
BROKEN_STATUS_TYPES = frozenset({
    "NOT_FOUND", "GONE", "ACCESS_DENIED", "CLIENT_ERROR", "SERVER_ERROR", "REDIRECT_TO_BROKEN",
    "TIMEOUT", "DNS_FAILURE", "CONNECTION_FAILED", "NETWORK_ERROR", "SSL_ERROR", "ROBOTS_BLOCKED",
    "WAF_BLOCKED", "RATE_LIMITED", "UNSAFE_DESTINATION", "REDIRECT_LOOP", "BROKEN_REDIRECT", "VALIDATION_ERROR",
})
BROKEN_IMAGE_TYPES = frozenset({"IMAGE_" + t for t in BROKEN_STATUS_TYPES} | {
    "IMAGE_CONTENT_TYPE_MISMATCH", "IMAGE_NOT_AN_IMAGE", "IMAGE_EMPTY"})

ACTIONS: Dict[str, str] = {
    # status of a link / canonical / hreflang / iframe destination
    "NOT_FOUND": "Destination returns 404. Update the link to a live URL, or remove it; if the page moved, add a 301 to its replacement.",
    "GONE": "Destination returns 410 (permanently removed). Remove or replace the link.",
    "ACCESS_DENIED": "Origin returned 401/403 (not a CDN/WAF block). Check whether the page should be public; otherwise stop linking to it.",
    "CLIENT_ERROR": "Destination returns a 4xx error. Fix the URL or the target page.",
    "SERVER_ERROR": "Destination returns 5xx. Check origin/application logs for the URL; re-run the audit after the fix.",
    "REDIRECT_TO_BROKEN": "Link redirects to an error page. Fix the redirect target or point the link to a live URL.",
    "REDIRECTED": "Link goes through a redirect. Update the href to the final URL to save a hop.",
    "REDIRECT_CHAIN": "Link passes through 2+ redirects. Point it at the final URL and collapse the chain to one hop.",
    "REDIRECT_UNRESOLVED": "Redirect did not resolve to a final page. Inspect the redirect rules for this URL.",
    "BROKEN_REDIRECT": "Redirect is broken (no Location header or a non-HTTP target). Fix the redirect rule.",
    "REDIRECT_LOOP": "Redirect loop / too many hops. Fix the redirect rules so the URL resolves.",
    "TIMEOUT": "Destination did not respond in time. Check server performance, then re-validate.",
    "DNS_FAILURE": "Hostname does not resolve. Fix the domain in the link (typo or retired domain) or remove it.",
    "CONNECTION_FAILED": "Server refused/dropped the connection. Check the host is up and reachable.",
    "NETWORK_ERROR": "Transport error while fetching. Re-validate; if it persists, check the host.",
    "SSL_ERROR": "TLS/SSL handshake failed. Fix the certificate/TLS configuration or link to a working URL.",
    "ROBOTS_BLOCKED": "robots.txt disallows this URL, so it was not requested (status unknown). Confirm the block is intended.",
    "WAF_BLOCKED": "CDN/WAF (e.g. Akamai) blocked the crawler, so this is NOT the origin's status. Re-verify from an allow-listed IP before acting.",
    "RATE_LIMITED": "Rate limited (429) by the edge/origin; status unknown. Re-validate at a lower request rate.",
    "UNSAFE_DESTINATION": "URL resolves to a private/internal address and was blocked for security. Remove internal URLs from public pages.",
    "VALIDATION_ERROR": "The URL could not be validated. Check error_message and re-run.",
    "NOT_VALIDATED": "No validation result was recorded. Re-run the audit (resume) for this job.",
    # other link checks
    "INVALID_URL": "The href is malformed (bad host/port). Fix the markup.",
    "INSECURE_HTTP_LINK": "HTTPS page links to an http:// URL. Switch the link to https://.",
    # images
    "IMAGE_CONTENT_TYPE_MISMATCH": "URL looks like an image but the server returns a non-image Content-Type (often an error/HTML page). Fix the asset path or server mapping.",
    "IMAGE_NOT_AN_IMAGE": "Image URL returns a non-image Content-Type. Point the <img> at a real image file.",
    "IMAGE_EMPTY": "Image returns an empty body (Content-Length 0). Re-upload or fix the asset.",
    "IMAGE_REDIRECTED": "Image is served through a redirect. Reference the final image URL directly.",
    "MISSING_ALT": "<img> has no alt attribute. Add a descriptive alt (or alt=\"\" if purely decorative).",
    "MIXED_CONTENT_IMAGE": "HTTPS page loads an http:// image (mixed content; browsers may block it). Serve it over https://.",
    "LARGE_IMAGE": "Image is larger than 300 KB. Compress/resize or serve a modern format (WebP/AVIF).",
    "INVALID_IMAGE_URL": "Image URL is malformed. Fix the src/srcset value.",
    # source pages
    "SOURCE_NOT_FOUND": "Sitemap lists a URL that returns 404/410. Remove it from the sitemap or restore the page.",
    "SOURCE_ACCESS_DENIED": "Sitemap URL returns 401/403 from the origin. Remove non-public URLs from the sitemap.",
    "SOURCE_CLIENT_ERROR": "Sitemap URL returns a 4xx error. Fix the page or drop it from the sitemap.",
    "SOURCE_SERVER_ERROR": "Sitemap URL returns 5xx. Check origin/application logs.",
    "SOURCE_TIMEOUT": "Sitemap URL timed out. Check page performance; re-run to confirm.",
    "SOURCE_FETCH_FAILED": "Sitemap URL could not be fetched (DNS/connection/TLS). See error_message.",
    "SOURCE_ROBOTS_BLOCKED": "Sitemap lists a URL that robots.txt disallows. Remove it from the sitemap or allow it in robots.txt.",
    "SOURCE_WAF_BLOCKED": "CDN/WAF blocked the crawler on this page; its content was not audited. Re-run from an allow-listed IP.",
    "SOURCE_UNSAFE": "Sitemap URL resolves to a private/internal address. Remove it from the sitemap.",
    "SOURCE_BROKEN_REDIRECT": "Sitemap URL has a broken redirect. Fix the redirect rule.",
    "SITEMAP_URL_REDIRECTS": "Sitemap URL redirects. List the final URL in the sitemap instead.",
    "SITEMAP_URL_NOT_CANONICAL": "Page's canonical points to a different URL. Sitemaps should list canonical URLs only; list the canonical or fix the tag.",
    "MULTIPLE_CANONICALS": "Page declares more than one canonical URL. Keep exactly one.",
    "MISSING_CANONICAL": "Page has no rel=canonical. Add a self-referencing canonical.",
    "SOFT_404": "Page returns 200 but its title says 'not found'. Return a real 404/410 or restore the content.",
    "MISSING_TITLE": "Page has no <title>. Add a descriptive title.",
    "PAGE_TRUNCATED": "Page HTML exceeded the size limit; links/images after the limit were NOT audited. Raise --max-source-bytes or reduce page weight.",
    "NO_LINKS_EXTRACTED": "HTML page yielded no links. It may be client-side rendered or empty; check the page.",
    "NON_HTML_SOURCE": "Sitemap URL is not an HTML page (Content-Type). Confirm it belongs in the page sitemap.",
    "SLOW_RESPONSE": "Page took more than 3 s to respond. Investigate server/CDN performance.",
    "CLASSIFICATION_NEEDS_REVIEW": "Business/page type could not be determined from tags, data-pageType or URL. Add tag-1/tag-2 meta tags.",
    # ---- SEO / technical SEO (seo_audit.py + cross-page checks in the packager)
    "TITLE_TOO_SHORT": "Title is short. Write a specific 30-60 character title with the page's main topic first.",
    "TITLE_TOO_LONG": "Title is over ~60 characters and will be truncated in results. Shorten it; keep the key phrase first.",
    "MULTIPLE_TITLE_TAGS": "Page has more than one <title>. Keep exactly one in <head> (check templates/plugins injecting a second).",
    "DUPLICATE_TITLE": "Several pages share this title. Give each page a unique title that describes its own content.",
    "META_DESCRIPTION_MISSING": "Add a unique 70-160 character meta description summarizing the page (it is often the search snippet).",
    "META_DESCRIPTION_TOO_SHORT": "Meta description is short. Expand to 70-160 characters with a clear value proposition.",
    "META_DESCRIPTION_TOO_LONG": "Meta description exceeds ~160 characters and will be cut. Tighten it.",
    "MULTIPLE_META_DESCRIPTIONS": "More than one meta description. Keep exactly one.",
    "DUPLICATE_META_DESCRIPTION": "Several pages share this meta description. Write a unique description per page.",
    "H1_MISSING": "Add one visible <h1> that states the page topic.",
    "MULTIPLE_H1": "Use a single <h1> for the main topic; demote the others to <h2>/<h3>.",
    "H1_EMPTY": "The <h1> has no text (e.g. only an image). Add descriptive heading text.",
    "DUPLICATE_H1": "Several pages share this H1. Make each page's main heading specific to that page.",
    "HEADING_LEVEL_SKIPPED": "Heading levels skip (e.g. h2 -> h4). Use sequential levels for accessibility and structure.",
    "OPEN_GRAPH_INCOMPLETE": "Add og:title, og:description and og:image so shares on social/messaging apps render a rich preview.",
    "NOINDEX_IN_SITEMAP": "Page is noindex but listed in the sitemap: conflicting signals. Remove it from the sitemap, or remove noindex if it should rank.",
    "PAGE_NOFOLLOW": "Page-level nofollow stops link signals flowing to linked pages. Remove it unless intentional.",
    "CANONICAL_OUTSIDE_HEAD": "rel=canonical is in <body>, where search engines ignore it. Move it into <head>.",
    "META_REFRESH": "Meta refresh redirect. Replace with a server-side 301 redirect.",
    "HTML_LANG_MISSING": "Add <html lang=\"..\"> with the page language (accessibility + language targeting).",
    "VIEWPORT_MISSING": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> (mobile-first indexing).",
    "MIXED_CONTENT": "HTTPS page loads http:// resources (blocked or flagged by browsers). Switch them to https://.",
    "STRUCTURED_DATA_INVALID": "JSON-LD does not parse, so search engines ignore it. Fix the syntax (validate with a Rich Results test).",
    "STRUCTURED_DATA_MISSING": "No structured data. Consider schema.org markup suited to the page (Organization, BreadcrumbList, FAQPage, Product...).",
    "INTERNAL_NOFOLLOW_LINKS": "Internal links with rel=nofollow waste internal link equity. Remove nofollow from internal links.",
    "JS_RENDERING_SUSPECTED": "Serve the main content and links in the initial HTML (SSR/prerendering), or verify with a rendered crawl: search engines and this HTTP audit see an almost empty page.",
    "THIN_CONTENT": "Little visible text. Expand with useful, unique content, or noindex/consolidate the page if it has no search value.",
    "DUPLICATE_CONTENT": "Visible text is identical to other audited pages. Consolidate, differentiate, or canonicalize to one version.",
    "HREFLANG_INVALID_CODE": "Use valid ISO 639-1 language (+ optional ISO 3166-1 region) codes, e.g. en-GB not en-UK; x-default for the fallback.",
    "HREFLANG_MISSING_SELF_REFERENCE": "Each page's hreflang set must include a self-referencing entry. Add one for this URL.",
    "HREFLANG_MISSING_X_DEFAULT": "Add hreflang=\"x-default\" pointing to the language selector / default version.",
    "HREFLANG_CONFLICT": "One hreflang code points to several URLs. Keep exactly one URL per language-region.",
    "HREFLANG_NOT_RECIPROCAL": "The alternate page does not link back. hreflang must be bidirectional: add the return link on the target page.",
    "URL_HAS_UPPERCASE": "Use lowercase URLs; uppercase variants create duplicates on case-sensitive servers.",
    "URL_HAS_UNDERSCORES": "Prefer hyphens to separate words in URLs.",
    "URL_DOUBLE_SLASH": "Remove the duplicate slash from the path (and 301 the old form).",
    "SITEMAP_URL_HAS_PARAMETERS": "Sitemaps should list clean canonical URLs. Remove parameterized URLs or list their canonical.",
    "URL_TOO_LONG": "Shorten the URL to a concise, readable path.",
    "TRAILING_SLASH_DUPLICATE": "Both slash and no-slash versions are in the sitemap. Pick one form, 301 the other, list only one.",
    "ORPHAN_PAGE": "No audited page links to this URL. Add internal links from relevant pages (only sitemap/search can discover it now).",
    "NOT_IN_SITEMAP": "Internal 200 HTML page linked from the site but missing from the sitemap. Add it, or noindex/redirect it if it should not rank.",
    "SITEMAP_FETCH_FAILED": "The sitemap file could not be fetched or parsed, so its URLs are not discoverable. Fix the URL/server/XML.",
    "SITEMAP_TOO_LARGE": "A sitemap file is limited to 50,000 URLs / 50 MB uncompressed. Split it and reference the parts from a sitemap index.",
    "SITEMAP_DUPLICATE_URLS": "The sitemap repeats URLs (here or in another sitemap). List each URL once.",
    "SITEMAP_LASTMOD_MISSING": "Add <lastmod> (W3C date) so crawlers can prioritize changed pages.",
    "SITEMAP_LASTMOD_INVALID": "<lastmod> is not a W3C Datetime (YYYY-MM-DD or full ISO 8601 with timezone). Fix the format.",
    "SITEMAP_LASTMOD_FUTURE": "<lastmod> is in the future. Set it to the page's real last modification date.",
}

ELEMENT_CATEGORY = {"CANONICAL": "CANONICAL", "HREFLANG": "HREFLANG", "IFRAME": "IFRAME",
                    "SCRIPT": "RESOURCE", "STYLESHEET": "RESOURCE", "LINK_RESOURCE": "RESOURCE",
                    "CSS_RESOURCE": "RESOURCE", "MEDIA": "RESOURCE", "EMBED": "RESOURCE",
                    "SOCIAL_URL": "METADATA_URL", "JSONLD_URL": "METADATA_URL", "META_REFRESH": "LINK",
                    "FORM": "FORM"}
# categories whose broken status issues mirror 07_broken_urls.csv (trust gate)
LINK_STATUS_CATEGORIES = frozenset({"LINK", "CANONICAL", "HREFLANG", "IFRAME", "RESOURCE", "METADATA_URL", "FORM"})

CANONICAL_ACTIONS = {
    "REDIRECTED": "Canonical points to a redirecting URL. Point rel=canonical at the final 200 URL.",
    "REDIRECT_CHAIN": "Canonical points through a redirect chain. Point rel=canonical at the final 200 URL.",
    "NOT_FOUND": "Canonical points to a 404 page. Point rel=canonical at the live canonical URL.",
    "INSECURE_HTTP_LINK": "Canonical uses http://. Use the https:// canonical URL.",
}
HREFLANG_ACTIONS = {
    "REDIRECTED": "hreflang alternate redirects. Reference the final URL of the language version.",
    "NOT_FOUND": "hreflang alternate returns 404. Fix the language-version URL or remove the alternate.",
}


IMAGE_STATUS_ACTIONS = {
    "NOT_FOUND": "Image returns 404. Re-upload the asset or fix the src/srcset path on the page.",
    "GONE": "Image returns 410. Replace the image reference on the page.",
    "REDIRECT_TO_BROKEN": "Image URL redirects to an error. Fix the asset path or the redirect.",
    "SERVER_ERROR": "Image server returns 5xx. Check the CDN/asset origin, then re-validate.",
}


def action_for(issue_type: str, category: str) -> str:
    if category == "CANONICAL" and issue_type in CANONICAL_ACTIONS:
        return CANONICAL_ACTIONS[issue_type]
    if category == "HREFLANG" and issue_type in HREFLANG_ACTIONS:
        return HREFLANG_ACTIONS[issue_type]
    if issue_type in ACTIONS:
        return ACTIONS[issue_type]
    if issue_type.startswith("IMAGE_"):
        base = issue_type[len("IMAGE_"):]
        if base in IMAGE_STATUS_ACTIONS:
            return IMAGE_STATUS_ACTIONS[base]
        if base in ACTIONS:   # same transport/status meaning, applied to the image URL
            return "Image: " + ACTIONS[base]
    return "Review this item."


def _status_issue(bucket: Optional[str], code: Optional[int], initial: Optional[int], hops: Optional[int],
                  error_type: str, classification: str, state: Optional[str]) -> Optional[Issue]:
    """Exactly one status issue per validated destination (or None when it is healthy 2xx)."""
    hops = int(hops or 0)
    error_type = error_type or ""
    classification = classification or ""
    if state in (None, "", "PENDING") or bucket in (None, "", "PENDING"):
        return ("NOT_VALIDATED", "HIGH", "No terminal validation state recorded")
    if bucket == "2xx":
        return None
    if bucket == "3xx":
        if code is None or code >= 400:
            return ("REDIRECT_TO_BROKEN", "HIGH", f"{initial} redirect -> final {code if code is not None else 'no response'} after {hops} hop(s)")
        if 300 <= code < 400:
            return ("REDIRECT_UNRESOLVED", "MEDIUM", f"{initial} redirect ended on another {code}")
        if hops >= 2:
            return ("REDIRECT_CHAIN", "MEDIUM", f"{hops} redirect hops ({initial} ... -> {code})")
        return ("REDIRECTED", "LOW", f"{initial} redirect -> {code}")
    if bucket == "4xx":
        if code == 404:
            return ("NOT_FOUND", "HIGH", "HTTP 404 Not Found")
        if code == 410:
            return ("GONE", "HIGH", "HTTP 410 Gone")
        if code in (401, 403):
            return ("ACCESS_DENIED", "MEDIUM", f"HTTP {code} from origin (not classified as WAF)")
        return ("CLIENT_ERROR", "HIGH", f"HTTP {code}")
    if bucket == "5xx":
        return ("SERVER_ERROR", "HIGH", f"HTTP {code}")
    if bucket == "Timeout":
        return ("TIMEOUT", "MEDIUM", "No response within the read timeout (after retries)")
    if bucket == "DNS/Network":
        if error_type == "DNS Error":
            return ("DNS_FAILURE", "HIGH", "Hostname did not resolve")
        if error_type == "Connection Error":
            return ("CONNECTION_FAILED", "HIGH", "Connection refused/failed")
        return ("NETWORK_ERROR", "MEDIUM", error_type or "Transport error")
    if bucket == "SSL":
        return ("SSL_ERROR", "HIGH", "TLS/SSL handshake failed")
    if bucket == "Robots blocked":
        return ("ROBOTS_BLOCKED", "INFO", "Disallowed by robots.txt; not requested")
    if bucket == "WAF blocked":
        if code == 429:
            return ("RATE_LIMITED", "MEDIUM", "HTTP 429 from edge/origin after retries")
        return ("WAF_BLOCKED", "MEDIUM", f"HTTP {code} classified as CDN/WAF block")
    # "Other errors"
    if classification == "Blocked (SSRF)":
        return ("UNSAFE_DESTINATION", "MEDIUM", "Resolves to a private/internal address; blocked")
    if classification == "Redirect Loop":
        return ("REDIRECT_LOOP", "HIGH", "Redirect loop or too many hops")
    if classification in ("Redirect Without Location", "Invalid Redirect Target"):
        return ("BROKEN_REDIRECT", "HIGH", classification)
    return ("VALIDATION_ERROR", "MEDIUM", classification or error_type or "Unknown error")


def _scheme(u: str) -> str:
    return urllib.parse.urlsplit(u or "").scheme.lower()


def link_issues(r: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    """Issues for one URL relationship. Returns [(category, issue_type, severity, detail)]."""
    scope = r["internal_external"]
    element = r["element_type"] or "ANCHOR"
    category = ELEMENT_CATEGORY.get(element, "LINK")
    out: List[Tuple[str, str, str, str]] = []
    if scope == "INVALID":
        return [(category, "INVALID_URL", "MEDIUM", f"Malformed href: {str(r['found_url'])[:120]}")]
    if scope not in ("INTERNAL", "EXTERNAL"):
        return out          # mailto:/tel:/fragment: recorded in 03, not an issue
    if r.get("base_validation_source") == "NOT_REQUESTED" and not r.get("validation_state"):
        return out          # form action: recorded, deliberately not requested
    st = _status_issue(r["status_bucket"], r["http_status"], r["initial_status"], r["hop_count"],
                       r["error_type"], r["status_classification"], r["validation_state"])
    if st:
        t, sev, detail = st
        if category == "CANONICAL" and t in ("REDIRECTED", "REDIRECT_CHAIN"):
            sev = "MEDIUM"          # a canonical must point at a 200 URL
        if t == "VALIDATION_ERROR" and r.get("error_message"):
            detail = f"{detail}: {str(r['error_message'])[:160]}"
        out.append((category, t, sev, detail))
    if _scheme(r["source_page_url"]) == "https" and _scheme(r["normalized_url"]) == "http":
        out.append((category, "INSECURE_HTTP_LINK", "MEDIUM" if category in ("CANONICAL", "HREFLANG") else "LOW",
                    "https page links to http:// URL"))
    return out


def image_issues(r: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    out: List[Tuple[str, str, str, str]] = []
    itype = r["image_type"]
    if itype == "INVALID_IMAGE":
        return [("IMAGE", "INVALID_IMAGE_URL", "MEDIUM", f"Malformed image URL: {str(r['image_url'])[:120]}")]
    # Alt is a property of the <img> TAG, not of every URL it references: flag it once per real image
    # source (src / lazy data-* attribute), never for srcset candidates or data: placeholders.
    sources = [a for a in str(r.get("attribute_source") or "").split("|") if a]
    primary_src = any(a.startswith("img.") and not a.endswith("srcset") for a in sources)
    if primary_src and itype in ("HTTP_IMAGE", "HTTPS_IMAGE") and str(r.get("alt_present")) == "false":
        out.append(("IMAGE_SEO", "MISSING_ALT", "MEDIUM", "No alt attribute on <img>"))
    if itype not in ("HTTP_IMAGE", "HTTPS_IMAGE"):
        return out          # data: URIs etc.: recorded in 05, nothing to validate
    issue = str(r.get("image_issue") or "")
    if issue:
        kind = issue.split(":", 1)[0]
        t = {"CONTENT_TYPE_MISMATCH": "IMAGE_CONTENT_TYPE_MISMATCH", "NOT_AN_IMAGE_CONTENT_TYPE": "IMAGE_NOT_AN_IMAGE",
             "EMPTY_IMAGE": "IMAGE_EMPTY"}.get(kind, "IMAGE_NOT_AN_IMAGE")
        out.append(("IMAGE", t, "HIGH", issue[:200]))
    else:
        st = _status_issue(r["status_bucket"], r["http_status"], r["initial_status"], r["hop_count"],
                           r["error_type"], r["status_classification"], r["validation_state"])
        if st:
            t, sev, detail = st
            if t in ("REDIRECTED", "REDIRECT_CHAIN"):
                out.append(("IMAGE", "IMAGE_REDIRECTED", "LOW", detail))
            else:
                out.append(("IMAGE", "IMAGE_" + t, sev, detail))
    if _scheme(r["source_page_url"]) == "https" and itype == "HTTP_IMAGE":
        out.append(("IMAGE", "MIXED_CONTENT_IMAGE", "MEDIUM", "https page loads http:// image"))
    try:
        cl = int(r.get("content_length") or 0)
    except (TypeError, ValueError):
        cl = 0
    if cl > LARGE_IMAGE_BYTES:
        out.append(("IMAGE", "LARGE_IMAGE", "LOW", f"{cl // 1024} KB"))
    return out


def source_issues(r: Dict[str, Any], canon: Optional[Tuple[int, str]], anchors: int = 0) -> List[Tuple[str, str, str, str]]:
    """Issues for one sitemap source page. canon = (distinct canonical count, one canonical URL);
    anchors = number of <a>/<area> link relationships (canonical/hreflang/meta links do not count)."""
    out: List[Tuple[str, str, str, str]] = []
    state = r["crawl_state"]
    code = r["http_status"]
    err = r.get("error_type") or ""
    remark = r.get("remark") or ""
    if state == "PENDING":
        return [("SOURCE_PAGE", "NOT_VALIDATED", "HIGH", "Source page was never fetched")]
    if state == "BLOCKED":
        if "ROBOTS" in err or "robots" in remark.lower():
            return [("SOURCE_PAGE", "SOURCE_ROBOTS_BLOCKED", "MEDIUM", "Disallowed by robots.txt")]
        if "SSRF" in err or "SSRF" in remark:
            return [("SOURCE_PAGE", "SOURCE_UNSAFE", "MEDIUM", "Private/internal address")]
        return [("SOURCE_PAGE", "SOURCE_WAF_BLOCKED", "MEDIUM", f"HTTP {code} WAF/CDN block")]
    if state == "TIMED_OUT":
        return [("SOURCE_PAGE", "SOURCE_TIMEOUT", "HIGH", "No response within the read timeout")]
    if state == "FAILED":
        if err in ("Redirect Without Location", "Invalid Redirect Target", "Redirect Loop"):
            return [("SOURCE_PAGE", "SOURCE_BROKEN_REDIRECT", "HIGH", err)]
        return [("SOURCE_PAGE", "SOURCE_FETCH_FAILED", "HIGH", f"{err}: {str(r.get('error_message') or '')[:140]}")]
    if state == "EXPLICITLY_EXCLUDED":
        return out
    # COMPLETED: an HTTP response was received
    if code is not None and code >= 400:
        if code in (404, 410):
            out.append(("SOURCE_PAGE", "SOURCE_NOT_FOUND", "HIGH", f"HTTP {code}"))
        elif code in (401, 403):
            out.append(("SOURCE_PAGE", "SOURCE_ACCESS_DENIED", "HIGH", f"HTTP {code}"))
        elif code >= 500:
            out.append(("SOURCE_PAGE", "SOURCE_SERVER_ERROR", "HIGH", f"HTTP {code}"))
        else:
            out.append(("SOURCE_PAGE", "SOURCE_CLIENT_ERROR", "HIGH", f"HTTP {code}"))
    if int(r.get("redirect_count") or 0) > 0:
        out.append(("SOURCE_PAGE", "SITEMAP_URL_REDIRECTS", "MEDIUM",
                    f"{r.get('initial_http_status')} -> {r.get('final_url')}"))
    if code is None or not (200 <= code <= 299):
        return out
    ctype = str(r.get("content_type") or "").lower()
    if ctype and "html" not in ctype:
        out.append(("SOURCE_PAGE", "NON_HTML_SOURCE", "INFO", f"Content-Type {ctype[:60]}"))
        return out
    if int(r.get("html_truncated") or 0):
        out.append(("SOURCE_PAGE", "PAGE_TRUNCATED", "HIGH", f"HTML larger than the limit ({r.get('html_bytes')} bytes read)"))
    if anchors == 0:
        out.append(("SOURCE_PAGE", "NO_LINKS_EXTRACTED", "MEDIUM",
                    f"No <a>/<area> links in the HTML ({int(r.get('url_relationships') or 0)} head/meta link(s) only)"))
    title = str(r.get("page_title") or "")
    if title and SOFT_404_TITLE.search(title):
        out.append(("SOURCE_PAGE", "SOFT_404", "HIGH", f"200 OK but title is '{title[:100]}'"))
    elif not title and int(r.get("html_bytes") or 0) > 0:
        out.append(("SOURCE_PAGE", "MISSING_TITLE", "LOW", "No <title> element"))
    n_canon, canon_url = canon or (0, "")
    if n_canon == 0:
        out.append(("SOURCE_PAGE", "MISSING_CANONICAL", "LOW", "No rel=canonical"))
    elif n_canon > 1:
        out.append(("SOURCE_PAGE", "MULTIPLE_CANONICALS", "MEDIUM", f"{n_canon} different canonical URLs"))
    else:
        page_urls = {URLNormalizer.normalize(str(r.get("final_url") or "")), str(r.get("normalized_source_url") or "")}
        if canon_url not in page_urls:
            out.append(("SOURCE_PAGE", "SITEMAP_URL_NOT_CANONICAL", "MEDIUM", f"canonical -> {canon_url[:150]}"))
    try:
        if float(r.get("response_time_ms") or 0) > SLOW_RESPONSE_MS:
            out.append(("SOURCE_PAGE", "SLOW_RESPONSE", "LOW", f"{float(r['response_time_ms']):.0f} ms"))
    except (TypeError, ValueError):
        pass
    if r.get("page_type") == "NEEDS_REVIEW" or r.get("business") == "NEEDS_REVIEW":
        out.append(("SOURCE_PAGE", "CLASSIFICATION_NEEDS_REVIEW", "INFO",
                    f"business={r.get('business')}, page_type={r.get('page_type')}"))
    return out
