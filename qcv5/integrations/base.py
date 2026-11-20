"""Shared plumbing for the free SEO integrations.

Every integration returns one result shape (``IntegrationResult.as_dict()``)::

    {"integration": id, "status": "ok" | "error" | "needs_key" | "unreachable",
     "target": url_or_domain, "summary": {...}, "findings": [{id, severity, title, detail, fix}],
     "data": {...}, "checked_at": iso, "duration_ms": n, "source": "..."}

Requests to the audited site go through ``SafeFetcher``: http/https only, SSRF check on the URL and on every
redirect hop (same guard as the crawler), bounded body size, no cookies kept. Requests to the fixed public APIs
(Google, Bing, IndexNow, archive.org) set ``trusted_api=True``: their host is a constant in this code, never
user input, so only the scheme/host is pinned.

Ported in part from claude-seo (https://github.com/AgriciDaniel/claude-seo, MIT, (c) 2026 agricidaniel);
see qcv5/integrations/NOTICE.md.
"""
from __future__ import annotations

import os
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from qcv5.core.config import settings
from qcv5.crawler.url_normalizer import URLNormalizer

SEVERITIES = ("HIGH", "MEDIUM", "LOW", "INFO")
USER_AGENT = "QC-V5-SEO-Integrations/1.0 (+site audit)"
MAX_BODY = 2_000_000
MAX_REDIRECTS = 5

# Tests (and only tests) swap this for an httpx.MockTransport.
_transport_override: Optional[httpx.AsyncBaseTransport] = None


def set_transport(transport: Optional[httpx.AsyncBaseTransport]) -> None:
    global _transport_override
    _transport_override = transport


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def env_key(*names: str) -> Optional[str]:
    """First non-empty environment variable among ``names`` (keys are never logged or returned)."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return None


def finding(fid: str, severity: str, title: str, detail: str = "", fix: str = "",
            evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    sev = severity.upper()
    if sev not in SEVERITIES:
        raise ValueError(f"bad severity {severity}")
    out = {"id": fid, "severity": sev, "title": title, "detail": detail, "fix": fix}
    if evidence:
        out["evidence"] = evidence
    return out


@dataclass
class IntegrationResult:
    integration: str
    target: str
    status: str = "ok"
    summary: Dict[str, Any] = field(default_factory=dict)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    source: str = ""
    started: float = field(default_factory=time.perf_counter)

    def as_dict(self) -> Dict[str, Any]:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        fs = sorted(self.findings, key=lambda f: order.get(f["severity"], 9))
        counts = {s: sum(1 for f in fs if f["severity"] == s) for s in SEVERITIES}
        return {"integration": self.integration, "status": self.status, "target": self.target,
                "summary": {**self.summary, "findings_by_severity": counts}, "findings": fs, "data": self.data,
                "error": self.error, "source": self.source, "checked_at": utc_now(),
                "duration_ms": round((time.perf_counter() - self.started) * 1000.0, 1)}


class UnsafeURLError(ValueError):
    pass


def normalize_target(url: str) -> str:
    """Accept 'example.com' or a full URL; return an absolute http(s) URL."""
    url = (url or "").strip()
    if not url:
        raise UnsafeURLError("URL is required")
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURLError("Only http(s) URLs with a host are accepted")
    if len(url) > 2048:
        raise UnsafeURLError("URL too long")
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))


def site_root(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def check_safe(url: str) -> None:
    if not URLNormalizer.is_safe_url(url, allow_local=bool(getattr(settings, "ALLOW_LOCAL_TESTING", False))):
        raise UnsafeURLError("URL points to a private, loopback or reserved address (blocked)")


@dataclass
class Fetched:
    url: str
    status: Optional[int] = None
    headers: Optional[httpx.Headers] = None
    body: bytes = b""
    final_url: str = ""
    hops: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_ms: float = 0.0
    truncated: bool = False

    @property
    def text(self) -> str:
        ctype = (self.headers or {}).get("content-type", "") if self.headers is not None else ""
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name) if self.headers is not None else None


class SafeFetcher:
    """Small async HTTP client for integrations (one per integration run)."""

    def __init__(self, timeout: float = 15.0, max_body: int = MAX_BODY):
        self.timeout = timeout
        self.max_body = max_body
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "SafeFetcher":
        kw: Dict[str, Any] = {"timeout": self.timeout, "follow_redirects": False,
                              "headers": {"User-Agent": USER_AGENT}, "trust_env": True}
        if _transport_override is not None:
            kw["transport"] = _transport_override
        self._client = httpx.AsyncClient(**kw)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def request(self, method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                      params: Any = None, json: Any = None, data: Optional[Dict[str, str]] = None, follow: bool = True,
                      trusted_api: bool = False, max_body: Optional[int] = None) -> Fetched:
        assert self._client is not None, "use 'async with SafeFetcher()'"
        limit = max_body or self.max_body
        res = Fetched(url=url)
        current = url
        t0 = time.perf_counter()
        try:
            for hop in range(MAX_REDIRECTS + 1):
                if not trusted_api:
                    check_safe(current)
                req = self._client.build_request(method, current, headers=headers, params=params if hop == 0 else None,
                                                 json=json if hop == 0 else None, data=data if hop == 0 else None)
                resp = await self._client.send(req, stream=True)
                try:
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > limit:
                            res.truncated = True
                            del body[limit:]
                            break
                finally:
                    await resp.aclose()
                loc = resp.headers.get("location")
                if follow and resp.status_code in (301, 302, 303, 307, 308) and loc:
                    nxt = urllib.parse.urljoin(current, loc)
                    res.hops.append({"url": current, "status": resp.status_code, "location": nxt})
                    if urllib.parse.urlsplit(nxt).scheme not in ("http", "https"):
                        raise UnsafeURLError("redirect to a non-http(s) URL")
                    current = nxt
                    if method == "POST":
                        method = "GET"
                    continue
                res.status, res.headers, res.body, res.final_url = resp.status_code, resp.headers, bytes(body), current
                break
            else:
                res.error = f"more than {MAX_REDIRECTS} redirects"
        except UnsafeURLError as e:
            res.error = f"blocked: {e}"
        except httpx.TimeoutException:
            res.error = "timed out"
        except httpx.HTTPError as e:
            res.error = f"{type(e).__name__}: {str(e)[:200]}"
        res.elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return res

    async def get(self, url: str, **kw) -> Fetched:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> Fetched:
        return await self.request("POST", url, **kw)
