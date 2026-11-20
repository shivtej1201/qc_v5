"""IndexNow: tell Bing, Yandex, Seznam, Naver, Yep and Amazon that URLs changed (free, no account).

Protocol (https://www.indexnow.org/documentation): you choose a key (8-128 chars of [a-zA-Z0-9-]), publish it as a
text file at ``https://<host>/<key>.txt`` (or a keyLocation), then POST the URL list to api.indexnow.org.
Ported from claude-seo's indexnow_submit.py (MIT, (c) 2026 agricidaniel). Key: ``INDEXNOW_KEY`` env var or passed in.
"""
from __future__ import annotations

import re
import secrets
import urllib.parse
from typing import Any, Dict, List, Optional

from .base import IntegrationResult, SafeFetcher, env_key, finding, normalize_target

ID = "indexnow"
ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_RE = re.compile(r"^[a-zA-Z0-9-]{8,128}$")
MAX_URLS = 10_000
ENGINES = ["Bing", "Yandex", "Seznam.cz", "Naver", "Yep", "Amazon"]


def generate_key() -> str:
    return secrets.token_hex(16)


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def validate_submission(host: str, key: str, key_location: str, urls: List[str]) -> Optional[str]:
    if not KEY_RE.match(key or ""):
        return "key must be 8-128 characters of a-z, A-Z, 0-9 or '-'"
    if not urls:
        return "no URLs to submit"
    if len(urls) > MAX_URLS:
        return f"at most {MAX_URLS} URLs per request"
    bad = [u for u in urls if _host(u) != host]
    if bad:
        return f"{len(bad)} URL(s) are not on host {host} (IndexNow rejects cross-host submissions)"
    if _host(key_location) != host:
        return "keyLocation must be on the same host"
    scope = key_location.rsplit("/", 1)[0] + "/"
    outside = [u for u in urls if not u.startswith(scope)]
    if outside:
        return f"{len(outside)} URL(s) are outside the keyLocation directory {scope}"
    return None


async def run(url: str, urls: Optional[List[str]] = None, key: Optional[str] = None, submit: bool = False,
              key_location: Optional[str] = None, **_: Any) -> Dict[str, Any]:
    """Verify the key file is published; with ``submit=True`` also submit ``urls`` (default: the target URL)."""
    target = normalize_target(url)
    host = _host(target)
    root = f"{urllib.parse.urlsplit(target).scheme}://{urllib.parse.urlsplit(target).netloc}"
    res = IntegrationResult(ID, target, source="IndexNow protocol; claude-seo indexnow_submit.py")
    key = (key or env_key("INDEXNOW_KEY") or "").strip()
    if not key:
        res.status = "needs_key"
        suggestion = generate_key()
        res.data = {"suggested_key": suggestion, "publish_at": f"{root}/{suggestion}.txt",
                    "file_contents": suggestion, "engines": ENGINES}
        res.error = "No IndexNow key. Publish the suggested key file, then set INDEXNOW_KEY (free, no account)."
        return res.as_dict()
    key_location = key_location or f"{root}/{key}.txt"
    url_list = [normalize_target(u) for u in (urls or [target])][:MAX_URLS + 1]
    async with SafeFetcher() as f:
        kf = await f.get(key_location, max_body=1024)
        published = kf.status == 200 and kf.text.strip() == key
        res.data = {"key_location": key_location, "key_file_status": kf.status, "key_published": published,
                    "engines": ENGINES}
        if not published:
            res.findings.append(finding("indexnow-key-not-published", "HIGH", "IndexNow key file is not published",
                                        f"{key_location} -> {kf.status or kf.error}",
                                        f"Publish a text file at {key_location} containing exactly the key."))
        if submit:
            err = validate_submission(host, key, key_location, url_list)
            if err:
                res.status, res.error = "error", err
                return res.as_dict()
            if not published:
                res.status, res.error = "error", "key file must be published before submitting"
                return res.as_dict()
            r = await f.post(ENDPOINT, json={"host": host, "key": key, "keyLocation": key_location,
                                             "urlList": url_list}, trusted_api=True, follow=False)
            meaning = {200: "accepted", 202: "accepted (key validation pending)", 400: "bad request",
                       403: "key not valid (file missing or mismatched)", 422: "URLs don't match host/key",
                       429: "too many requests"}
            res.data["submission"] = {"status": r.status, "meaning": meaning.get(r.status or 0, r.error or ""),
                                      "submitted": len(url_list)}
            if r.status not in (200, 202):
                res.status, res.error = "error", f"IndexNow answered {r.status}: {meaning.get(r.status or 0, r.error)}"
    res.summary = {"key_published": res.data.get("key_published"),
                   "submitted": (res.data.get("submission") or {}).get("submitted", 0)}
    return res.as_dict()
