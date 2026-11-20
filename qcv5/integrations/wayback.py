"""Wayback Machine history (archive.org CDX API, free, no key): first / latest capture, captures per year, and how
often the homepage changed. Useful for domain heritage checks (expired-domain reuse) and "when did this page change".
Idea from claude-seo's domain_history.py (MIT, (c) 2026 agricidaniel); this uses archive.org instead of WHOIS.
"""
from __future__ import annotations

import json
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict

from .base import IntegrationResult, SafeFetcher, finding, normalize_target

ID = "wayback"
CDX = "https://web.archive.org/cdx/search/cdx"


def _ts(ts: str) -> str:
    try:
        return datetime.strptime(ts[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return ts


def summarize(rows) -> Dict[str, Any]:
    """rows: CDX JSON rows (first row is the header) with timestamp,statuscode,digest."""
    if not rows or len(rows) < 2:
        return {"captures": 0}
    head, body = rows[0], rows[1:]
    idx = {k: i for i, k in enumerate(head)}
    ts = [r[idx["timestamp"]] for r in body]
    digests = [r[idx["digest"]] for r in body] if "digest" in idx else []
    changes = sum(1 for a, b in zip(digests, digests[1:]) if a != b)
    per_year = Counter(t[:4] for t in ts)
    first, last = min(ts), max(ts)
    age_years = round((datetime.now(timezone.utc) - datetime.strptime(first[:8], "%Y%m%d").replace(
        tzinfo=timezone.utc)).days / 365.25, 1)
    return {"captures": len(ts), "first_capture": _ts(first), "last_capture": _ts(last), "archive_age_years": age_years,
            "content_changes": changes, "captures_per_year": dict(sorted(per_year.items())),
            "first_snapshot_url": f"https://web.archive.org/web/{first}/", "last_snapshot_url": f"https://web.archive.org/web/{last}/"}


async def run(url: str, **_: Any) -> Dict[str, Any]:
    target = normalize_target(url)
    host = urllib.parse.urlsplit(target).hostname or ""
    res = IntegrationResult(ID, target, source="archive.org Wayback CDX API")
    params = [("url", host), ("output", "json"), ("fl", "timestamp,statuscode,digest"), ("filter", "statuscode:200"),
              ("collapse", "timestamp:8"), ("limit", "5000")]
    async with SafeFetcher(timeout=40.0) as f:
        r = await f.get(CDX, params=params, trusted_api=True, max_body=5_000_000)
    if r.error or r.status != 200:
        res.status, res.error = "error", r.error or f"archive.org HTTP {r.status}"
        return res.as_dict()
    try:
        rows = json.loads(r.body or b"[]")
    except json.JSONDecodeError:
        rows = []
    s = summarize(rows)
    for k in ("first_snapshot_url", "last_snapshot_url"):
        if k in s:
            s[k] = s[k] + host
    res.data = s
    if not s["captures"]:
        res.findings.append(finding("wayback-no-captures", "INFO", "The homepage has no Wayback captures",
                                    "New domain, or archiving is blocked."))
    res.summary = {k: s.get(k) for k in ("captures", "first_capture", "last_capture", "archive_age_years", "content_changes")}
    return res.as_dict()
