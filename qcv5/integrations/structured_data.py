"""Structured data: extract JSON-LD from a page, validate Google rich-result fields, flag retired types, and serve
ready-to-fill Schema.org templates.

Product / Offer rules and the retired-type list are ported from claude-seo's schema_ecommerce_validate.py; the
templates are claude-seo's schema/templates.json (MIT, (c) 2026 agricidaniel). The Rich Results Test remains the
authority for syntax; this check catches missing fields and retired types. No API key needed.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List

from .base import IntegrationResult, SafeFetcher, finding, normalize_target

ID = "structured_data"
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "schema_templates.json")

# Google retired these rich results in 2025 (Search Central blog, June/July 2025).
DEPRECATED_TYPES = {
    "Vehicle": "Vehicle Listing rich result retired June 2025.",
    "VehicleListing": "Vehicle Listing rich result retired June 2025.",
    "ClaimReview": "Claim Review rich result retired June 2025.",
    "EstimatedSalary": "Estimated Salary rich result retired June 2025.",
    "LearningVideo": "Learning Video rich result retired June 2025.",
    "SpecialAnnouncement": "Special Announcement rich result deprecated July 2025.",
}
# Google Search Central "required" properties per rich-result type (recommended ones are listed separately).
REQUIRED = {
    "Product": ("name",),
    "Offer": ("price", "priceCurrency"),
    "Article": ("headline",), "NewsArticle": ("headline",), "BlogPosting": ("headline",),
    "BreadcrumbList": ("itemListElement",),
    "FAQPage": ("mainEntity",),
    "Organization": ("name",),
    "LocalBusiness": ("name", "address"),
    "Event": ("name", "startDate", "location"),
    "Recipe": ("name", "image"),
    "VideoObject": ("name", "thumbnailUrl", "uploadDate"),
    "JobPosting": ("title", "description", "datePosted", "hiringOrganization", "jobLocation"),
    "Review": ("itemReviewed", "reviewRating", "author"),
    "AggregateRating": ("ratingValue",),
    "SoftwareApplication": ("name",),
}
RECOMMENDED = {
    "Product": ("image", "description", "offers", "brand", "sku", "aggregateRating", "review"),
    "Offer": ("availability", "url", "priceValidUntil", "hasMerchantReturnPolicy", "shippingDetails"),
    "Article": ("image", "datePublished", "dateModified", "author"),
    "NewsArticle": ("image", "datePublished", "dateModified", "author"),
    "BlogPosting": ("image", "datePublished", "dateModified", "author"),
    "Organization": ("url", "logo", "sameAs"),
    "LocalBusiness": ("telephone", "openingHoursSpecification", "geo", "url"),
    "Event": ("image", "description", "endDate", "offers", "organizer"),
    "VideoObject": ("description", "contentUrl", "duration"),
}
_LDJSON = re.compile(r"<script[^>]+type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.I | re.S)


def extract_jsonld(html: str) -> Dict[str, Any]:
    blocks, errors = [], []
    for i, m in enumerate(_LDJSON.finditer(html or "")):
        raw = m.group(1).strip()
        if raw.startswith("<!--"):
            raw = raw[4:]
        if raw.endswith("-->"):
            raw = raw[:-3]
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError as e:
            errors.append({"block": i + 1, "error": f"{e.msg} (line {e.lineno}, col {e.colno})"})
    return {"blocks": blocks, "errors": errors}


def _types(node: Dict[str, Any]) -> List[str]:
    t = node.get("@type")
    return [str(x) for x in (t if isinstance(t, list) else [t]) if x]


def iter_nodes(value: Any) -> Iterable[Dict[str, Any]]:
    """Every dict that carries an @type, through @graph and nested properties."""
    if isinstance(value, list):
        for v in value:
            yield from iter_nodes(v)
    elif isinstance(value, dict):
        if "@type" in value:
            yield value
        for k, v in value.items():
            if k != "@context":
                yield from iter_nodes(v)


def _present(v: Any) -> bool:
    return v not in (None, "", [], {})


def validate_nodes(blocks: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for node in iter_nodes(blocks):
        for t in _types(node):
            if t in DEPRECATED_TYPES:
                out.append(finding(f"deprecated-{t}", "HIGH", f"{t}: rich result retired", DEPRECATED_TYPES[t],
                                   "Remove the markup or switch to a supported type."))
            missing = [p for p in REQUIRED.get(t, ()) if not _present(node.get(p))]
            if t == "Offer" and not missing and isinstance(node.get("price"), str) and \
                    not re.fullmatch(r"\d+(\.\d+)?", node["price"].strip()):
                out.append(finding("offer-price-format", "MEDIUM", "Offer.price is not a plain number",
                                   f"price={node['price'][:40]!r}", "Use a number like 1499.00 (no currency sign)."))
            if missing:
                out.append(finding(f"{t}-missing-required", "HIGH", f"{t} is missing required properties",
                                   ", ".join(missing), f"Add {', '.join(missing)} to the {t} markup.",
                                   {"type": t, "missing": missing}))
            rec = [p for p in RECOMMENDED.get(t, ()) if not _present(node.get(p))]
            if rec:
                out.append(finding(f"{t}-missing-recommended", "LOW", f"{t} lacks recommended properties",
                                   ", ".join(rec), "Recommended fields make the rich result more complete.",
                                   {"type": t, "missing": rec}))
            if t == "Product":
                offers = node.get("offers")
                offer_list = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
                if any("AggregateOffer" in _types(o) for o in offer_list if isinstance(o, dict)):
                    out.append(finding("product-aggregate-offer", "MEDIUM",
                                       "Product uses AggregateOffer",
                                       "Merchant listings need Offer; AggregateOffer only fits product snippets "
                                       "across sellers.", "Use Offer for a single seller."))
    # de-duplicate identical findings (same id + detail) from repeated nodes
    seen, uniq = set(), []
    for fnd in out:
        key = (fnd["id"], fnd["detail"])
        if key not in seen:
            seen.add(key)
            uniq.append(fnd)
    return uniq


def templates() -> Dict[str, Any]:
    """{name: {"type", "description", "template"}} (the bundled file is a list; names are unique)."""
    with open(_DATA, encoding="utf-8") as f:
        items = json.load(f)["templates"]
    return {t.get("type", f"template_{i}"): t for i, t in enumerate(items)}


def template_list() -> List[Dict[str, str]]:
    t = templates()
    return [{"name": k, "type": v.get("type", k), "description": v.get("description", "")} for k, v in t.items()]


async def run(url: str, **_: Any) -> Dict[str, Any]:
    target = normalize_target(url)
    res = IntegrationResult(ID, target, source="claude-seo schema_ecommerce_validate.py + Google rich-result fields")
    async with SafeFetcher() as f:
        page = await f.get(target)
    if page.error or page.status is None or page.status >= 400:
        res.status = "unreachable"
        res.error = page.error or f"HTTP {page.status}"
        return res.as_dict()
    ext = extract_jsonld(page.text)
    for e in ext["errors"]:
        res.findings.append(finding("jsonld-invalid", "HIGH", f"JSON-LD block {e['block']} is not valid JSON",
                                    e["error"], "Fix the syntax; search engines ignore invalid blocks."))
    res.findings.extend(validate_nodes(ext["blocks"]))
    types = sorted({t for n in iter_nodes(ext["blocks"]) for t in _types(n)})
    microdata = len(re.findall(r"\bitemtype\s*=", page.text, re.I))
    rdfa = len(re.findall(r"\btypeof\s*=", page.text, re.I))
    if not ext["blocks"] and not microdata and not rdfa:
        res.findings.append(finding("no-structured-data", "MEDIUM", "No structured data on the page",
                                    fix="Add JSON-LD for the page type (Organization, Product, Article, "
                                        "BreadcrumbList …). Templates: /api/integrations/schema-templates"))
    res.data = {"types": types, "jsonld_blocks": len(ext["blocks"]), "invalid_blocks": len(ext["errors"]),
                "microdata_items": microdata, "rdfa_items": rdfa, "blocks": ext["blocks"][:10]}
    res.summary = {"jsonld_blocks": len(ext["blocks"]), "types": types, "invalid_blocks": len(ext["errors"])}
    return res.as_dict()
