"""Rule catalogue: every finding the product can report, written for people (blueprint §10.7, design spec §2).

Each rule has a scope that decides how findings group into fixes:
  template -> caused by the layout (one fix per page type)
  subject  -> one broken target referenced from many pages (one fix per target)
  page     -> genuinely page-specific
Message fields are str.format templates. Available fields: n_pages, template, subject, example, value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict

SEVERITIES = ("critical", "high", "medium", "low")
DIMENSIONS = ("technical", "links", "content", "experience", "performance", "security")


@dataclass(frozen=True)
class Rule:
    code: str
    scope: str
    severity: str
    dimension: str
    title: str      # <= 140 chars after formatting with typical values
    why: str
    fix: str
    verify: str


RULES: Dict[str, Rule] = {}


def _r(code, scope, severity, dimension, title, why, fix, verify="The next run shows 0 pages for this fix."):
    RULES[code] = Rule(code, scope, severity, dimension, title, why, fix, verify)


# ---- coverage crawl (raw HTML / HTTP) -------------------------------------------------------------------------
_r("PAGE_NOT_FOUND", "page", "critical", "technical",
   "A sitemap page returns {value}",
   "People and search engines following the sitemap land on an error instead of the page.",
   "Restore the page, or redirect it and remove it from the sitemap.")
_r("PAGE_SERVER_ERROR", "page", "critical", "technical",
   "A sitemap page fails with a server error ({value})",
   "Visitors see an error and search engines may drop the page if it keeps failing.",
   "Check the server logs for this URL and fix the error.")
_r("BROKEN_LINK", "subject", "high", "links",
   "{n_pages} pages link to a page that no longer exists",
   "Visitors who click it hit a dead end, and search engines waste crawl time on it.",
   "Update or remove the link to {subject}. If the target moved, redirect it to its new address.")
_r("REDIRECTED_LINK", "subject", "low", "links",
   "{n_pages} pages link to an address that redirects",
   "Each redirect adds a delay for visitors and dilutes the link for search engines.",
   "Point the links straight at {value}.")
_r("BROKEN_IMAGE", "subject", "high", "content",
   "An image used on {n_pages} pages does not load",
   "Visitors see an empty box and the page looks broken.",
   "Restore the image at {subject} or replace it in the {template} template.")
_r("H1_MISSING", "template", "medium", "content",
   "{n_pages} {template} have no main heading",
   "Search engines and screen-reader users use the main heading to understand what a page is about.",
   "Add one <h1> to the {template} template that states the page's topic.",
   "The next run shows 0 pages without a heading. You can check one now: {example}")
_r("TITLE_MISSING", "template", "high", "content",
   "{n_pages} {template} have no page title",
   "The title is what people see in search results and browser tabs.",
   "Give every page in the {template} template a specific title of about 30-60 characters.")
_r("TITLE_DUPLICATE", "page", "low", "content",
   "{n_pages} pages share the title \"{subject}\"",
   "Search results can't tell these pages apart, so the wrong one may rank.",
   "Write a title for each page that names what makes it different.")
_r("DESCRIPTION_MISSING", "template", "low", "content",
   "{n_pages} {template} have no meta description",
   "Search engines will pick text from the page for the result snippet, often a poor one.",
   "Add a meta description of about 70-160 characters to the {template} template.")
_r("NOINDEX_IN_SITEMAP", "page", "high", "technical",
   "A page in the sitemap tells search engines not to index it",
   "The sitemap and the page disagree; the page will not appear in search.",
   "Either remove noindex or remove the page from the sitemap.")
_r("CANONICAL_ELSEWHERE", "page", "medium", "technical",
   "A sitemap page points its canonical to another address",
   "Search engines will index {value} instead of this page.",
   "List only canonical addresses in the sitemap, or fix the canonical tag.")
_r("SLOW_SERVER", "template", "medium", "performance",
   "{template} take {value} to start responding",
   "Visitors wait before anything appears; slow responses also slow down search engine crawling.",
   "Cache these pages at the CDN or speed up the server work behind the {template} template.")
_r("MIXED_CONTENT", "template", "medium", "security",
   "{n_pages} {template} load files over plain HTTP",
   "Browsers block or warn about insecure files on secure pages.",
   "Load every script, style and image in the {template} template over https://.")

# ---- real-visitor review (rendered) ---------------------------------------------------------------------------
_r("CONTENT_ONLY_AFTER_JS", "template", "medium", "experience",
   "{template} show their main heading only after JavaScript runs",
   "Search engines that don't run JavaScript, and visitors on slow phones, see an empty page first.",
   "Render the heading and main text in the server HTML of the {template} template.")
_r("LINKS_ONLY_AFTER_JS", "template", "low", "experience",
   "{template} add {value} links only after JavaScript runs",
   "Search engines may not follow links that are not in the server HTML.",
   "Put important navigation links in the server HTML.")
_r("LAYOUT_SHIFT", "template", "medium", "experience",
   "{template} jump around while loading (layout shift {value})",
   "Content moving under a visitor's finger causes mis-taps; Google counts it in page experience.",
   "Reserve space for images, ads and banners in the {template} template (width/height or aspect-ratio).",
   "Real-visitor checks show layout shift below 0.1.")
_r("SLOW_MAIN_CONTENT", "template", "medium", "performance",
   "The main content of {template} takes {value} to appear",
   "Visitors look at a blank or half-built page; above 2.5 s many leave.",
   "Make the largest image or heading load first: preload it, compress it, and avoid render-blocking scripts.",
   "Real-visitor checks show the largest content under 2.5 s.")
_r("SLOW_RESPONSE_TO_TAP", "template", "medium", "experience",
   "{template} react slowly to taps ({value})",
   "The page feels frozen; visitors tap again or give up.",
   "Break up long JavaScript tasks that run when the page loads.")
_r("CONSOLE_ERRORS", "template", "low", "experience",
   "{template} show JavaScript errors in the browser",
   "Errors often mean a widget or tracking script is broken for visitors.",
   "Open one page in the browser console and fix the first error: {value}")
_r("A11Y_SERIOUS", "template", "high", "experience",
   "{template}: {value}",
   "People using screen readers or keyboards can't use this part of the page.",
   "Fix it in the {template} template; the check shows the exact element.",
   "Real-visitor checks show no serious accessibility problems.")
_r("CONSENT_SHIFTS_PAGE", "template", "medium", "experience",
   "The cookie banner shifts the page on {template}",
   "The page jumps when the banner appears or closes.",
   "Show the banner as an overlay that doesn't push content.")

# ---- journeys --------------------------------------------------------------------------------------------------
_r("JOURNEY_FAILED", "subject", "critical", "experience",
   "The journey \"{subject}\" fails at step {value}",
   "Customers taking this path get stuck.",
   "Open the failed step: it shows what happened, the screenshot and the browser errors.",
   "The journey passes on its next scheduled run.")

# ---- extra free checks (self-hosted Nu HTML checker, LanguageTool, local Lighthouse) ----------------------------
_r("HTML_INVALID", "template", "low", "technical",
   "{template} have {value} markup errors",
   "Broken markup can make browsers, screen readers and search engines read the page differently from what you intended.",
   "Fix the first errors in the {template} template; the checker lists the line and the problem: {subject}")
_r("SPELLING", "template", "low", "content",
   "{template} have spelling or grammar mistakes ({value})",
   "Mistakes in headings and key text make the brand look careless and can confuse readers.",
   "Correct the text in the CMS or the {template} template. First ones found: {subject}")
_r("LAB_PERFORMANCE", "template", "medium", "performance",
   "{template} score {value} out of 100 for speed in a lab test",
   "A slow lab score usually means visitors on mid-range phones wait too long for the page.",
   "Open the Lighthouse report for one page of this type and start with the largest savings: {subject}",
   "The lab score reaches 90 or more.")
_r("LAB_ACCESSIBILITY", "template", "medium", "experience",
   "{template} score {value} out of 100 for accessibility in a lab test",
   "Parts of the page are hard or impossible to use with a screen reader, keyboard or low vision.",
   "Fix the failed checks listed in the Lighthouse report: {subject}",
   "The lab score reaches 90 or more.")

# ---- checks carried over from the V5 page check without a dedicated V6 text yet ------------------------------------
_r("PAGE_CHECK", "template", "medium", "content",
   "{n_pages} {template}: {value}",
   "Found by the page check on every page of this type.",
   "{subject}")
_r("LINK_CHECK", "subject", "medium", "links",
   "{n_pages} pages: {value}",
   "Found by the link check.",
   "{subject}")

# ---- access ---------------------------------------------------------------------------------------------------
_r("ACCESS_CHALLENGED", "subject", "high", "technical",
   "{subject} challenged our visitors as bots",
   "Real-visitor checks on this host stopped, so experience results are missing.",
   "Ask your CDN team to allow QC with the rule in Settings → Crawler access.",
   "Test access in Settings shows no challenge.")


_CODEY = re.compile(r"\b[A-Z]{2,}_[A-Z_]{2,}\b")


class _Default(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(code: str, **fields) -> Dict[str, str]:
    """Format a rule's four texts. Missing fields stay visible as {name} so tests catch them."""
    r = RULES[code]
    f = _Default(fields)
    if "n_pages" in f and isinstance(f["n_pages"], int):
        f["n_pages"] = format_count(f["n_pages"])
    if "template" in f and f["template"] and not str(f["template"]).endswith("pages"):
        f["template"] = f"{f['template']} pages"
    if fields.get("n_pages") == 1 and str(f.get("template", "")).endswith(" pages"):
        f["template"] = str(f["template"])[:-1]                     # "1 Product page", not "1 Product pages"
    return {"title": r.title.format_map(f), "why": r.why.format_map(f), "fix": r.fix.format_map(f),
            "verify": r.verify.format_map(f)}


def format_count(n: int, system: str = "in") -> str:
    """Indian grouping by default (3,00,000), international on request (300,000)."""
    if system != "in" or n < 1000:
        return f"{n:,}"
    s = str(n)
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def lint_copy() -> list[str]:
    """Style rules for every rule text (design spec §2). Returns problems; empty list = clean."""
    problems = []
    for code, r in RULES.items():
        for field_name in ("title", "why", "fix", "verify"):
            text = getattr(r, field_name)
            if _CODEY.search(text):
                problems.append(f"{code}.{field_name}: internal code in text")
            if "!" in text:
                problems.append(f"{code}.{field_name}: exclamation mark")
        sample = render(code, n_pages=41200, template="Product", subject="https://www.example.com/a",
                        example="https://www.example.com/p/1", value="2.8 s")["title"]
        if len(sample) > 140:
            problems.append(f"{code}.title: longer than 140 characters")
        if r.scope not in ("template", "subject", "page") or r.severity not in SEVERITIES or r.dimension not in DIMENSIONS:
            problems.append(f"{code}: bad scope/severity/dimension")
    return problems
