"""Google Analytics 4 (free, service account) for the visit plan: which pages people actually land on, and the
real mobile/desktop mix, so the visitors sample the pages that matter and use the devices real people use.

Site settings: {"ga4_property_id": "123456789"}; credentials: GOOGLE_SERVICE_ACCOUNT_FILE (same as QC V5's
Search Console integration). The service account needs Viewer access to the GA4 property.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Dict, Optional

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport"
_transport: Optional[httpx.AsyncBaseTransport] = None


def set_transport(t) -> None:
    global _transport
    _transport = t


async def traffic(site: Dict[str, Any], days: int = 28, limit: int = 2000) -> Optional[Dict[str, Any]]:
    """{"paths": {path: sessions}, "mobile_share": 0..1} or None when GA4 isn't set up."""
    pid = str((site.get("settings") or {}).get("ga4_property_id") or "")
    if not pid.isdigit():
        return None
    from qcv5.integrations.google_search_console import load_service_account, make_jwt
    sa = load_service_account()
    if not sa:
        return None
    kw: Dict[str, Any] = {"timeout": 30}
    if _transport is not None:
        kw["transport"] = _transport
    async with httpx.AsyncClient(**kw) as c:
        tr = await c.post(sa.get("token_uri") or TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                                  "assertion": make_jwt(sa)})
        tr.raise_for_status()
        auth = {"Authorization": f"Bearer {tr.json()['access_token']}"}
        rng = [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}]
        pages = await c.post(API.format(pid=pid), headers=auth, json={
            "dateRanges": rng, "dimensions": [{"name": "landingPage"}], "metrics": [{"name": "sessions"}],
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}], "limit": limit})
        dev = await c.post(API.format(pid=pid), headers=auth, json={
            "dateRanges": rng, "dimensions": [{"name": "deviceCategory"}], "metrics": [{"name": "sessions"}]})
    paths: Dict[str, int] = {}
    if pages.status_code == 200:
        for r in pages.json().get("rows", []):
            p = urllib.parse.urlsplit(r["dimensionValues"][0]["value"]).path or "/"
            paths[p] = paths.get(p, 0) + int(r["metricValues"][0]["value"])
    mobile_share = None
    if dev.status_code == 200:
        counts = {r["dimensionValues"][0]["value"]: int(r["metricValues"][0]["value"]) for r in dev.json().get("rows", [])}
        total = sum(counts.values())
        if total:
            mobile_share = round((counts.get("mobile", 0) + counts.get("tablet", 0)) / total, 3)
    return {"paths": paths, "mobile_share": mobile_share}
