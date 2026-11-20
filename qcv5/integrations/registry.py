"""Catalogue of the free SEO integrations and a single entry point to run one."""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Dict, List

from . import (ai_access, bing_webmaster, google_search_console, indexnow, pagespeed, security_headers,
               structured_data, wayback)
from .base import UnsafeURLError, utc_now

Runner = Callable[..., Awaitable[Dict[str, Any]]]

INTEGRATIONS: List[Dict[str, Any]] = [
    {"id": ai_access.ID, "name": "AI crawler access", "category": "Crawlability", "cost": "Free, no key",
     "env": [], "runner": ai_access.run, "params": [],
     "description": "robots.txt rules for 13 AI and search crawlers (GPTBot, ClaudeBot, PerplexityBot, Googlebot …), "
                    "Content-Signal preferences and /llms.txt."},
    {"id": security_headers.ID, "name": "HTTPS & security headers", "category": "Security", "cost": "Free, no key",
     "env": [], "runner": security_headers.run, "params": [],
     "description": "HSTS, CSP, nosniff, framing, Referrer-Policy, Permissions-Policy, cookie flags, http→https "
                    "redirect and www / non-www consolidation."},
    {"id": structured_data.ID, "name": "Structured data", "category": "Rich results", "cost": "Free, no key",
     "env": [], "runner": structured_data.run, "params": [],
     "description": "Extracts JSON-LD, checks Google's required and recommended fields, flags retired rich-result "
                    "types; ships 11 ready-to-fill Schema.org templates."},
    {"id": pagespeed.ID, "name": "PageSpeed & Core Web Vitals", "category": "Performance",
     "cost": "Free (optional free API key)", "env": list(pagespeed.KEY_VARS), "runner": pagespeed.run,
     "params": [{"name": "strategy", "options": ["mobile", "desktop"], "default": "mobile"}],
     "description": "Lighthouse scores and lab metrics from PageSpeed Insights plus real-user Chrome UX Report "
                    "field data (LCP, INP, CLS)."},
    {"id": wayback.ID, "name": "Wayback history", "category": "Domain", "cost": "Free, no key",
     "env": [], "runner": wayback.run, "params": [],
     "description": "archive.org captures: first and latest snapshot, captures per year, how often the homepage "
                    "changed (domain heritage)."},
    {"id": indexnow.ID, "name": "IndexNow", "category": "Indexing", "cost": "Free, self-generated key",
     "env": ["INDEXNOW_KEY"], "runner": indexnow.run,
     "params": [{"name": "submit", "options": [False, True], "default": False}],
     "description": "Checks the published IndexNow key and submits changed URLs to Bing, Yandex, Seznam, Naver, "
                    "Yep and Amazon."},
    {"id": bing_webmaster.ID, "name": "Bing Webmaster Tools", "category": "Backlinks & indexing",
     "cost": "Free account + API key", "env": ["BING_WEBMASTER_API_KEY"], "runner": bing_webmaster.run, "params": [],
     "description": "Inbound-link counts per page, crawl issues and URL submission quota for a verified site."},
    {"id": google_search_console.ID, "name": "Google Search Console + GA4", "category": "Search performance",
     "cost": "Free (service account)", "env": ["GOOGLE_SERVICE_ACCOUNT_FILE", "GSC_PROPERTY", "GA4_PROPERTY_ID"],
     "runner": google_search_console.run, "params": [{"name": "days", "options": [7, 28, 90], "default": 28}],
     "description": "Clicks, impressions, CTR and position (top queries and pages), URL Inspection index status and "
                    "GA4 sessions by channel."},
]
BY_ID = {i["id"]: i for i in INTEGRATIONS}

NOT_INCLUDED = [
    {"name": "DataForSEO, Ahrefs, Moz, SE Ranking, Profound", "reason": "paid APIs"},
    {"name": "Firecrawl, image generation (banana)", "reason": "paid or credit-based; not SEO auditing"},
    {"name": "Common Crawl web graph", "reason": "downloads multi-GB rank files per query; too heavy for a web request"},
    {"name": "Matomo, PostHog, Shopify, YouTube, Google Ads keyword planner",
     "reason": "need a self-hosted analytics stack, a store, or an Ads account; out of scope for a site audit"},
]


def _configured(env: List[str]) -> bool:
    return all(os.environ.get(e) for e in env if e not in ("GA4_PROPERTY_ID", "GOOGLE_API_KEY", "PAGESPEED_API_KEY"))


def catalogue() -> Dict[str, Any]:
    items = []
    for i in INTEGRATIONS:
        optional_key = i["id"] == pagespeed.ID
        items.append({k: v for k, v in i.items() if k != "runner"} | {
            "configured": True if optional_key or not i["env"] else _configured(i["env"]),
            "key_set": any(os.environ.get(e) for e in i["env"]) if i["env"] else None})
    return {"integrations": items, "not_included": NOT_INCLUDED,
            "attribution": "Checks ported from claude-seo (MIT, (c) 2026 agricidaniel)."}


async def run_integration(integration_id: str, url: str, **params: Any) -> Dict[str, Any]:
    spec = BY_ID.get(integration_id)
    if spec is None:
        raise KeyError(integration_id)
    allowed = {p["name"] for p in spec["params"]} | ({"urls", "key", "key_location"} if integration_id == indexnow.ID else set())
    clean = {k: v for k, v in params.items() if k in allowed and v is not None}
    try:
        return await spec["runner"](url, **clean)
    except UnsafeURLError as e:
        return {"integration": integration_id, "status": "error", "target": url, "error": str(e), "findings": [],
                "summary": {}, "data": {}, "checked_at": utc_now(), "duration_ms": 0, "source": ""}
