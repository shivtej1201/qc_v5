"""
QC V5 — sitemap configuration for on-page audits.

Builds on the existing SitemapExtractor (SSRF check on every hop, robots.txt per URL, gzip, nested
indexes, depth/size/URL limits) and adds what an audit needs:

* discovery from a domain, a homepage or robots.txt (``Sitemap:`` lines, then /sitemap.xml);
* an import snapshot with every sitemap file (tree, fetch status, hygiene stats) and every page URL
  with its sitemap, lastmod, changefreq and priority, saved as JSON so it can be filtered repeatedly;
* selection: choose sitemaps, include/exclude URL patterns, cap the page count;
* saved, reusable sitemap profiles for recurring audits.

Sitemap XML requests are a handful per site and happen before the crawl, outside the crawl budget.
"""
import asyncio
import fnmatch
import json
import os
import re
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from qcv5.audit.seo_audit import lastmod_state

IMPORT_ID_RE = re.compile(r"^smi_[a-f0-9]{12}$")
PROFILE_ID_RE = re.compile(r"^smp_[a-f0-9]{10}$")
MAX_PATTERNS = 50


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_input(raw: str) -> Tuple[str, str]:
    """('sitemap'|'robots'|'site', absolute URL). Bare domains become https://domain/."""
    v = (raw or "").strip()
    if not v:
        raise ValueError("empty input")
    if "://" not in v:
        v = "https://" + v.lstrip("/")
    p = urllib.parse.urlsplit(v)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        raise ValueError(f"not an http(s) URL or domain: {raw!r}")
    path = p.path or "/"
    if path.lower().endswith("/robots.txt"):
        return "robots", v
    if path == "/" and not p.query:
        return "site", f"{p.scheme}://{p.netloc}/"
    return "sitemap", v


def parse_robots_sitemaps(text: str) -> List[str]:
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.lower().startswith("sitemap:"):
            u = line.split(":", 1)[1].strip()
            if u and u not in out:
                out.append(u)
    return out


def _pattern_match(url: str, patterns: List[str]) -> bool:
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            if fnmatch.fnmatchcase(url, pat) or fnmatch.fnmatchcase(urllib.parse.urlsplit(url).path, pat):
                return True
        elif pat in url:
            return True
    return False


def _clean_patterns(v: Any) -> List[str]:
    if isinstance(v, str):
        v = v.splitlines()
    return [str(p).strip()[:300] for p in (v or []) if str(p).strip()][:MAX_PATTERNS]


