"""Google Search Console (+ optional GA4) with a Google Cloud service account: top queries and pages (clicks,
impressions, CTR, position) for the last 28 days, URL Inspection for the audited URL, and GA4 organic sessions.

Free. Setup: create a service account in Google Cloud, enable the Search Console API (and Analytics Data API for
GA4), add the service account's e-mail as a user of the Search Console property (and GA4 property), then set:

    GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json
    GSC_PROPERTY=sc-domain:example.com          (or https://www.example.com/)
    GA4_PROPERTY_ID=123456789                   (optional)

Auth is the standard OAuth 2.0 JWT-bearer flow signed with RS256 (``cryptography``); no Google SDK needed.
Scope of the integration follows claude-seo's gsc_query.py / gsc_inspect.py / ga4_report.py (MIT).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from datetime import date, timedelta
from typing import Any, Dict, Optional

from .base import IntegrationResult, SafeFetcher, env_key, finding, normalize_target

ID = "google_search_console"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
INSPECT_API = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
GA4_API = "https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport"
SCOPES = "https://www.googleapis.com/auth/webmasters.readonly https://www.googleapis.com/auth/analytics.readonly"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(sa: Dict[str, Any], now: Optional[int] = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(now or time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if sa.get("private_key_id"):
        header["kid"] = sa["private_key_id"]
    claims = {"iss": sa["client_email"], "scope": SCOPES, "aud": sa.get("token_uri") or TOKEN_URL,
              "iat": now, "exp": now + 3600}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(sig)}"


def load_service_account() -> Optional[Dict[str, Any]]:
    path = env_key("GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS")
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        sa = json.load(f)
    if sa.get("type") != "service_account" or not sa.get("private_key") or not sa.get("client_email"):
        raise ValueError("file is not a Google service-account key")
    return sa


async def run(url: str, days: int = 28, **_: Any) -> Dict[str, Any]:
    target = normalize_target(url)
    res = IntegrationResult(ID, target, source="Search Console API + URL Inspection API + GA4 Data API")
    try:
        sa = load_service_account()
    except (ValueError, json.JSONDecodeError) as e:
        res.status, res.error = "error", f"GOOGLE_SERVICE_ACCOUNT_FILE: {e}"
        return res.as_dict()
    prop = env_key("GSC_PROPERTY")
    if not sa or not prop:
        res.status = "needs_key"
        res.error = ("Set GOOGLE_SERVICE_ACCOUNT_FILE and GSC_PROPERTY (free; add the service account as a user of "
                     "the Search Console property).")
        return res.as_dict()
    days = max(1, min(int(days or 28), 480))
    end = date.today() - timedelta(days=2)              # GSC data lags ~2 days
    start = end - timedelta(days=days - 1)
    async with SafeFetcher(timeout=60.0) as f:
        tr = await f.post(sa.get("token_uri") or TOKEN_URL, trusted_api=True, follow=False, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": make_jwt(sa)})
        if tr.status != 200:
            res.status, res.error = "error", f"Google token exchange failed ({tr.error or 'HTTP %s' % tr.status})"
            return res.as_dict()
        auth = {"Authorization": f"Bearer {json.loads(tr.body or b'{}').get('access_token', '')}"}
        site = urllib.parse.quote(prop, safe="")
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "rowLimit": 25}
        q = await f.post(GSC_API.format(site=site), json={**body, "dimensions": ["query"]}, headers=auth, trusted_api=True)
        p = await f.post(GSC_API.format(site=site), json={**body, "dimensions": ["page"]}, headers=auth, trusted_api=True)
        totals = await f.post(GSC_API.format(site=site), json={k: v for k, v in body.items() if k != "rowLimit"},
                              headers=auth, trusted_api=True)
        insp = await f.post(INSPECT_API, json={"inspectionUrl": target, "siteUrl": prop}, headers=auth, trusted_api=True)
        ga = None
        pid = env_key("GA4_PROPERTY_ID")
        if pid and pid.isdigit():
            ga = await f.post(GA4_API.format(pid=pid), headers=auth, trusted_api=True, json={
                "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
                "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}, {"name": "conversions"}]})
    if q.status in (401, 403):
        res.status, res.error = "error", "Service account has no access to this Search Console property"
        return res.as_dict()

    def rows(r):
        try:
            return (json.loads(r.body or b"{}").get("rows") or []) if r.status == 200 else []
        except json.JSONDecodeError:
            return []

    def shape(rs):
        return [{"key": r.get("keys", [""])[0], "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0),
                 "ctr": round(float(r.get("ctr", 0)) * 100, 2), "position": round(float(r.get("position", 0)), 1)} for r in rs]

    tot = (rows(totals) or [{}])[0]
    res.data = {"property": prop, "start": start.isoformat(), "end": end.isoformat(),
                "totals": {"clicks": tot.get("clicks", 0), "impressions": tot.get("impressions", 0),
                           "ctr": round(float(tot.get("ctr", 0)) * 100, 2), "position": round(float(tot.get("position", 0)), 1)},
                "top_queries": shape(rows(q)), "top_pages": shape(rows(p))}
    if insp.status == 200:
        ir = (json.loads(insp.body or b"{}").get("inspectionResult") or {}).get("indexStatusResult") or {}
        res.data["inspection"] = {k: ir.get(k) for k in ("verdict", "coverageState", "robotsTxtState", "indexingState",
                                                         "lastCrawlTime", "pageFetchState", "googleCanonical",
                                                         "userCanonical")}
        if ir.get("verdict") and ir["verdict"] != "PASS":
            res.findings.append(finding("gsc-not-indexed", "HIGH", f"Google: {ir.get('coverageState') or ir['verdict']}",
                                        f"URL Inspection verdict {ir['verdict']}", "Open URL Inspection in Search Console."))
        if ir.get("googleCanonical") and ir.get("userCanonical") and ir["googleCanonical"] != ir["userCanonical"]:
            res.findings.append(finding("gsc-canonical-mismatch", "MEDIUM", "Google picked a different canonical",
                                        f"declared {ir['userCanonical']} / Google {ir['googleCanonical']}"))
    if ga is not None and ga.status == 200:
        gd = json.loads(ga.body or b"{}")
        res.data["ga4_channels"] = [{"channel": r["dimensionValues"][0]["value"],
                                     "sessions": int(r["metricValues"][0]["value"]),
                                     "engaged_sessions": int(r["metricValues"][1]["value"]),
                                     "conversions": float(r["metricValues"][2]["value"])} for r in gd.get("rows", [])]
    res.summary = {"clicks": res.data["totals"]["clicks"], "impressions": res.data["totals"]["impressions"],
                   "ctr_pct": res.data["totals"]["ctr"], "avg_position": res.data["totals"]["position"],
                   "indexed": (res.data.get("inspection") or {}).get("verdict")}
    return res.as_dict()
