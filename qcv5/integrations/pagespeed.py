"""PageSpeed Insights (Lighthouse lab data + CrUX field data) and the Chrome UX Report API.

Free: PSI works without a key on Google's shared public quota (often exhausted); a free Google Cloud API key
(``GOOGLE_API_KEY`` or ``PAGESPEED_API_KEY``) gives 25,000 requests/day. CrUX requires a key (free).
Parsing and Core Web Vitals thresholds are ported from claude-seo's pagespeed_check.py (MIT, (c) 2026 agricidaniel).
The key is sent in the X-Goog-Api-Key header, never in the URL, and never returned.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import IntegrationResult, SafeFetcher, check_safe, env_key, finding, normalize_target

ID = "pagespeed"
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
KEY_VARS = ("PAGESPEED_API_KEY", "GOOGLE_API_KEY")

CWV_THRESHOLDS = {
    "largest_contentful_paint": {"good": 2500, "poor": 4000, "unit": "ms", "label": "LCP"},
    "interaction_to_next_paint": {"good": 200, "poor": 500, "unit": "ms", "label": "INP"},
    "cumulative_layout_shift": {"good": 0.1, "poor": 0.25, "unit": "", "label": "CLS"},
    "first_contentful_paint": {"good": 1800, "poor": 3000, "unit": "ms", "label": "FCP"},
    "experimental_time_to_first_byte": {"good": 800, "poor": 1800, "unit": "ms", "label": "TTFB"},
}
PSI_METRIC_MAP = {
    "LARGEST_CONTENTFUL_PAINT_MS": "largest_contentful_paint",
    "INTERACTION_TO_NEXT_PAINT": "interaction_to_next_paint",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cumulative_layout_shift",
    "FIRST_CONTENTFUL_PAINT_MS": "first_contentful_paint",
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "experimental_time_to_first_byte",
}
LAB_AUDITS = ("first-contentful-paint", "largest-contentful-paint", "total-blocking-time",
              "cumulative-layout-shift", "speed-index", "interactive")


def rate(metric: str, value: Optional[float]) -> Optional[str]:
    t = CWV_THRESHOLDS.get(metric)
    if t is None or value is None:
        return None
    return "good" if value <= t["good"] else ("poor" if value > t["poor"] else "needs-improvement")


def parse_psi(data: Dict[str, Any]) -> Dict[str, Any]:
    lr = data.get("lighthouseResult", {}) or {}
    scores = {k: round(v["score"] * 100) for k, v in (lr.get("categories") or {}).items()
              if isinstance(v, dict) and v.get("score") is not None}
    audits = lr.get("audits", {}) or {}
    lab = {a: {"value": audits[a]["numericValue"], "display": audits[a].get("displayValue", ""),
               "score": audits[a].get("score")}
           for a in LAB_AUDITS if isinstance(audits.get(a), dict) and audits[a].get("numericValue") is not None}
    field: Dict[str, Any] = {}
    for exp_key, src in (("loadingExperience", "url"), ("originLoadingExperience", "origin")):
        for psi_name, name in PSI_METRIC_MAP.items():
            md = ((data.get(exp_key) or {}).get("metrics") or {}).get(psi_name)
            if md and md.get("percentile") is not None:
                p75 = md["percentile"]
                if name == "cumulative_layout_shift":
                    p75 = p75 / 100 if p75 > 1 else p75
                field[f"{src}_{name}"] = {"p75": p75, "rating": str(md.get("category", "NONE")).lower().replace("_", "-")}
    opps = sorted(({"id": k, "title": v.get("title", k), "savings_ms": v["details"]["overallSavingsMs"]}
                   for k, v in audits.items()
                   if isinstance(v, dict) and (v.get("details") or {}).get("type") == "opportunity"
                   and ((v.get("details") or {}).get("overallSavingsMs") or 0) > 0),
                  key=lambda o: o["savings_ms"], reverse=True)
    return {"lighthouse_version": lr.get("lighthouseVersion"), "scores": scores, "lab": lab, "field": field,
            "opportunities": opps[:15], "analysis_utc": data.get("analysisUTCTimestamp")}


def parse_crux(data: Dict[str, Any]) -> Dict[str, Any]:
    metrics = ((data.get("record") or {}).get("metrics") or {})
    out = {}
    for name, md in metrics.items():
        p75 = (md.get("percentiles") or {}).get("p75")
        if p75 is None:
            continue
        try:
            val = float(p75)
        except (TypeError, ValueError):
            continue
        out[name] = {"p75": val, "rating": rate(name, val)}
    return out


async def run(url: str, strategy: str = "mobile", **_: Any) -> Dict[str, Any]:
    target = normalize_target(url)
    strategy = "desktop" if str(strategy).lower() == "desktop" else "mobile"
    res = IntegrationResult(ID, target, source="Google PageSpeed Insights v5 + CrUX API; claude-seo pagespeed_check.py")
    check_safe(target)            # never ask Google to fetch a private address on our behalf
    key = env_key(*KEY_VARS)
    headers = {"X-Goog-Api-Key": key} if key else None
    params = [("url", target), ("strategy", strategy.upper())] + \
             [("category", c) for c in ("PERFORMANCE", "ACCESSIBILITY", "BEST_PRACTICES", "SEO")]
    async with SafeFetcher(timeout=120.0) as f:
        psi = await f.get(PSI_ENDPOINT, params=params, headers=headers, trusted_api=True, max_body=20_000_000)
        crux = None
        if key:
            crux = await f.post(CRUX_ENDPOINT, json={"url": target, "formFactor": "PHONE" if strategy == "mobile" else "DESKTOP"},
                                headers=headers, trusted_api=True)
    res.data["key_configured"] = bool(key)
    if psi.error or psi.status != 200:
        res.status = "error"
        if psi.status == 429:
            res.error = ("PSI rate limit exceeded" + ("" if key else
                         " on Google's shared keyless quota; set PAGESPEED_API_KEY (free) and retry"))
            if not key:
                res.status = "needs_key"
        else:
            res.error = psi.error or f"PSI HTTP {psi.status}: {psi.text[:200]}"
        return res.as_dict()
    import json
    parsed = parse_psi(json.loads(psi.body or b"{}"))
    res.data.update({"strategy": strategy, **parsed})
    if crux is not None:
        if crux.status == 200:
            res.data["crux"] = parse_crux(json.loads(crux.body or b"{}"))
        else:
            res.data["crux"] = {"error": "no CrUX data for this URL" if crux.status == 404 else
                                (crux.error or f"HTTP {crux.status}")}
    perf = parsed["scores"].get("performance")
    if perf is not None and perf < 50:
        res.findings.append(finding("lighthouse-performance-poor", "HIGH", f"Lighthouse performance {perf}/100",
                                    fix="Start with the largest opportunities listed."))
    elif perf is not None and perf < 90:
        res.findings.append(finding("lighthouse-performance-average", "MEDIUM", f"Lighthouse performance {perf}/100"))
    seo = parsed["scores"].get("seo")
    if seo is not None and seo < 90:
        res.findings.append(finding("lighthouse-seo", "MEDIUM", f"Lighthouse SEO {seo}/100"))
    for k, v in parsed["field"].items():
        if v["rating"] == "slow" or v["rating"] == "poor":
            label = CWV_THRESHOLDS.get(k.split("_", 1)[1], {}).get("label", k)
            res.findings.append(finding(f"field-{k}", "HIGH", f"Real-user {label} is poor (p75 {v['p75']})",
                                        "Chrome UX Report field data from real visitors."))
    for o in parsed["opportunities"][:5]:
        res.findings.append(finding(f"opportunity-{o['id']}", "LOW", o["title"],
                                    f"Estimated saving {round(o['savings_ms'])} ms"))
    res.summary = {"strategy": strategy, "scores": parsed["scores"],
                   "lcp_ms": (parsed["lab"].get("largest-contentful-paint") or {}).get("value"),
                   "cls": (parsed["lab"].get("cumulative-layout-shift") or {}).get("value"),
                   "field_data": bool(parsed["field"])}
    return res.as_dict()