class SitemapConfigStore:
    """Import snapshots + saved profiles, as JSON files under base_dir (no new infrastructure)."""

    def __init__(self, base_dir: str, allow_local: bool = False):
        self.base_dir = os.path.abspath(base_dir)
        self.imports_dir = os.path.join(self.base_dir, "sitemap_imports")
        self.profiles_path = os.path.join(self.base_dir, "sitemap_profiles.json")
        self.allow_local = allow_local
        os.makedirs(self.imports_dir, exist_ok=True)

    # ------------------------------------------------------------------ discovery + import
    async def discover(self, inputs: List[str], max_urls: int = 50000, max_depth: int = 5,
                       browser_fallback: bool = False) -> Dict[str, Any]:
        from qcv5.crawler.sitemap_extractor import SitemapExtractor   # lazy: keeps the API import light

        if not inputs:
            raise ValueError("enter at least one sitemap URL, robots.txt URL or domain")
        parsed = [_normalize_input(i) for i in inputs[:20]]
        ex = SitemapExtractor(max_depth=max_depth, max_urls=max(1, min(int(max_urls), 200000)),
                              allow_local=self.allow_local, use_browser_fallback=browser_fallback)
        t0 = time.time()
        discovery: List[Dict[str, Any]] = []
        entry_sitemaps: List[str] = []
        try:
            for kind, url in parsed:
                if kind == "sitemap":
                    discovery.append({"input": url, "method": "direct", "sitemaps": [url]})
                    entry_sitemaps.append(url)
                    continue
                root = urllib.parse.urlsplit(url)
                robots_url = url if kind == "robots" else f"{root.scheme}://{root.netloc}/robots.txt"
                body, code, _, _, _ = await ex._safe_fetch(robots_url)
                found = parse_robots_sitemaps(body.decode("utf-8", "replace")) if code == 200 and body else []
                if found:
                    discovery.append({"input": url, "method": "robots.txt", "robots_status": code, "sitemaps": found})
                else:
                    fallback = f"{root.scheme}://{root.netloc}/sitemap.xml"
                    discovery.append({"input": url, "method": "fallback /sitemap.xml", "robots_status": code,
                                      "sitemaps": [fallback]})
                    found = [fallback]
                entry_sitemaps.extend(u for u in found if u not in entry_sitemaps)
            res = await ex.extract_all(entry_sitemaps)
        finally:
            await ex.close()

        now = _utc()
        urls = [{"u": r.get("raw_url") or r["normalized_url"], "sm": r.get("source_sitemap_url", ""), "lastmod": r.get("lastmod", ""),
                 "changefreq": r.get("changefreq", ""), "priority": r.get("priority"),
                 "robots": bool(r.get("robots_allowed", True))}
                for r in res.get("unique_urls", [])]
        per_sm: Dict[str, Dict[str, int]] = {}
        for r in urls:
            c = per_sm.setdefault(r["sm"], {"unique_urls": 0, "robots_blocked": 0, "lastmod_missing": 0,
                                            "lastmod_invalid": 0, "lastmod_future": 0})
            c["unique_urls"] += 1
            c["robots_blocked"] += 0 if r["robots"] else 1
            st = lastmod_state(r["lastmod"] or "", now)
            if st:
                c["lastmod_" + st.lower()] += 1
        sources = []
        for s in res.get("sitemap_sources", []):
            kind = s.get("kind") or ("INDEX" if s.get("status") == "INDEX_PARSED" else "URLSET" if s.get("status") == "PARSED" else "")
            sources.append({
                "sitemap_url": s.get("sitemap_url", ""), "parent_sitemap_url": s.get("parent_sitemap_url", ""),
                "depth": int(s.get("depth") or 0), "kind": kind, "status": s.get("status", ""),
                "http_status": s.get("http_status"), "classification": s.get("classification", ""),
                "urls_listed": int(s.get("urls_found") or 0), "duplicates": int(s.get("duplicates") or 0),
                "rejected": int(s.get("rejected") or 0), "size_bytes": int(s.get("size_bytes") or 0),
                "error_message": str(s.get("error_message") or "")[:300],
                "content_type": str(s.get("content_type") or "")[:120],
                "response_time_ms": float(s.get("response_time_ms") or 0.0),
                "child_sitemap_count": int(s.get("child_sitemap_count") or 0),
                **per_sm.get(s.get("sitemap_url", ""), {"unique_urls": 0, "robots_blocked": 0, "lastmod_missing": 0,
                                                        "lastmod_invalid": 0, "lastmod_future": 0}),
            })
        snap = {
            "import_id": f"smi_{uuid.uuid4().hex[:12]}", "created_at": now, "inputs": [u for _, u in parsed],
            "discovery": discovery, "sources": sources, "urls": urls,
            "summary": {
                "sitemaps": len(sources), "sitemaps_failed": sum(1 for s in sources if s["status"] == "FAILED"),
                "unique_urls": len(urls), "duplicates": int(res.get("duplicate_count") or 0),
                "rejected": int(res.get("rejected_count") or 0),
                "robots_blocked": sum(1 for r in urls if not r["robots"]),
                "completeness": res.get("extraction_completeness", ""), "seconds": round(time.time() - t0, 2),
            },
            "errors": [str(e)[:300] for e in (res.get("errors") or [])][:50],
        }
        self._write_json(os.path.join(self.imports_dir, snap["import_id"] + ".json"), snap)
        return self.describe_import(snap)

    def load_import(self, import_id: str) -> Dict[str, Any]:
        if not IMPORT_ID_RE.match(import_id or ""):
            raise ValueError("invalid sitemap import id")
        p = os.path.join(self.imports_dir, import_id + ".json")
        if not os.path.exists(p):
            raise KeyError(import_id)
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def describe_import(snap: Dict[str, Any]) -> Dict[str, Any]:
        """Everything but the URL list (which can be 100K+ rows)."""
        return {k: snap[k] for k in ("import_id", "created_at", "inputs", "discovery", "sources", "summary", "errors")}

    # ------------------------------------------------------------------ selection
    def select(self, snap: Dict[str, Any], selected_sitemaps: Optional[List[str]] = None,
               include: Any = None, exclude: Any = None, max_pages: int = 0,
               include_robots_blocked: bool = False) -> Dict[str, Any]:
        include, exclude = _clean_patterns(include), _clean_patterns(exclude)
        chosen = set(selected_sitemaps or []) or None
        known = {s["sitemap_url"] for s in snap["sources"]}
        if chosen is not None and not chosen.issubset(known):
            raise ValueError("selected_sitemaps contains sitemaps that are not in this import")
        picked: List[Dict[str, Any]] = []
        excl = {"sitemap": 0, "robots": 0, "include": 0, "exclude": 0, "cap": 0}
        for r in snap["urls"]:
            if chosen is not None and r["sm"] not in chosen:
                excl["sitemap"] += 1
            elif not r["robots"] and not include_robots_blocked:
                excl["robots"] += 1
            elif include and not _pattern_match(r["u"], include):
                excl["include"] += 1
            elif exclude and _pattern_match(r["u"], exclude):
                excl["exclude"] += 1
            elif max_pages and len(picked) >= int(max_pages):
                excl["cap"] += 1
            else:
                picked.append(r)
        return {"urls": picked, "selected_count": len(picked), "excluded": excl,
                "selected_sitemaps": sorted(chosen) if chosen else [s["sitemap_url"] for s in snap["sources"]],
                "include": include, "exclude": exclude, "max_pages": int(max_pages or 0),
                "sample": [r["u"] for r in picked[:15]]}

    @staticmethod
    def audit_meta(snap: Dict[str, Any], sel: Dict[str, Any]) -> Dict[str, Any]:
        """Sidecar the audit worker loads into on_page_sitemap_entries / on_page_sitemap_sources."""
        chosen = set(sel["selected_sitemaps"])
        return {
            "import_id": snap["import_id"],
            "entries": [{"u": r["u"], "sm": r["sm"], "lastmod": r["lastmod"], "changefreq": r["changefreq"],
                         "priority": r["priority"]} for r in sel["urls"]],
            "sources": [{**{k: s.get(k) for k in ("sitemap_url", "parent_sitemap_url", "depth", "kind", "status",
                                                    "http_status", "classification", "urls_listed", "duplicates",
                                                    "rejected", "size_bytes", "error_message", "content_type",
                                                    "response_time_ms", "child_sitemap_count")},
                         "selected": int(s["sitemap_url"] in chosen or s["kind"] == "INDEX")}
                        for s in snap["sources"]],
        }

    # ------------------------------------------------------------------ profiles
    def _profiles(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.profiles_path):
            return []
        with open(self.profiles_path, encoding="utf-8") as f:
            return json.load(f)

    def list_profiles(self) -> List[Dict[str, Any]]:
        return sorted(self._profiles(), key=lambda p: p.get("updated_at", ""), reverse=True)

    def get_profile(self, profile_id: str) -> Dict[str, Any]:
        if not PROFILE_ID_RE.match(profile_id or ""):
            raise ValueError("invalid profile id")
        for p in self._profiles():
            if p["id"] == profile_id:
                return p
        raise KeyError(profile_id)

    def save_profile(self, data: Dict[str, Any], profile_id: Optional[str] = None) -> Dict[str, Any]:
        name = str(data.get("name") or "").strip()[:120]
        inputs = [str(u).strip() for u in (data.get("inputs") or []) if str(u).strip()][:20]
        if not name:
            raise ValueError("profile name is required")
        if not inputs:
            raise ValueError("a profile needs at least one sitemap URL, robots.txt URL or domain")
        for u in inputs:
            _normalize_input(u)
        prof = {
            "name": name, "inputs": inputs,
            "selected_sitemaps": [str(s) for s in (data.get("selected_sitemaps") or [])][:500],
            "include": _clean_patterns(data.get("include")), "exclude": _clean_patterns(data.get("exclude")),
            "max_pages": max(0, int(data.get("max_pages") or 0)),
            "preset": str(data.get("preset") or "conservative"),
            "primary_domain": str(data.get("primary_domain") or "").strip().lower()[:120],
            "overrides": {k: v for k, v in (data.get("overrides") or {}).items() if isinstance(v, (int, float, str))},
        }
        profiles = self._profiles()
        now = _utc()
        if profile_id:
            cur = self.get_profile(profile_id)
            cur.update(prof, updated_at=now)
            profiles = [cur if p["id"] == profile_id else p for p in profiles]
            out = cur
        else:
            out = {"id": f"smp_{uuid.uuid4().hex[:10]}", **prof, "created_at": now, "updated_at": now,
                   "last_run": None}
            profiles.append(out)
        self._write_json(self.profiles_path, profiles)
        return out

    def touch_profile(self, profile_id: str, run: Dict[str, Any]):
        profiles = self._profiles()
        for p in profiles:
            if p["id"] == profile_id:
                p["last_run"] = run
        self._write_json(self.profiles_path, profiles)

    def delete_profile(self, profile_id: str):
        self.get_profile(profile_id)
        self._write_json(self.profiles_path, [p for p in self._profiles() if p["id"] != profile_id])

    @staticmethod
    def _write_json(path: str, obj: Any):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        os.replace(tmp, path)


def run_sync(coro):
    """Run discovery from synchronous code (CLI/tests)."""
    return asyncio.run(coro)
