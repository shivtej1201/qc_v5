"""Bing Webmaster Tools API (free account + free API key): backlink counts, crawl issues, URL submission quota.

Key: ``BING_WEBMASTER_API_KEY`` (Bing Webmaster Tools > Settings > API access). The site must be verified there.
Endpoints and payload handling ported from claude-seo's bing_webmaster.py (MIT, (c) 2026 agricidaniel).
The key travels as the ``apikey`` query parameter (Bing's only option); it is never logged or returned.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List

from .base import IntegrationResult, SafeFetcher, env_key, finding, normalize_target

ID = "bing_webmaster"
API = "https://ssl.bing.com/webmaster/api.svc/json"
MAX_LINK_PAGES = 5


def _payload(body: bytes) -> Any:
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return {}
    return data.get("d", data) if isinstance(data, dict) else data


def _site(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}/"


async def run(url: str, **_: Any) -> Dict[str, Any]:
    target = normalize_target(url)
    site = _site(target)
    res = IntegrationResult(ID, target, source="Bing Webmaster API; claude-seo bing_webmaster.py")
    key = env_key("BING_WEBMASTER_API_KEY")
    if not key:
        res.status = "needs_key"
        res.error = "Set BING_WEBMASTER_API_KEY (free: Bing Webmaster Tools > Settings > API access)."
        return res.as_dict()

    async def call(endpoint: str, **params: Any):
        return await f.get(f"{API}/{endpoint}", params={**params, "apikey": key}, trusted_api=True, follow=False)

    async with SafeFetcher(timeout=30.0) as f:
        links: Dict[str, int] = {}
        pages, total_pages = 0, 1
        while pages < min(total_pages, MAX_LINK_PAGES):
            r = await call("GetLinkCounts", siteUrl=site, page=pages)
            if r.status != 200:
                if pages == 0:
                    res.status = "error"
                    res.error = ("API key rejected or site not verified in Bing Webmaster Tools"
                                 if r.status in (400, 401, 403) else (r.error or f"HTTP {r.status}"))
                    return res.as_dict()
                break
            d = _payload(r.body) or {}
            total_pages = max(1, int(d.get("TotalPages") or 1))
            for item in d.get("Links") or []:
                if isinstance(item, dict) and item.get("Url"):
                    links[item["Url"]] = int(item.get("Count") or 0)
            pages += 1
        crawl = await call("GetCrawlIssues", siteUrl=site)
        quota = await call("GetUrlSubmissionQuota", siteUrl=site)
    issues: List[Dict[str, Any]] = _payload(crawl.body) if crawl.status == 200 else []
    issues = issues if isinstance(issues, list) else []
    q = _payload(quota.body) if quota.status == 200 else {}
    top = sorted(links.items(), key=lambda kv: kv[1], reverse=True)[:20]
    res.data = {"site": site, "pages_with_inbound_links": len(links), "inbound_links_sampled": sum(links.values()),
                "link_pages_read": pages, "link_pages_total": total_pages, "top_linked_pages": top,
                "crawl_issues": issues[:100], "submission_quota": q if isinstance(q, dict) else {}}
    if issues:
        res.findings.append(finding("bing-crawl-issues", "MEDIUM", f"Bing reports {len(issues)} crawl issue(s)",
                                    fix="Open Bing Webmaster Tools > Crawl information for details."))
    if not links:
        res.findings.append(finding("bing-no-backlinks", "INFO", "Bing reports no inbound links (sample)"))
    res.summary = {"pages_with_inbound_links": len(links), "inbound_links_sampled": sum(links.values()),
                   "crawl_issues": len(issues), "complete": pages >= total_pages}
    return res.as_dict()
