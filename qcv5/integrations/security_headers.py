"""Security headers + HTTPS transport: HSTS, CSP, nosniff, framing, Referrer-Policy, Permissions-Policy, cookie
flags, http -> https redirect and www / non-www consolidation.

Security headers are a trust matter, not a confirmed ranking signal; HTTPS is a lightweight one. What matters most
for search: http that never reaches https, and www / non-www both answering 200 (duplicate hosts).
Header grading is ported from claude-seo's transport_check.py (MIT, (c) 2026 agricidaniel). No API key needed.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional

from .base import IntegrationResult, SafeFetcher, finding, normalize_target

ID = "security_headers"
ONE_YEAR = 31_536_000
MAX_COOKIES = 30
_REFERRER_POLICIES = frozenset({
    "no-referrer", "no-referrer-when-downgrade", "origin", "origin-when-cross-origin",
    "same-origin", "strict-origin", "strict-origin-when-cross-origin", "unsafe-url",
})


def _clip(v: Optional[str], n: int = 200) -> Optional[str]:
    return None if v is None else str(v)[:n]


def parse_cookie(raw: str, https: bool) -> Dict[str, Any]:
    """Flags for one Set-Cookie line. Only the NAME is kept; the value is dropped."""
    pieces = [p.strip() for p in raw.split(";")]
    name = pieces[0].split("=", 1)[0].strip() if "=" in pieces[0] else ""
    attrs = {}
    for piece in pieces[1:]:
        k, _s, v = piece.partition("=")
        attrs[k.strip().lower()] = v.strip()
    same_site = attrs.get("samesite")
    c = {"name": name[:80] or "(unnamed)", "secure": "secure" in attrs, "http_only": "httponly" in attrs,
         "same_site": same_site.capitalize()[:10] if same_site else None, "issues": []}
    if https and not c["secure"]:
        c["issues"].append("Missing Secure on an HTTPS site.")
    if not c["http_only"]:
        c["issues"].append("Missing HttpOnly; fine only if scripts must read this cookie.")
    if same_site is None:
        c["issues"].append("No SameSite attribute; browsers default to Lax.")
    elif same_site.lower() == "none" and not c["secure"]:
        c["issues"].append("SameSite=None without Secure is rejected by browsers.")
    return c


def grade_hsts(raw: Optional[str], https: bool) -> Dict[str, Any]:
    h = {"present": raw is not None, "value": _clip(raw), "max_age": None, "include_subdomains": False,
         "preload": False, "preload_ready": False, "issues": []}
    if raw is None:
        if https:
            h["issues"].append("Strict-Transport-Security was not sent.")
        return h
    directives: Dict[str, str] = {}
    for part in raw.split(",")[0].split(";"):          # browsers act on the first header only (RFC 6797)
        k, _s, v = part.strip().partition("=")
        k = k.strip().lower()
        if k in directives:
            h["issues"].append("Duplicate HSTS directives invalidate the header, so browsers ignore it.")
            return h
        if k:
            directives[k] = v.strip().strip('"')
    ma = directives.get("max-age", "")
    h["max_age"] = int(ma) if ma.isascii() and ma.isdigit() else None
    h["include_subdomains"] = "includesubdomains" in directives
    h["preload"] = "preload" in directives
    if h["max_age"] is None:
        h["issues"].append("max-age is missing, so browsers ignore the header.")
    elif h["max_age"] == 0:
        h["issues"].append("max-age=0 switches HSTS off.")
    elif h["max_age"] < ONE_YEAR:
        h["issues"].append("max-age is below one year.")
    if not h["include_subdomains"]:
        h["issues"].append("includeSubDomains is not set.")
    h["preload_ready"] = (h["max_age"] or 0) >= ONE_YEAR and h["include_subdomains"] and h["preload"]
    return h


def _parse_csp(value: str) -> Dict[str, List[str]]:
    d: Dict[str, List[str]] = {}
    for chunk in value.split(";"):
        t = chunk.strip().split()
        if t:
            d.setdefault(t[0].lower(), [x.lower() for x in t[1:]])
    return d


def _csp_issues(d: Dict[str, List[str]]) -> List[str]:
    issues = []
    script = next((n for n in ("script-src", "default-src") if n in d), None)
    if script is None:
        issues.append("No script-src or default-src, so scripts are unrestricted.")
    elif "'unsafe-eval'" in d[script]:
        issues.append(f"'unsafe-eval' in {script}.")
    elem = "script-src-elem" if "script-src-elem" in d else script
    attr = "script-src-attr" if "script-src-attr" in d else script
    for name in dict.fromkeys((elem, attr)):
        if name is None:
            continue
        src = d[name]
        strict = any(s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) for s in src)
        if "'unsafe-inline'" in src and not strict:
            issues.append(f"'unsafe-inline' in {name}.")
        if name == elem:
            for loose in ("*", "https:", "http:", "data:"):
                if loose in src:
                    issues.append(f"{loose} in {name} allows scripts from almost anywhere.")
    if "object-src" not in d and d.get("default-src") != ["'none'"]:
        issues.append("No object-src restriction.")
    if "base-uri" not in d:
        issues.append("No base-uri restriction.")
    return issues


def _restricts_frame_ancestors(sources: List[str]) -> bool:
    return not any(s in {"http:", "https:"} or re.fullmatch(r"(?:https?://)?\*(?::(?:\*|\d+))?/?", s)
                   for s in sources)


def analyze_headers(headers: Dict[str, str], set_cookies: Optional[List[str]] = None, *,
                    https: bool = True) -> Dict[str, Any]:
    """Grade the security headers of one response. Makes no requests; cookie values are never returned."""
    low = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    enforced, report_only = low.get("content-security-policy"), low.get("content-security-policy-report-only")
    policy = enforced if enforced is not None else report_only
    csp = {"present": enforced is not None, "report_only": enforced is None and report_only is not None,
           "issues": []}
    if policy is None:
        csp["issues"].append("Content-Security-Policy was not sent.")
    else:
        pols = [p for p in policy.split(",") if p.strip()]
        csp["issues"].extend(_csp_issues(_parse_csp(pols[0]) if pols else {}))
        if csp["report_only"]:
            csp["issues"].insert(0, "Only a report-only policy was sent, so nothing is enforced.")
    nosniff, xfo = low.get("x-content-type-options"), low.get("x-frame-options")
    xfo_ok = xfo is not None and xfo.strip().upper() in ("DENY", "SAMEORIGIN")
    anc = [_parse_csp(v)["frame-ancestors"] for v in (enforced or "").split(",") if "frame-ancestors" in _parse_csp(v)]
    framing_ok = any(_restricts_frame_ancestors(s) for s in anc) if anc else xfo_ok
    ref = low.get("referrer-policy")
    known = [t.strip().lower() for t in (ref or "").split(",") if t.strip().lower() in _REFERRER_POLICIES]
    effective = known[-1] if known else None
    return {
        "hsts": grade_hsts(low.get("strict-transport-security"), https),
        "csp": csp,
        "x_content_type_options": {"present": nosniff is not None, "value": _clip(nosniff, 60),
                                   "ok": nosniff is not None and nosniff.strip().lower() == "nosniff"},
        "framing": {"x_frame_options": _clip(xfo, 60), "frame_ancestors": bool(anc), "ok": framing_ok},
        "referrer_policy": {"present": ref is not None, "value": _clip(ref, 120),
                            "ok": effective is not None and effective != "unsafe-url"},
        "permissions_policy": {"present": "permissions-policy" in low},
        "cookies": [parse_cookie(c, https) for c in (set_cookies or [])[:MAX_COOKIES] if c.strip()],
    }


def header_findings(h: Dict[str, Any], https: bool) -> List[Dict[str, Any]]:
    out = []
    hsts = h["hsts"]
    if https and not hsts["present"]:
        out.append(finding("hsts-missing", "MEDIUM", "No Strict-Transport-Security header",
                           fix="Send 'Strict-Transport-Security: max-age=31536000; includeSubDomains'."))
    elif https and not hsts["max_age"]:
        out.append(finding("hsts-disabled", "MEDIUM", "HSTS has no effect", "; ".join(hsts["issues"]),
                           "Set a valid max-age (one year or more)."))
    elif https and hsts["issues"]:
        out.append(finding("hsts-weak", "LOW", "HSTS is weak", "; ".join(hsts["issues"]),
                           "Use max-age >= 31536000 with includeSubDomains."))
    csp = h["csp"]
    if not csp["present"] and not csp["report_only"]:
        out.append(finding("csp-missing", "LOW", "No Content-Security-Policy",
                           fix="Add a CSP; start in report-only mode to find breakage."))
    elif csp["report_only"]:
        out.append(finding("csp-report-only", "LOW", "CSP is report-only (nothing enforced)",
                           "; ".join(csp["issues"][:4])))
    elif csp["issues"]:
        out.append(finding("csp-weak", "LOW", "CSP has gaps", "; ".join(csp["issues"][:4])))
    if not h["x_content_type_options"]["ok"]:
        out.append(finding("nosniff-missing", "LOW", "X-Content-Type-Options is not 'nosniff'",
                           fix="Send 'X-Content-Type-Options: nosniff'."))
    if not h["framing"]["ok"]:
        out.append(finding("clickjacking", "LOW", "Framing is not restricted",
                           fix="Send CSP frame-ancestors 'self' (or X-Frame-Options: SAMEORIGIN)."))
    if not h["referrer_policy"]["present"]:
        out.append(finding("referrer-policy-missing", "INFO", "No Referrer-Policy (browser default applies)"))
    elif not h["referrer_policy"]["ok"]:
        out.append(finding("referrer-policy-unsafe", "LOW", "Referrer-Policy is unsafe-url or unknown",
                           h["referrer_policy"]["value"] or ""))
    if not h["permissions_policy"]["present"]:
        out.append(finding("permissions-policy-missing", "INFO", "No Permissions-Policy header"))
    insecure = sorted({c["name"] for c in h["cookies"]
                       if any("Missing Secure" in i or "SameSite=None" in i for i in c["issues"])})
    if insecure:
        out.append(finding("cookie-insecure", "LOW", "Cookies without the Secure flag", ", ".join(insecure[:10]),
                           "Set Secure on every cookie of an HTTPS site."))
    return out


def _swap_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else "www." + host


async def run(url: str, **_: Any) -> Dict[str, Any]:
    target = normalize_target(url)
    p = urllib.parse.urlsplit(target)
    res = IntegrationResult(ID, target, source="claude-seo transport_check.py (header grading)")
    async with SafeFetcher() as f:
        main = await f.get(target, max_body=200_000)
        http_url = urllib.parse.urlunsplit(("http", p.netloc, p.path or "/", p.query, ""))
        plain = await f.get(http_url, follow=False, max_body=10_000)
        sib_url = urllib.parse.urlunsplit((p.scheme, _swap_www(p.hostname or ""), "/", "", ""))
        sibling = await f.get(sib_url, follow=False, max_body=10_000)

    if main.error or main.status is None:
        res.status, res.error = "unreachable", main.error
        res.findings.append(finding("page-unreachable", "HIGH", "Page could not be fetched", main.error or ""))
        return res.as_dict()

    https = main.final_url.startswith("https://")
    cookies = main.headers.get_list("set-cookie") if main.headers is not None else []
    graded = analyze_headers(dict(main.headers or {}), cookies, https=https)
    res.findings.extend(header_findings(graded, https))
    if not https:
        res.findings.append(finding("not-https", "HIGH", "Page is served over plain HTTP",
                                    fix="Serve every page over HTTPS and redirect http -> https."))

    redirects: Dict[str, Any] = {"http_url": http_url, "http_status": plain.status, "http_error": plain.error}
    loc = plain.header("location") if plain.status else None
    redirects["http_redirects_to"] = urllib.parse.urljoin(http_url, loc) if loc else None
    if plain.status and plain.status < 300:
        res.findings.append(finding("http-not-redirected", "HIGH", "http:// answers without redirecting to https",
                                    f"{http_url} -> {plain.status}", "301-redirect every http URL to https."))
    elif loc and not redirects["http_redirects_to"].startswith("https://"):
        res.findings.append(finding("http-redirect-not-https", "MEDIUM", "http:// redirects, but not to https",
                                    redirects["http_redirects_to"]))
    elif plain.status in (302, 303, 307):
        res.findings.append(finding("http-redirect-temporary", "LOW", f"http -> https uses a temporary {plain.status}",
                                    fix="Use 301/308 so the https URL consolidates signals."))

    redirects.update({"sibling_url": sib_url, "sibling_status": sibling.status, "sibling_error": sibling.error})
    if sibling.status and 200 <= sibling.status < 300 and main.status and 200 <= main.status < 300:
        res.findings.append(finding("www-duplicate-host", "MEDIUM", "Both www and non-www answer 200",
                                    f"{sib_url} also returns {sibling.status}",
                                    "301-redirect one host to the other (and keep canonicals consistent)."))

    res.data = {"status": main.status, "final_url": main.final_url, "https": https, "headers": graded,
                "redirects": redirects, "redirect_hops": main.hops}
    score_parts = [graded["hsts"]["present"], graded["csp"]["present"], graded["x_content_type_options"]["ok"],
                   graded["framing"]["ok"], graded["referrer_policy"]["ok"], graded["permissions_policy"]["present"]]
    res.summary = {"https": https, "status": main.status,
                   "security_headers_passed": sum(1 for x in score_parts if x), "security_headers_checked": 6,
                   "http_redirects_to_https": bool(redirects["http_redirects_to"] and
                                                   redirects["http_redirects_to"].startswith("https://")),
                   "cookies": len(graded["cookies"])}
    return res.as_dict()
