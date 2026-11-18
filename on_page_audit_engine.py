import asyncio
import concurrent.futures
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.robotparser
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import psutil

from classifier import Classifier
from config import settings
from diagnostics_403 import Diagnostics403
from html_parser import Parser
from status_classifier import StatusClassifier
from url_normalizer import URLNormalizer

NON_HTTP_SCHEMES = {
    "mailto": "MAILTO",
    "tel": "TEL",
    "javascript": "JAVASCRIPT",
    "data": "DATA",
}

TYPE_PRIORITY = {
    "CANONICAL": 1,
    "HREFLANG": 2,
    "ANCHOR": 3,
    "AREA": 4,
    "IFRAME": 5,
    "LINK_META": 6,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def registrable_domain(host: str) -> str:
    h = (host or "").lower().split(":")[0].strip(".")
    parts = h.split(".")
    if len(parts) >= 2:
        # Handle common 2-part ccSLDs like .co.in, .co.uk
        if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "edu") and len(parts[-1]) == 2:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])
    return h


def classify_and_normalize_discovered_url(
    raw_href: str,
    base_url: str,
    source_host: str,
) -> Dict[str, Any]:
    """
    Classify raw href syntax, resolve relative URLs against base_url,
    normalize HTTP(S) URLs per QC V5 URLNormalizer, and classify scope
    (INTERNAL, EXTERNAL, NON_HTTP, FRAGMENT).
    """
    href = (raw_href or "").strip()
    if not href:
        return {
            "valid": False,
            "url_syntax_type": "EMPTY",
            "found_url_scope": "NON_HTTP",
            "normalized_url": "",
            "is_http": False,
        }

    # Pure fragment
    if href.startswith("#"):
        return {
            "valid": True,
            "url_syntax_type": "FRAGMENT",
            "found_url_scope": "FRAGMENT",
            "normalized_url": href,
            "is_http": False,
        }

    lower = href.lower()
    # Explicit scheme check (before urljoin)
    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", href)
    if scheme_match and not href.startswith("//"):
        scheme = scheme_match.group(1).lower()
        if scheme not in ("http", "https"):
            syntax = NON_HTTP_SCHEMES.get(scheme, "NON_HTTP_OTHER")
            return {
                "valid": True,
                "url_syntax_type": syntax,
                "found_url_scope": "NON_HTTP",
                "normalized_url": href[:500],
                "is_http": False,
                "non_http_scheme": scheme,
            }
        syntax_type = "ABSOLUTE"
    elif href.startswith("//"):
        syntax_type = "PROTOCOL_RELATIVE"
    elif href.startswith("/"):
        syntax_type = "ROOT_RELATIVE"
    else:
        syntax_type = "RELATIVE"

    # Resolve against base URL
    resolved = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlsplit(resolved)
    if parsed.scheme.lower() not in ("http", "https"):
        return {
            "valid": True,
            "url_syntax_type": "NON_HTTP_OTHER",
            "found_url_scope": "NON_HTTP",
            "normalized_url": resolved[:500],
            "is_http": False,
            "non_http_scheme": parsed.scheme.lower() or "unknown",
        }

    norm = URLNormalizer.normalize(resolved)
    dest_host = (urllib.parse.urlsplit(norm).hostname or "").lower()
    src_host_clean = (source_host or "").lower()

    if dest_host == src_host_clean or (
        registrable_domain(dest_host) == registrable_domain(src_host_clean)
        and dest_host not in ("", "localhost")
    ):
        scope = "INTERNAL"
    else:
        scope = "EXTERNAL"

    return {
        "valid": True,
        "url_syntax_type": syntax_type,
        "found_url_scope": scope,
        "normalized_url": norm,
        "is_http": True,
    }


def extract_on_page_urls(
    source_page_url: str,
    final_source_url: str,
    html: str,
) -> Tuple[int, List[Dict[str, Any]], Parser]:
    """
    Parse HTML with existing QC V5 Parser and return:
      (raw_occurrences_count, collapsed_per_page_relationships, parser)
    Identical normalized destinations on the same source page are collapsed into
    one relationship with occurrence_count, anchor_texts, link_types, and
    representative_location (Section 14).
    """
    base_url = final_source_url or source_page_url
    parser = Parser(base_url)
    if html:
        try:
            parser.feed(html)
        except Exception:
            pass

    raw_elements = parser.get_all_on_page_urls()
    raw_count = len(raw_elements)
    source_host = (urllib.parse.urlsplit(base_url).hostname or "").lower()

    # Collapse per normalized destination on this source page
    grouped: Dict[str, Dict[str, Any]] = {}
    for el in raw_elements:
        raw_href = (el.get("href") or "").strip()
        if not raw_href:
            continue
        meta = classify_and_normalize_discovered_url(raw_href, parser.final_base_href, source_host)
        if not meta["valid"]:
            continue

        norm_key = meta["normalized_url"]
        if not norm_key:
            continue

        el_type = el.get("url_type", "ANCHOR")
        anchor_text = " ".join((el.get("text") or "").split())[:250]
        rel = (el.get("rel") or "").strip()
        hreflang = (el.get("hreflang") or "").strip()
        loc = el.get("component") or "Content Section"

        if norm_key not in grouped:
            grouped[norm_key] = {
                "found_url": raw_href[:1000],
                "normalized_found_url": norm_key,
                "found_url_type": el_type,
                "url_syntax_type": meta["url_syntax_type"],
                "found_url_scope": meta["found_url_scope"],
                "is_http": meta["is_http"],
                "non_http_scheme": meta.get("non_http_scheme", ""),
                "anchor_text": anchor_text,
                "anchor_texts": [anchor_text] if anchor_text else [],
                "link_types": [el_type],
                "rel": rel,
                "hreflang": hreflang,
                "occurrence_count": 1,
                "representative_location": loc,
            }
        else:
            rec = grouped[norm_key]
            rec["occurrence_count"] += 1
            if el_type not in rec["link_types"]:
                rec["link_types"].append(el_type)
            if TYPE_PRIORITY.get(el_type, 99) < TYPE_PRIORITY.get(rec["found_url_type"], 99):
                rec["found_url_type"] = el_type
            if anchor_text and anchor_text not in rec["anchor_texts"] and len(rec["anchor_texts"]) < 10:
                rec["anchor_texts"].append(anchor_text)
            if not rec["anchor_text"] and anchor_text:
                rec["anchor_text"] = anchor_text
            if not rec["rel"] and rel:
                rec["rel"] = rel
            if not rec["hreflang"] and hreflang:
                rec["hreflang"] = hreflang

    return raw_count, list(grouped.values()), parser


def classify_http_outcome(
    initial_status: Optional[int],
    final_status: Optional[int],
    redirect_count: int,
    error_type: str,
    error_message: str,
    robots_allowed: bool,
    ssrf_safe: bool,
    waf_diag: Optional[Dict[str, Any]],
) -> Tuple[str, str, str, str, str]:
    """
    Returns (terminal_state, status_bucket, status_classification, remark, waf_status)
      terminal_state in ('VALIDATED', 'FAILED', 'BLOCKED', 'TIMED_OUT')
      status_bucket in ('2xx', '3xx', '4xx', '5xx', 'Timeout', 'DNS/Network', 'SSL',
                        'Robots blocked', 'WAF blocked', 'Other errors')
    """
    if not ssrf_safe:
        return (
            "BLOCKED",
            "Other errors",
            "Blocked (SSRF)",
            "Blocked (SSRF) — Unsafe or Prohibited Destination",
            "NONE",
        )

    if not robots_allowed:
        return (
            "BLOCKED",
            "Robots blocked",
            "ROBOTS_BLOCKED",
            "ROBOTS_BLOCKED — Disallowed by robots.txt",
            "NONE",
        )

    if error_type:
        if error_type == "Timeout":
            return ("TIMED_OUT", "Timeout", "Timeout", "Timeout — Request Timed Out", "NONE")
        if error_type in ("DNS Error", "Connection Error", "Network Error"):
            return (
                "FAILED",
                "DNS/Network",
                error_type,
                f"{error_type} — {error_message[:140] if error_message else 'Network failure'}",
                "NONE",
            )
        if error_type == "SSL Error":
            return (
                "FAILED",
                "SSL",
                "SSL Error",
                f"SSL Error — {error_message[:140] if error_message else 'TLS handshake failed'}",
                "NONE",
            )
        if error_type == "Redirect Loop":
            return (
                "FAILED",
                "Other errors",
                "Redirect Loop",
                "Redirect Loop / Excessive Redirects",
                "NONE",
            )
        return (
            "FAILED",
            "Other errors",
            error_type,
            f"{error_type} — {error_message[:140] if error_message else 'Crawl error'}",
            "NONE",
        )

    cat = StatusClassifier.classify(final_status or 0)
    waf_status = "NONE"

    if waf_diag:
        waf_cls = waf_diag.get("classification", "")
        if waf_cls in ("CDN_WAF_BLOCKED_OR_CHALLENGED", "BOT_DETECTION_CHALLENGE", "RATE_LIMITED"):
            waf_status = waf_cls
            if final_status == 429:
                return (
                    "BLOCKED",
                    "WAF blocked",
                    "Rate Limited",
                    "429 Rate Limited — CDN/WAF or Origin Throttling",
                    waf_status,
                )
            return (
                "BLOCKED",
                "WAF blocked",
                "CDN_WAF_BLOCKED_OR_CHALLENGED",
                f"403 CDN_WAF_BLOCKED_OR_CHALLENGED — {waf_diag.get('what_happened', 'Blocked by CDN/WAF')[:120]}",
                waf_status,
            )
        elif waf_cls:
            waf_status = waf_cls

    if redirect_count > 0 or (initial_status is not None and 300 <= initial_status <= 399):
        init_code = initial_status if initial_status is not None else 301
        if final_status is not None and 200 <= final_status <= 299:
            return (
                "VALIDATED",
                "3xx",
                f"Redirect -> {cat}",
                f"{init_code} Redirect -> {final_status} OK (Redirected but Healthy)",
                waf_status,
            )
        if final_status is not None and 400 <= final_status <= 499:
            return (
                "VALIDATED",
                "3xx",
                f"Redirect -> {cat}",
                f"{init_code} Redirect -> {final_status} {cat} (Redirected to Broken URL)",
                waf_status,
            )
        if final_status is not None and 500 <= final_status <= 599:
            return (
                "VALIDATED",
                "3xx",
                f"Redirect -> {cat}",
                f"{init_code} Redirect -> {final_status} Server Error (Redirected to Server Error)",
                waf_status,
            )
        return ("VALIDATED", "3xx", f"Redirect -> {cat}", f"{init_code} Redirect -> {final_status} {cat}", waf_status)

    if final_status is not None and 200 <= final_status <= 299:
        return ("VALIDATED", "2xx", cat, f"{final_status} OK — Working", waf_status)

    if final_status is not None and 400 <= final_status <= 499:
        if final_status == 404:
            remark = "404 Not Found — Broken URL"
        elif final_status == 410:
            remark = "410 Gone — Permanently Removed"
        elif final_status == 403:
            remark = "403 HTTP_403 — Origin Forbidden"
            return ("VALIDATED", "4xx", "HTTP_403", remark, waf_status or "ORIGIN_FORBIDDEN")
        elif final_status == 429:
            return ("BLOCKED", "WAF blocked", "Rate Limited", "429 Rate Limited — Edge/Origin Rate Limit", "RATE_LIMITED")
        else:
            remark = f"{final_status} {cat} — Client Error"
        return ("VALIDATED", "4xx", cat, remark, waf_status)

    if final_status is not None and 500 <= final_status <= 599:
        return ("VALIDATED", "5xx", cat, f"{final_status} Server Error — Origin Failure", waf_status)

    return ("FAILED", "Other errors", cat or "Unknown", f"Unexpected HTTP Status ({final_status})", waf_status)


class RobotsCache:
    """Async per-host robots.txt fetcher & cache respecting QC V5 security."""

    def __init__(self, allow_local: bool = False, before_fetch=None):
        self.allow_local = allow_local
        self._cache: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        # Optional async hook(url) awaited before the robots.txt GET (used to charge
        # primary-domain robots fetches to the shared primary rate budget).
        self.before_fetch = before_fetch
        self.fetch_count = 0

    async def can_fetch(self, client: httpx.AsyncClient, url: str) -> bool:
        if not getattr(settings, "RESPECT_ROBOTS", True):
            return True
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin not in self._cache:
            # Check-and-create has no await in between, so it is atomic on the event loop:
            # exactly one Lock per origin, and exactly one robots.txt GET per origin.
            if origin not in self._locks:
                self._locks[origin] = asyncio.Lock()
            async with self._locks[origin]:
                if origin not in self._cache:
                    robots_url = f"{origin}/robots.txt"
                    if not URLNormalizer.is_safe_url(robots_url, allow_local=self.allow_local):
                        self._cache[origin] = None
                    else:
                        rp = urllib.robotparser.RobotFileParser()
                        try:
                            if self.before_fetch is not None:
                                await self.before_fetch(robots_url)
                            self.fetch_count += 1
                            r = await client.get(robots_url, timeout=8.0)
                            if r.status_code == 200 and r.text:
                                rp.parse(r.text.splitlines())
                                self._cache[origin] = rp
                            else:
                                self._cache[origin] = None
                        except Exception:
                            self._cache[origin] = None
        rp = self._cache.get(origin)
        if rp is None:
            return True
        try:
            return rp.can_fetch("*", url)
        except Exception:
            return True


class OnPageAuditEngine:
    """
    Production QC V5 Complete On-Page URL Extraction + Status Audit Engine.
    Integrates directly into existing QC V5 SQLite WAL DB, Classifier, Parser,
    StatusClassifier, URLNormalizer, Diagnostics403, robots.txt, and polite
    Akamai throttling/cooldown recovery.
    """

    def __init__(
        self,
        db_path: str = settings.DB_FILE,
        job_id: Optional[str] = None,
        job_name: str = "QC V5 On-Page URL Extraction + Status Audit",
        allow_local: bool = False,
        primary_domain: str = "bajajfinserv.in",
        target_rps: float = 8.0,
        min_rps: float = 3.0,
        max_rps: float = 10.0,
        source_concurrency: int = 10,
        dest_concurrency: int = 20,
        per_host_concurrency: int = 6,
        akamai_cooldown_sec: float = 120.0,
        max_retries: Optional[int] = None,
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
        batch_size: int = 250,
        progress_json_path: Optional[str] = None,
        # ---- performance / adaptive-control tunables (defaults are conservative) ----
        parse_workers: int = 2,
        flush_interval_sec: float = 1.0,
        reconcile_interval_sec: float = 900.0,
        progress_interval_sec: float = 2.0,
        rps_increase_step: float = 0.25,
        rps_increase_window: int = 2000,
        waf_cooldown_threshold: int = 5,
        probe_url: Optional[str] = None,
        probe_interval_sec: float = 45.0,
        http2: bool = False,
        keepalive_expiry: float = 30.0,
        discard_body_limit: int = 256 * 1024,
        primary_hosts: Optional[List[str]] = None,
    ):
        self.db_path = db_path
        self.job_id = job_id or str(uuid.uuid4())
        self.job_name = job_name
        self.allow_local = allow_local
        self.primary_domain = primary_domain.lower()
        self.target_rps = target_rps
        self.min_rps = min_rps
        self.max_rps = max_rps
        self.source_concurrency = max(1, min(source_concurrency, settings.PER_DOMAIN_CONCURRENCY))
        self.dest_concurrency = max(1, min(dest_concurrency, settings.LINK_AUDIT_CONCURRENCY))
        self.per_host_concurrency = max(1, min(per_host_concurrency, settings.LINK_AUDIT_PER_HOST_CONCURRENCY))
        self.akamai_cooldown_sec = akamai_cooldown_sec
        self.max_retries = settings.MAX_RETRIES if max_retries is None else max_retries
        self.connect_timeout = settings.CONNECT_TIMEOUT if connect_timeout is None else connect_timeout
        self.read_timeout = settings.READ_TIMEOUT if read_timeout is None else read_timeout
        self.batch_size = batch_size
        self.progress_json_path = progress_json_path

        self.parse_workers = max(1, int(parse_workers))
        self.flush_interval_sec = flush_interval_sec
        self.reconcile_interval_sec = reconcile_interval_sec
        self.progress_interval_sec = progress_interval_sec
        self.rps_increase_step = rps_increase_step
        self.rps_increase_window = max(1, int(rps_increase_window))
        self.waf_cooldown_threshold = max(1, int(waf_cooldown_threshold))
        self.probe_url = probe_url
        self.probe_interval_sec = probe_interval_sec
        self.http2 = http2
        self.keepalive_expiry = keepalive_expiry
        self.discard_body_limit = discard_body_limit
        self.primary_hosts = {h.lower().strip() for h in (primary_hosts or []) if h and h.strip()}

        self.classifier = Classifier()
        self.robots = RobotsCache(allow_local=allow_local, before_fetch=self._pace_if_primary)
        self.conn: Optional[sqlite3.Connection] = None

        # Primary-domain polite pacemaker & Akamai cooldown gate.
        # ONE authoritative budget: source fetches, destination validations, every redirect hop
        # and robots.txt fetches to primary hosts all reserve slots from the same virtual clock.
        self._next_slot_ts = 0.0
        self._cooldown_epoch = 0
        self._cooldown_lock = asyncio.Lock()
        self._cooldown_gate = asyncio.Event()
        self._cooldown_gate.set()
        self.consecutive_waf = 0
        self.cooldown_count = 0
        self._window_ok = 0
        self._stop_requested = False

        # ---- Destination registry (in-memory front of on_page_validation_cache) ----
        # normalized_url -> terminal validation_state for every URL whose result is known
        self._dest_state: Dict[str, str] = {}
        # normalized_url -> Future owned by the single queued/in-flight validation of that URL
        self._inflight: Dict[str, asyncio.Future] = {}
        # referenced destinations that are also still-pending sitemap sources: the source
        # fetch will satisfy them, so they are NOT sent to the destination queue
        self._awaiting_source: Dict[str, str] = {}
        self._pending_source_norms: Set[str] = set()
        self._dest_outstanding = 0
        self._bg_tasks: Set[asyncio.Task] = set()

        # ---- Writer-owned exact mirror of persisted state (replaces hot-path COUNT/GROUP BY) ----
        self._w_cache: Dict[str, List[Any]] = {}
        self._mirror: Dict[str, Any] = {}
        self._mirror_dirty = False
        self._rels_discovered = 0
        self._run_rel_base = 0
        self._run_sources_done = 0
        self._job_status = "RUNNING"
        self._write_q: Optional[asyncio.Queue] = None
        self._dest_q: Optional[asyncio.Queue] = None
        self._dest_idle: Optional[asyncio.Event] = None

        self.metrics: Dict[str, Any] = self._new_metrics()

        # Per-host semaphores for external & secondary hosts
        self._host_sems: Dict[str, asyncio.Semaphore] = {}
        self._host_sem_lock = asyncio.Lock()

        # In-memory job-scoped validation cache tracking (loaded from DB on resume)
        self._seen_http_dests: Set[str] = set()

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        conn.execute("PRAGMA cache_size=-131072;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY,
                filename TEXT,
                mode TEXT,
                status TEXT,
                created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                upload_id TEXT,
                sheet_name TEXT,
                total_records INTEGER DEFAULT 0,
                processed_records INTEGER DEFAULT 0,
                status TEXT DEFAULT 'QUEUED',
                worker_id TEXT,
                lease_expires_at TIMESTAMP,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                force_recheck BOOLEAN DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS url_results (
                normalized_url TEXT PRIMARY KEY,
                original_url TEXT,
                initial_status_code INTEGER,
                final_status_code INTEGER,
                status TEXT,
                final_url TEXT,
                redirect_count INTEGER,
                redirect_chain TEXT,
                response_time_ms REAL,
                content_type TEXT,
                content_length INTEGER,
                error_type TEXT,
                error_message TEXT,
                check_method TEXT,
                browser_verified TEXT,
                page_type TEXT,
                business TEXT,
                classification_method TEXT,
                checked_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS source_classifications (
                source_url TEXT PRIMARY KEY,
                page_type TEXT,
                business TEXT,
                classification_method TEXT,
                tag_1 TEXT,
                tag_2 TEXT,
                diagnostic_notes TEXT,
                classified_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS on_page_audit_jobs (
                job_id TEXT PRIMARY KEY,
                name TEXT,
                status TEXT DEFAULT 'QUEUED',
                source_pages_total INTEGER DEFAULT 0,
                source_pages_pending INTEGER DEFAULT 0,
                source_pages_completed INTEGER DEFAULT 0,
                source_pages_failed INTEGER DEFAULT 0,
                source_pages_blocked INTEGER DEFAULT 0,
                source_pages_timed_out INTEGER DEFAULT 0,
                source_pages_explicitly_excluded INTEGER DEFAULT 0,
                url_occurrences_extracted INTEGER DEFAULT 0,
                url_relationships_discovered INTEGER DEFAULT 0,
                url_relationships_persisted INTEGER DEFAULT 0,
                explicitly_excluded_relationships INTEGER DEFAULT 0,
                unique_destination_urls INTEGER DEFAULT 0,
                unique_http_destinations INTEGER DEFAULT 0,
                unique_http_destinations_validated INTEGER DEFAULT 0,
                unique_http_destinations_failed INTEGER DEFAULT 0,
                unique_http_destinations_blocked INTEGER DEFAULT 0,
                unique_http_destinations_timed_out INTEGER DEFAULT 0,
                unique_http_destinations_excluded INTEGER DEFAULT 0,
                new_http_validations INTEGER DEFAULT 0,
                reused_http_validations INTEGER DEFAULT 0,
                cache_freshness_hours REAL DEFAULT 24.0,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS on_page_source_pages (
                job_id TEXT NOT NULL,
                source_page_url TEXT NOT NULL,
                source_page_id TEXT NOT NULL,
                source_sitemap TEXT DEFAULT '',
                source_sitemap_number INTEGER DEFAULT 0,
                normalized_source_url TEXT NOT NULL,
                crawl_state TEXT DEFAULT 'PENDING',
                initial_http_status INTEGER,
                http_status INTEGER,
                final_url TEXT,
                redirect_count INTEGER DEFAULT 0,
                redirect_chain TEXT DEFAULT '[]',
                content_type TEXT DEFAULT '',
                response_time_ms REAL DEFAULT 0.0,
                error_type TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                robots_status TEXT DEFAULT 'PENDING',
                waf_status TEXT DEFAULT 'NONE',
                business TEXT DEFAULT 'NEEDS_REVIEW',
                language TEXT DEFAULT 'UNKNOWN',
                page_type TEXT DEFAULT 'NEEDS_REVIEW',
                classification_method TEXT DEFAULT '',
                raw_occurrences_count INTEGER DEFAULT 0,
                relationships_count INTEGER DEFAULT 0,
                remark TEXT DEFAULT '',
                checked_at TIMESTAMP,
                PRIMARY KEY (job_id, source_page_url)
            );
            CREATE INDEX IF NOT EXISTS idx_opsp_state ON on_page_source_pages(job_id, crawl_state);
            CREATE INDEX IF NOT EXISTS idx_opsp_id ON on_page_source_pages(job_id, source_page_id);

            CREATE TABLE IF NOT EXISTS on_page_validation_cache (
                job_id TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                url_scope TEXT NOT NULL,
                validation_state TEXT DEFAULT 'PENDING',
                initial_http_status INTEGER,
                http_status INTEGER,
                final_url TEXT,
                redirect_count INTEGER DEFAULT 0,
                redirect_chain TEXT DEFAULT '[]',
                response_time_ms REAL DEFAULT 0.0,
                content_type TEXT DEFAULT '',
                status_bucket TEXT DEFAULT 'PENDING',
                status_classification TEXT DEFAULT 'Pending',
                remark TEXT DEFAULT 'Pending Validation',
                error_type TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                robots_status TEXT DEFAULT 'ALLOWED',
                waf_status TEXT DEFAULT 'NONE',
                first_seen_source_url TEXT DEFAULT '',
                reuse_count INTEGER DEFAULT 0,
                referenced INTEGER DEFAULT 1,
                checked_at TIMESTAMP,
                PRIMARY KEY (job_id, normalized_url)
            );
            CREATE INDEX IF NOT EXISTS idx_opvc_state ON on_page_validation_cache(job_id, referenced, validation_state);
            CREATE INDEX IF NOT EXISTS idx_opvc_bucket ON on_page_validation_cache(job_id, status_bucket);

            CREATE TABLE IF NOT EXISTS on_page_link_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                source_page_url TEXT NOT NULL,
                source_page_status INTEGER,
                source_page_final_url TEXT,
                found_url TEXT NOT NULL,
                normalized_found_url TEXT NOT NULL,
                found_url_type TEXT NOT NULL,
                url_syntax_type TEXT NOT NULL,
                found_url_scope TEXT NOT NULL,
                anchor_text TEXT DEFAULT '',
                anchor_texts TEXT DEFAULT '[]',
                link_types TEXT DEFAULT '[]',
                rel TEXT DEFAULT '',
                hreflang TEXT DEFAULT '',
                occurrence_count INTEGER DEFAULT 1,
                representative_location TEXT DEFAULT '',
                business TEXT DEFAULT 'NEEDS_REVIEW',
                language TEXT DEFAULT 'UNKNOWN',
                page_type TEXT DEFAULT 'NEEDS_REVIEW',
                validation_source TEXT NOT NULL,
                remark TEXT DEFAULT '',
                extracted_at TIMESTAMP,
                UNIQUE (job_id, source_page_url, normalized_found_url)
            );
            CREATE INDEX IF NOT EXISTS idx_oplr_job ON on_page_link_relationships(job_id);
            CREATE INDEX IF NOT EXISTS idx_oplr_dest ON on_page_link_relationships(job_id, normalized_found_url);
            CREATE INDEX IF NOT EXISTS idx_oplr_scope ON on_page_link_relationships(job_id, found_url_scope);
            CREATE INDEX IF NOT EXISTS idx_oplr_valsrc ON on_page_link_relationships(job_id, validation_source);
        """)
        conn.commit()
        try:
            conn.execute("ALTER TABLE on_page_validation_cache ADD COLUMN referenced INTEGER DEFAULT 1")
            conn.commit()
        except Exception:
            pass

    def initialize_inventory(self, source_entries: List[Dict[str, Any]]) -> str:
        """
        Seed authoritative source-page inventory into SQLite idempotently (safe on resume!).
        Each entry needs at least {'original_url': ..., 'normalized_url': ...}
        and optionally 'source_sitemap', 'source_sitemap_number'.
        """
        if self.conn is None:
            self.conn = self._connect_db()

        now = utc_now_iso()
        total = len(source_entries)

        existing = self.conn.execute(
            "SELECT job_id FROM on_page_audit_jobs WHERE job_id = ?", (self.job_id,)
        ).fetchone()

        if not existing:
            upload_id = str(uuid.uuid4())
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
                    (upload_id, f"{self.job_name}.json", "on_page_audit", "PROCESSING", now),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.job_id, upload_id, self.job_name, total, 0, "QUEUED", now, now),
                )
                self.conn.execute(
                    "INSERT INTO on_page_audit_jobs (job_id, name, status, source_pages_total, source_pages_pending, created_at, updated_at) "
                    "VALUES (?, ?, 'QUEUED', ?, ?, ?, ?)",
                    (self.job_id, self.job_name, total, total, now, now),
                )

                rows = []
                for idx, entry in enumerate(source_entries, 1):
                    orig = (entry.get("original_url") or entry.get("url") or "").strip()
                    norm = (entry.get("normalized_url") or URLNormalizer.normalize(orig)).strip()
                    rows.append((
                        self.job_id,
                        orig,
                        f"SRC_{idx:06d}",
                        entry.get("source_sitemap", ""),
                        int(entry.get("source_sitemap_number", 0) or 0),
                        norm,
                        URLNormalizer.detect_language(orig).upper() or "UNKNOWN",
                    ))
                    if len(rows) >= 5000:
                        self.conn.executemany(
                            """
                            INSERT OR IGNORE INTO on_page_source_pages (
                                job_id, source_page_url, source_page_id, source_sitemap,
                                source_sitemap_number, normalized_source_url, language
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            rows,
                        )
                        rows.clear()
                if rows:
                    self.conn.executemany(
                        """
                        INSERT OR IGNORE INTO on_page_source_pages (
                            job_id, source_page_url, source_page_id, source_sitemap,
                            source_sitemap_number, normalized_source_url, language
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )

        self.sync_job_counters()
        return self.job_id

    @staticmethod
    def _new_metrics() -> Dict[str, Any]:
        return {
            "started_monotonic": time.monotonic(),
            "http_requests": 0,              # every network request incl. redirect hops & retries
            "primary_requests": 0,           # requests charged to the shared primary budget
            "source_fetches": 0,             # source pages fetched (final attempts + WAF requeues)
            "dest_validations": 0,           # destination validations that hit the network
            "dest_validations_skipped_cached": 0,
            "source_seeded_validations": 0,  # destinations satisfied by a source-page fetch
            "status_counts": Counter(),
            "status_403": 0,
            "status_429": 0,
            "waf_events": 0,
            "timeouts": 0,
            "network_errors": 0,
            "latencies_ms": [],              # bounded reservoir
            "db_flushes": 0,
            "db_flush_ms": [],               # bounded reservoir
            "db_rows_written": 0,
            "write_q_peak": 0,
            "dest_q_peak": 0,
            "parse_q_peak": 0,
            "reconciliations": 0,
            "reconciliation_mismatches": 0,
            "rss_peak_mb": 0.0,
            "cpu_samples": [],
            "rps_history": [],
        }

    @staticmethod
    def _reservoir_add(lst: List[float], v: float, cap: int = 20000):
        if len(lst) < cap:
            lst.append(v)
        else:
            lst[int(time.monotonic() * 1e6) % cap] = v

    def _is_primary_host(self, url: str) -> bool:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host:
            return False
        if host in self.primary_hosts:
            return True
        return bool(self.primary_domain) and (
            host == self.primary_domain or host.endswith("." + self.primary_domain)
        )

    async def _get_host_sem(self, url: str) -> asyncio.Semaphore:
        host = (urllib.parse.urlsplit(url).hostname or "unknown").lower()
        sem = self._host_sems.get(host)
        if sem is not None:
            return sem
        async with self._host_sem_lock:
            if host not in self._host_sems:
                cap = self.source_concurrency if self._is_primary_host(url) else self.per_host_concurrency
                self._host_sems[host] = asyncio.Semaphore(cap)
            return self._host_sems[host]

    async def _acquire_primary_pace(self):
        """
        Reserve the next slot on the single primary-domain virtual clock (strict spacing of
        1/target_rps, no burst allowance).

        - No lock: the read-modify-write of _next_slot_ts contains no await, so it is atomic
          on the event loop (the old _pace_lock only added an extra scheduling hop per request).
        - No busy waiting: one asyncio.sleep until the reserved slot.
        - No thundering herd after cooldown: a slot reserved before/while a cooldown started is
          void (epoch check); the waiter re-reserves after the gate reopens, and the cooldown
          resets the clock, so released workers leave one interval apart instead of all at once.
        """
        while True:
            await self._cooldown_gate.wait()
            epoch = self._cooldown_epoch
            now = time.monotonic()
            interval = 1.0 / max(0.5, self.target_rps)
            slot = self._next_slot_ts if self._next_slot_ts > now else now
            self._next_slot_ts = slot + interval
            wait_s = slot - now
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            if self._cooldown_gate.is_set() and epoch == self._cooldown_epoch:
                self.metrics["primary_requests"] += 1
                return

    async def _pace_if_primary(self, url: str):
        if self._is_primary_host(url):
            await self._acquire_primary_pace()

    def _note_primary_ok(self):
        """Adaptive increase (shared by source + destination traffic: same edge, same budget)."""
        self.consecutive_waf = 0
        self._window_ok += 1
        if self._window_ok >= self.rps_increase_window and self.target_rps < self.max_rps:
            old = self.target_rps
            self.target_rps = min(self.max_rps, round(self.target_rps + self.rps_increase_step, 2))
            self._window_ok = 0
            self.metrics["rps_history"].append((round(time.monotonic() - self.metrics["started_monotonic"], 1), old, self.target_rps, "increase"))

    def _note_primary_waf(self) -> bool:
        """Returns True when the existing consecutive-WAF threshold says a cooldown must start."""
        self.consecutive_waf += 1
        self.metrics["waf_events"] += 1
        return self.consecutive_waf >= self.waf_cooldown_threshold

    async def _wait_for_primary_ready(self, client: httpx.AsyncClient, initial_wait: bool = False):
        if not self.probe_url and (not self.primary_domain or self.primary_domain in ("127.0.0.1", "localhost")):
            return
        if initial_wait and self.akamai_cooldown_sec > 0:
            print(f"[COOLDOWN] Waiting {self.akamai_cooldown_sec:.0f}s for Akamai edge window reset...", flush=True)
            await asyncio.sleep(self.akamai_cooldown_sec)
        probe_url = self.probe_url or f"https://www.{self.primary_domain}/robots.txt"
        for attempt in range(1, 12):
            try:
                self.metrics["http_requests"] += 1
                r = await client.get(probe_url, timeout=10.0)
                if r.status_code == 200:
                    print(f"[READY] Primary edge open ({probe_url} -> 200 on probe #{attempt})", flush=True)
                    return
                print(f"[COOLDOWN] Probe #{attempt}: HTTP {r.status_code}; waiting {self.probe_interval_sec:.0f}s...", flush=True)
            except Exception as e:
                print(f"[COOLDOWN] Probe #{attempt} error: {e}; waiting {self.probe_interval_sec:.0f}s...", flush=True)
            await asyncio.sleep(self.probe_interval_sec)

    async def _trigger_primary_cooldown(self, client: httpx.AsyncClient):
        if not self._cooldown_gate.is_set():
            await self._cooldown_gate.wait()
            return
        async with self._cooldown_lock:
            if not self._cooldown_gate.is_set():
                return
            self._cooldown_gate.clear()
            self._cooldown_epoch += 1
            self.cooldown_count += 1
            old_rps = self.target_rps
            self.target_rps = max(self.min_rps, round(self.target_rps * 0.85, 2))
            self.consecutive_waf = 0
            self._window_ok = 0
            self.metrics["rps_history"].append((round(time.monotonic() - self.metrics["started_monotonic"], 1), old_rps, self.target_rps, "cooldown"))
            print(
                f"\n[AKAMAI COOLDOWN #{self.cooldown_count}] Stepping target_rps {old_rps} -> {self.target_rps} req/s...",
                flush=True,
            )
            try:
                await self._wait_for_primary_ready(client, initial_wait=True)
            finally:
                self._next_slot_ts = time.monotonic()
                self._cooldown_gate.set()

    async def _discard_body(self, resp: httpx.Response):
        """
        Destination checks do not need the body. Drain small bodies as raw (still-compressed)
        bytes so the keep-alive connection can be reused without paying gzip decode; for large
        or unbounded bodies (PDFs, media) stop early and let httpx close the connection instead
        of downloading megabytes. The HTTP status is already authoritative at this point.
        """
        try:
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > self.discard_body_limit:
                return
            n = 0
            async for chunk in resp.aiter_raw():
                n += len(chunk)
                if n > self.discard_body_limit:
                    return
        except Exception:
            return

    async def fetch_url_manual_redirects(
        self,
        client: httpx.AsyncClient,
        url: str,
        need_body: bool = True,
        max_hops: int = 10,
    ) -> Dict[str, Any]:
        """
        Fetch URL with:
        - SSRF validation on initial URL and every redirect hop
        - Per-host robots.txt check
        - Polite Akamai pacing on primary domain (every primary hop is charged to the budget)
        - Manual hop-by-hop redirects with loop detection
        - Bounded retries with backoff on transient 429/502/503/504/network errors
        - Diagnostics403 WAF classification
        - need_body=False: body is not downloaded/decoded except for 403/429 diagnostics
        """
        # 1. SSRF validation
        if not URLNormalizer.is_safe_url(url, allow_local=self.allow_local):
            term, bkt, scls, rem, waf_st = classify_http_outcome(
                None, None, 0, "", "", True, False, None
            )
            return {
                "terminal_state": term,
                "initial_http_status": None,
                "http_status": None,
                "final_url": url,
                "redirect_count": 0,
                "redirect_chain": [],
                "response_time_ms": 0.0,
                "content_type": "",
                "status_bucket": bkt,
                "status_classification": scls,
                "remark": rem,
                "error_type": "Blocked (SSRF)",
                "error_message": "Blocked by SSRF policy",
                "robots_status": "ALLOWED",
                "waf_status": waf_st,
                "html": "",
                "is_primary_waf": False,
            }

        # 2. Robots validation
        robots_ok = await self.robots.can_fetch(client, url)
        if not robots_ok:
            term, bkt, scls, rem, waf_st = classify_http_outcome(
                None, None, 0, "", "", False, True, None
            )
            return {
                "terminal_state": term,
                "initial_http_status": None,
                "http_status": None,
                "final_url": url,
                "redirect_count": 0,
                "redirect_chain": [],
                "response_time_ms": 0.0,
                "content_type": "",
                "status_bucket": bkt,
                "status_classification": scls,
                "remark": rem,
                "error_type": "ROBOTS_BLOCKED",
                "error_message": "Disallowed by robots.txt",
                "robots_status": "DISALLOWED",
                "waf_status": waf_st,
                "html": "",
                "is_primary_waf": False,
            }

        is_primary = self._is_primary_host(url)
        host_sem = await self._get_host_sem(url)
        m = self.metrics

        t0 = time.time()
        retries = 0
        initial_status: Optional[int] = None
        final_status: Optional[int] = None
        final_url = url
        redirect_chain: List[Dict[str, Any]] = []
        content_type = ""
        error_type = ""
        error_message = ""
        ssrf_safe = True
        raw_bytes = b""
        resp_headers: Dict[str, str] = {}

        while retries <= self.max_retries:
            current_url = url
            redirect_chain = []
            visited_in_chain = {url}
            final_status = None
            initial_status = None
            content_type = ""
            error_type = ""
            error_message = ""
            ssrf_safe = True
            raw_bytes = b""
            resp_headers = {}

            try:
                # Concurrency slot first, then the rate slot: a request leaves at (about) its
                # reserved time instead of reserving a slot and then queueing on the semaphore,
                # which used to let several requests fire back-to-back when slots freed up.
                async with host_sem:
                    for _hop in range(max_hops + 1):
                        if not URLNormalizer.is_safe_url(current_url, allow_local=self.allow_local):
                            ssrf_safe = False
                            break

                        if (is_primary and _hop == 0) or (_hop > 0 and self._is_primary_host(current_url)):
                            await self._acquire_primary_pace()

                        t_req = time.perf_counter()
                        m["http_requests"] += 1
                        async with client.stream("GET", current_url) as resp:
                            st = resp.status_code
                            m["status_counts"][st] += 1
                            if st in (301, 302, 303, 307, 308):
                                location = resp.headers.get("Location") or resp.headers.get("location")
                                redirect_chain.append({"url": current_url, "status": st})
                                if not location:
                                    final_status = st
                                    final_url = current_url
                                    content_type = resp.headers.get("content-type", "")
                                    resp_headers = dict(resp.headers)
                                    await self._discard_body(resp)
                                    break
                                await self._discard_body(resp)
                                next_url = URLNormalizer.normalize(urllib.parse.urljoin(current_url, location))
                                if next_url in visited_in_chain or len(redirect_chain) >= max_hops:
                                    error_type = "Redirect Loop"
                                    error_message = f"Redirect loop or exceeded {max_hops} hops at {next_url}"
                                    final_url = next_url
                                    break
                                visited_in_chain.add(next_url)
                                current_url = next_url
                                continue
                            else:
                                final_status = st
                                final_url = str(resp.url)
                                content_type = resp.headers.get("content-type", "")
                                resp_headers = dict(resp.headers)
                                if need_body or st in (403, 429):
                                    raw_bytes = (await resp.aread())[: settings.MAX_PAYLOAD_SIZE_BYTES]
                                else:
                                    await self._discard_body(resp)
                                self._reservoir_add(m["latencies_ms"], (time.perf_counter() - t_req) * 1000.0)
                                break

                initial_status = redirect_chain[0]["status"] if redirect_chain else final_status
                if final_status == 403:
                    m["status_403"] += 1
                elif final_status == 429:
                    m["status_429"] += 1

                if not ssrf_safe or error_type == "Redirect Loop":
                    break
                if final_status in (502, 503, 504) and retries < self.max_retries:
                    retries += 1
                    await asyncio.sleep(1.0 * retries)
                    continue
                if final_status == 429 and retries < self.max_retries:
                    retries += 1
                    await asyncio.sleep(2.0 * retries)
                    continue
                break

            except httpx.TimeoutException as e:
                error_type = "Timeout"
                error_message = str(e)[:200]
                m["timeouts"] += 1
                retries += 1
                if retries <= self.max_retries:
                    await asyncio.sleep(1.0 * retries)
            except Exception as e:
                err_l = str(e).lower()
                if "timeout" in err_l or "timed out" in err_l:
                    error_type = "Timeout"
                    m["timeouts"] += 1
                elif "dns" in err_l or "getaddrinfo" in err_l or "name_not_resolved" in err_l or "resolve" in err_l:
                    error_type = "DNS Error"
                elif "ssl" in err_l or "cert" in err_l or "tls" in err_l:
                    error_type = "SSL Error"
                elif "connect" in err_l:
                    error_type = "Connection Error"
                else:
                    error_type = "Network Error"
                if error_type != "Timeout":
                    m["network_errors"] += 1
                error_message = str(e)[:200]
                retries += 1
                if retries <= self.max_retries:
                    await asyncio.sleep(1.0 * retries)

        elapsed_ms = round((time.time() - t0) * 1000.0, 2)
        html_text = ""
        if raw_bytes and ("html" in content_type.lower() or not content_type or final_status in (403, 429)):
            html_text = raw_bytes.decode("utf-8", errors="ignore")
        raw_bytes = b""

        waf_diag = None
        is_primary_waf = False
        if not error_type and final_status in (403, 429):
            waf_diag = Diagnostics403.classify(
                status_code=final_status,
                headers=resp_headers,
                body_snippet=html_text[:4096],
                stage="page_crawl",
                robots_allowed=True,
                redirect_chain=redirect_chain,
                error_type=error_type,
            )
            if is_primary and waf_diag.get("classification") in (
                "CDN_WAF_BLOCKED_OR_CHALLENGED",
                "BOT_DETECTION_CHALLENGE",
                "RATE_LIMITED",
            ):
                is_primary_waf = True

        term, bkt, scls, rem, waf_st = classify_http_outcome(
            initial_status=initial_status,
            final_status=final_status,
            redirect_count=len(redirect_chain),
            error_type=error_type,
            error_message=error_message,
            robots_allowed=True,
            ssrf_safe=ssrf_safe,
            waf_diag=waf_diag,
        )

        return {
            "terminal_state": term,
            "initial_http_status": initial_status,
            "http_status": final_status,
            "final_url": final_url,
            "redirect_count": len(redirect_chain),
            "redirect_chain": redirect_chain,
            "response_time_ms": elapsed_ms,
            "content_type": content_type[:120],
            "status_bucket": bkt,
            "status_classification": scls,
            "remark": rem,
            "error_type": error_type if error_type else ("" if (final_status and final_status < 400 and ssrf_safe) else scls),
            "error_message": error_message,
            "robots_status": "ALLOWED",
            "waf_status": waf_st,
            "html": html_text if need_body else "",
            "is_primary_waf": is_primary_waf,
        }

    def _exec_with_retry(self, fn):
        for attempt in range(10):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 9:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise

    def _flush_source_batch_sync(
        self,
        source_updates: List[Dict[str, Any]],
        rel_inserts: List[Dict[str, Any]],
        cache_seed_inserts: List[Dict[str, Any]],
        cache_reuse_increments: Counter,
        source_self_cache_upserts: List[Dict[str, Any]],
    ):
        def _do():
            with self.conn:
                self._write_source_parts(
                    source_updates, rel_inserts, cache_seed_inserts,
                    cache_reuse_increments, source_self_cache_upserts,
                )

        self._exec_with_retry(_do)

    def _write_source_parts(
        self,
        source_updates: List[Dict[str, Any]],
        rel_inserts: List[Dict[str, Any]],
        cache_seed_inserts: List[Dict[str, Any]],
        cache_reuse_increments: Counter,
        source_self_cache_upserts: List[Dict[str, Any]],
    ) -> int:
        """Execute the source-side statements inside the CALLER's transaction.
        Returns the exact number of relationship rows inserted."""
        rel_rowcount = 0
        if source_updates:
            self.conn.executemany(
                """
                UPDATE on_page_source_pages SET
                    crawl_state = :crawl_state,
                    initial_http_status = :initial_http_status,
                    http_status = :http_status,
                    final_url = :final_url,
                    redirect_count = :redirect_count,
                    redirect_chain = :redirect_chain,
                    content_type = :content_type,
                    response_time_ms = :response_time_ms,
                    error_type = :error_type,
                    error_message = :error_message,
                    robots_status = :robots_status,
                    waf_status = :waf_status,
                    business = :business,
                    language = :language,
                    page_type = :page_type,
                    classification_method = :classification_method,
                    raw_occurrences_count = :raw_occurrences_count,
                    relationships_count = :relationships_count,
                    remark = :remark,
                    checked_at = :checked_at
                WHERE job_id = :job_id AND source_page_url = :source_page_url
                """,
                source_updates,
            )
            # Mirror classifications into QC V5 source_classifications
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO source_classifications (
                    source_url, page_type, business, classification_method,
                    tag_1, tag_2, diagnostic_notes, classified_at
                ) VALUES (
                    :source_page_url, :page_type, :business, :classification_method,
                    :tag_1, :tag_2, :remark, :checked_at
                )
                """,
                source_updates,
            )

        if cache_seed_inserts:
            self.conn.executemany(
                """
                INSERT INTO on_page_validation_cache (
                    job_id, normalized_url, url_scope, validation_state,
                    first_seen_source_url, reuse_count, referenced
                ) VALUES (
                    :job_id, :normalized_url, :url_scope, 'PENDING',
                    :first_seen_source_url, 0, 1
                )
                ON CONFLICT(job_id, normalized_url) DO UPDATE SET
                    referenced = 1
                """,
                cache_seed_inserts,
            )

        # Store live source-page fetch result into validation cache if this URL is also referenced
        # (or pre-seed it with referenced=0 so any later source page linking to it reuses this exact same-job live result!)
        if source_self_cache_upserts:
            self.conn.executemany(
                """
                INSERT INTO on_page_validation_cache (
                    job_id, normalized_url, url_scope, validation_state,
                    initial_http_status, http_status, final_url, redirect_count,
                    redirect_chain, response_time_ms, content_type, status_bucket,
                    status_classification, remark, error_type, error_message,
                    robots_status, waf_status, first_seen_source_url, reuse_count, referenced, checked_at
                ) VALUES (
                    :job_id, :normalized_url, 'INTERNAL', :validation_state,
                    :initial_http_status, :http_status, :final_url, :redirect_count,
                    :redirect_chain, :response_time_ms, :content_type, :status_bucket,
                    :status_classification, :remark, :error_type, :error_message,
                    :robots_status, :waf_status, :first_seen_source_url, 0, 0, :checked_at
                )
                ON CONFLICT(job_id, normalized_url) DO UPDATE SET
                    validation_state = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.validation_state ELSE on_page_validation_cache.validation_state END,
                    initial_http_status = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.initial_http_status ELSE on_page_validation_cache.initial_http_status END,
                    http_status = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.http_status ELSE on_page_validation_cache.http_status END,
                    final_url = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.final_url ELSE on_page_validation_cache.final_url END,
                    redirect_count = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.redirect_count ELSE on_page_validation_cache.redirect_count END,
                    redirect_chain = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.redirect_chain ELSE on_page_validation_cache.redirect_chain END,
                    response_time_ms = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.response_time_ms ELSE on_page_validation_cache.response_time_ms END,
                    content_type = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.content_type ELSE on_page_validation_cache.content_type END,
                    status_bucket = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.status_bucket ELSE on_page_validation_cache.status_bucket END,
                    status_classification = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.status_classification ELSE on_page_validation_cache.status_classification END,
                    remark = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.remark ELSE on_page_validation_cache.remark END,
                    error_type = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.error_type ELSE on_page_validation_cache.error_type END,
                    error_message = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.error_message ELSE on_page_validation_cache.error_message END,
                    robots_status = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.robots_status ELSE on_page_validation_cache.robots_status END,
                    waf_status = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.waf_status ELSE on_page_validation_cache.waf_status END,
                    checked_at = CASE WHEN on_page_validation_cache.validation_state = 'PENDING'
                        THEN excluded.checked_at ELSE on_page_validation_cache.checked_at END
                """,
                source_self_cache_upserts,
            )

        if rel_inserts:
            rel_rowcount = self.conn.executemany(
                """
                INSERT OR IGNORE INTO on_page_link_relationships (
                    job_id, source_page_url, source_page_status, source_page_final_url,
                    found_url, normalized_found_url, found_url_type, url_syntax_type,
                    found_url_scope, anchor_text, anchor_texts, link_types, rel, hreflang,
                    occurrence_count, representative_location, business, language, page_type,
                    validation_source, remark, extracted_at
                ) VALUES (
                    :job_id, :source_page_url, :source_page_status, :source_page_final_url,
                    :found_url, :normalized_found_url, :found_url_type, :url_syntax_type,
                    :found_url_scope, :anchor_text, :anchor_texts, :link_types, :rel, :hreflang,
                    :occurrence_count, :representative_location, :business, :language, :page_type,
                    :validation_source, :remark, :extracted_at
                )
                """,
                rel_inserts,
            ).rowcount

        if cache_reuse_increments:
            self.conn.executemany(
                "UPDATE on_page_validation_cache SET reuse_count = reuse_count + ? WHERE job_id = ? AND normalized_url = ?",
                [(cnt, self.job_id, u) for u, cnt in cache_reuse_increments.items()],
            )
        return rel_rowcount

    def _flush_dest_batch_sync(self, dest_updates: List[Dict[str, Any]]):
        if not dest_updates:
            return

        def _do():
            with self.conn:
                self._write_dest_parts(dest_updates)

        self._exec_with_retry(_do)

    def _write_dest_parts(self, dest_updates: List[Dict[str, Any]]):
        """Destination results inside the CALLER's transaction. UPSERT (not bare UPDATE) so the
        result can never be silently dropped if it is committed in the same batch as its seed."""
        self.conn.executemany(
            """
            INSERT INTO on_page_validation_cache (
                job_id, normalized_url, url_scope, validation_state,
                initial_http_status, http_status, final_url, redirect_count,
                redirect_chain, response_time_ms, content_type, status_bucket,
                status_classification, remark, error_type, error_message,
                robots_status, waf_status, first_seen_source_url, reuse_count, referenced, checked_at
            ) VALUES (
                :job_id, :normalized_url, :url_scope, :validation_state,
                :initial_http_status, :http_status, :final_url, :redirect_count,
                :redirect_chain, :response_time_ms, :content_type, :status_bucket,
                :status_classification, :remark, :error_type, :error_message,
                :robots_status, :waf_status, '', 0, 1, :checked_at
            )
            ON CONFLICT(job_id, normalized_url) DO UPDATE SET
                validation_state = excluded.validation_state,
                initial_http_status = excluded.initial_http_status,
                http_status = excluded.http_status,
                final_url = excluded.final_url,
                redirect_count = excluded.redirect_count,
                redirect_chain = excluded.redirect_chain,
                response_time_ms = excluded.response_time_ms,
                content_type = excluded.content_type,
                status_bucket = excluded.status_bucket,
                status_classification = excluded.status_classification,
                remark = excluded.remark,
                error_type = excluded.error_type,
                error_message = excluded.error_message,
                robots_status = excluded.robots_status,
                waf_status = excluded.waf_status,
                checked_at = excluded.checked_at
            """,
            dest_updates,
        )
        # Mirror validated destination results into QC V5 url_results
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO url_results (
                normalized_url, original_url, initial_status_code, final_status_code,
                status, final_url, redirect_count, redirect_chain, response_time_ms,
                content_type, content_length, error_type, error_message, check_method,
                browser_verified, checked_at
            ) VALUES (
                :normalized_url, :normalized_url, :initial_http_status, :http_status,
                :status_classification, :final_url, :redirect_count, :redirect_chain,
                :response_time_ms, :content_type, 0, :error_type, :error_message,
                'HTTP', 'NO', :checked_at
            )
            """,
            dest_updates,
        )

    def sync_job_counters(self, status_override: Optional[str] = None) -> Dict[str, Any]:
        """Compute exact authoritative populations directly from SQLite and update on_page_audit_jobs + jobs."""
        def _do():
            now = utc_now_iso()
            # 1. Source page population
            src_counts = dict(
                self.conn.execute(
                    "SELECT crawl_state, COUNT(*) FROM on_page_source_pages WHERE job_id = ? GROUP BY crawl_state",
                    (self.job_id,),
                ).fetchall()
            )
            src_total = sum(src_counts.values())
            src_pending = src_counts.get("PENDING", 0)
            src_completed = src_counts.get("COMPLETED", 0)
            src_failed = src_counts.get("FAILED", 0)
            src_blocked = src_counts.get("BLOCKED", 0)
            src_timed_out = src_counts.get("TIMED_OUT", 0)
            src_excluded = src_counts.get("EXPLICITLY_EXCLUDED", 0)
            src_processed = src_completed + src_failed + src_blocked + src_timed_out + src_excluded

            # 2. Relationships & occurrences
            occ_sum = self.conn.execute(
                "SELECT COALESCE(SUM(raw_occurrences_count), 0) FROM on_page_source_pages WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0]
            rel_total = self.conn.execute(
                "SELECT COUNT(*) FROM on_page_link_relationships WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0]
            val_src_counts = dict(
                self.conn.execute(
                    "SELECT validation_source, COUNT(*) FROM on_page_link_relationships WHERE job_id = ? GROUP BY validation_source",
                    (self.job_id,),
                ).fetchall()
            )
            new_checks = val_src_counts.get("NEW_CHECK", 0)
            reused_checks = val_src_counts.get("REUSED", 0)

            # 3. Unique HTTP destinations (indexed O(1) lookup via referenced=1!)
            dest_counts = dict(
                self.conn.execute(
                    """
                    SELECT validation_state, COUNT(*)
                    FROM on_page_validation_cache
                    WHERE job_id = ? AND referenced = 1
                    GROUP BY validation_state
                    """,
                    (self.job_id,),
                ).fetchall()
            )
            uniq_http = sum(dest_counts.values())
            uniq_non_http = self.conn.execute(
                """
                SELECT COUNT(DISTINCT normalized_found_url)
                FROM on_page_link_relationships
                WHERE job_id = ? AND found_url_scope IN ('NON_HTTP', 'FRAGMENT')
                """,
                (self.job_id,),
            ).fetchone()[0]
            uniq_all_dests = uniq_http + uniq_non_http
            dest_validated = dest_counts.get("VALIDATED", 0)
            dest_failed = dest_counts.get("FAILED", 0)
            dest_blocked = dest_counts.get("BLOCKED", 0)
            dest_timed_out = dest_counts.get("TIMED_OUT", 0)
            dest_excluded = dest_counts.get("EXPLICITLY_EXCLUDED", 0)

            cur_status = status_override
            if cur_status is None:
                row = self.conn.execute(
                    "SELECT status FROM on_page_audit_jobs WHERE job_id = ?", (self.job_id,)
                ).fetchone()
                cur_status = row[0] if row else "RUNNING"

            with self.conn:
                self.conn.execute(
                    """
                    UPDATE on_page_audit_jobs SET
                        status = ?,
                        source_pages_total = ?,
                        source_pages_pending = ?,
                        source_pages_completed = ?,
                        source_pages_failed = ?,
                        source_pages_blocked = ?,
                        source_pages_timed_out = ?,
                        source_pages_explicitly_excluded = ?,
                        url_occurrences_extracted = ?,
                        url_relationships_discovered = ?,
                        url_relationships_persisted = ?,
                        explicitly_excluded_relationships = 0,
                        unique_destination_urls = ?,
                        unique_http_destinations = ?,
                        unique_http_destinations_validated = ?,
                        unique_http_destinations_failed = ?,
                        unique_http_destinations_blocked = ?,
                        unique_http_destinations_timed_out = ?,
                        unique_http_destinations_excluded = ?,
                        new_http_validations = ?,
                        reused_http_validations = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        cur_status,
                        src_total,
                        src_pending,
                        src_completed,
                        src_failed,
                        src_blocked,
                        src_timed_out,
                        src_excluded,
                        occ_sum,
                        rel_total,
                        rel_total,
                        uniq_all_dests,
                        uniq_http,
                        dest_validated,
                        dest_failed,
                        dest_blocked,
                        dest_timed_out,
                        dest_excluded,
                        new_checks,
                        reused_checks,
                        now,
                        self.job_id,
                    ),
                )
                self.conn.execute(
                    "UPDATE jobs SET total_records = ?, processed_records = ?, status = ?, updated_at = ? WHERE id = ?",
                    (src_total, src_processed, cur_status, now, self.job_id),
                )

            return {
                "job_id": self.job_id,
                "status": cur_status,
                "source_pages_total": src_total,
                "source_pages_processed": src_processed,
                "source_pages_pending": src_pending,
                "source_pages_completed": src_completed,
                "source_pages_failed": src_failed,
                "source_pages_blocked": src_blocked,
                "source_pages_timed_out": src_timed_out,
                "source_pages_explicitly_excluded": src_excluded,
                "url_occurrences_extracted": occ_sum,
                "url_relationships_discovered": rel_total,
                "url_relationships_persisted": rel_total,
                "unique_destination_urls": uniq_all_dests,
                "unique_http_destinations": uniq_http,
                "unique_http_destinations_validated": dest_validated,
                "unique_http_destinations_failed": dest_failed,
                "unique_http_destinations_blocked": dest_blocked,
                "unique_http_destinations_timed_out": dest_timed_out,
                "unique_http_destinations_excluded": dest_excluded,
                "new_http_validations": new_checks,
                "reused_http_validations": reused_checks,
                "updated_at": now,
            }

        stats = self._exec_with_retry(_do)
        if self.progress_json_path:
            tmp = self.progress_json_path + ".tmp"
            os.makedirs(os.path.dirname(self.progress_json_path), exist_ok=True)
            proc = psutil.Process()
            stats_out = dict(stats)
            stats_out["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
            stats_out["target_rps"] = self.target_rps
            stats_out["akamai_cooldowns_triggered"] = self.cooldown_count
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(stats_out, f, indent=2)
            os.replace(tmp, self.progress_json_path)
        return stats

    # =====================================================================================
    # Writer-owned exact counter mirror (replaces COUNT(*)/GROUP BY in the hot path)
    # =====================================================================================
    _SRC_STATES = ("PENDING", "COMPLETED", "FAILED", "BLOCKED", "TIMED_OUT", "EXPLICITLY_EXCLUDED")
    _DEST_STATES = ("PENDING", "VALIDATED", "FAILED", "BLOCKED", "TIMED_OUT", "EXPLICITLY_EXCLUDED")
    _MIRROR_KEYS = (
        "source_pages_total", "source_pages_pending", "source_pages_completed", "source_pages_failed",
        "source_pages_blocked", "source_pages_timed_out", "source_pages_explicitly_excluded",
        "url_occurrences_extracted", "url_relationships_persisted", "unique_destination_urls",
        "unique_http_destinations", "unique_http_destinations_validated", "unique_http_destinations_failed",
        "unique_http_destinations_blocked", "unique_http_destinations_timed_out",
        "unique_http_destinations_excluded", "new_http_validations", "reused_http_validations",
    )

    def _mirror_from_stats(self, stats: Dict[str, Any]):
        dest_known = (
            stats["unique_http_destinations_validated"] + stats["unique_http_destinations_failed"]
            + stats["unique_http_destinations_blocked"] + stats["unique_http_destinations_timed_out"]
            + stats["unique_http_destinations_excluded"]
        )
        nonhttp = {
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT normalized_found_url FROM on_page_link_relationships "
                "WHERE job_id = ? AND found_url_scope IN ('NON_HTTP', 'FRAGMENT')",
                (self.job_id,),
            ).fetchall()
        }
        self._mirror = {
            "src": Counter({
                "PENDING": stats["source_pages_pending"],
                "COMPLETED": stats["source_pages_completed"],
                "FAILED": stats["source_pages_failed"],
                "BLOCKED": stats["source_pages_blocked"],
                "TIMED_OUT": stats["source_pages_timed_out"],
                "EXPLICITLY_EXCLUDED": stats["source_pages_explicitly_excluded"],
            }),
            "occ": stats["url_occurrences_extracted"],
            "rel": stats["url_relationships_persisted"],
            "valsrc": Counter({"NEW_CHECK": stats["new_http_validations"], "REUSED": stats["reused_http_validations"]}),
            "dest": Counter({
                "PENDING": stats["unique_http_destinations"] - dest_known,
                "VALIDATED": stats["unique_http_destinations_validated"],
                "FAILED": stats["unique_http_destinations_failed"],
                "BLOCKED": stats["unique_http_destinations_blocked"],
                "TIMED_OUT": stats["unique_http_destinations_timed_out"],
                "EXPLICITLY_EXCLUDED": stats["unique_http_destinations_excluded"],
            }),
            "nonhttp": nonhttp,
        }
        self._mirror_dirty = False

    def _stats_from_counts(self, src: Counter, occ: int, rel: int, valsrc: Counter, dest: Counter,
                           nonhttp_count: int, status: str) -> Dict[str, Any]:
        src_total = sum(src[s] for s in self._SRC_STATES)
        src_processed = src_total - src["PENDING"]
        uniq_http = sum(dest[s] for s in self._DEST_STATES)
        return {
            "job_id": self.job_id,
            "status": status,
            "source_pages_total": src_total,
            "source_pages_processed": src_processed,
            "source_pages_pending": src["PENDING"],
            "source_pages_completed": src["COMPLETED"],
            "source_pages_failed": src["FAILED"],
            "source_pages_blocked": src["BLOCKED"],
            "source_pages_timed_out": src["TIMED_OUT"],
            "source_pages_explicitly_excluded": src["EXPLICITLY_EXCLUDED"],
            "url_occurrences_extracted": occ,
            "url_relationships_discovered": max(self._rels_discovered, rel),
            "url_relationships_persisted": rel,
            "unique_destination_urls": uniq_http + nonhttp_count,
            "unique_http_destinations": uniq_http,
            "unique_http_destinations_validated": dest["VALIDATED"],
            "unique_http_destinations_failed": dest["FAILED"],
            "unique_http_destinations_blocked": dest["BLOCKED"],
            "unique_http_destinations_timed_out": dest["TIMED_OUT"],
            "unique_http_destinations_excluded": dest["EXPLICITLY_EXCLUDED"],
            "new_http_validations": valsrc["NEW_CHECK"],
            "reused_http_validations": valsrc["REUSED"],
            "updated_at": utc_now_iso(),
        }

    def _mirror_stats(self) -> Dict[str, Any]:
        m = self._mirror
        return self._stats_from_counts(m["src"], m["occ"], m["rel"], m["valsrc"], m["dest"], len(m["nonhttp"]), self._job_status)

    def _write_job_row(self, st: Dict[str, Any]):
        """Single-row UPDATEs (O(1)) — what the realtime UI polls. Runs inside caller's txn."""
        self.conn.execute(
            """
            UPDATE on_page_audit_jobs SET
                status = ?, source_pages_total = ?, source_pages_pending = ?, source_pages_completed = ?,
                source_pages_failed = ?, source_pages_blocked = ?, source_pages_timed_out = ?,
                source_pages_explicitly_excluded = ?, url_occurrences_extracted = ?,
                url_relationships_discovered = ?, url_relationships_persisted = ?,
                explicitly_excluded_relationships = 0, unique_destination_urls = ?, unique_http_destinations = ?,
                unique_http_destinations_validated = ?, unique_http_destinations_failed = ?,
                unique_http_destinations_blocked = ?, unique_http_destinations_timed_out = ?,
                unique_http_destinations_excluded = ?, new_http_validations = ?, reused_http_validations = ?,
                updated_at = ?
            WHERE job_id = ?
            """,
            (
                st["status"], st["source_pages_total"], st["source_pages_pending"], st["source_pages_completed"],
                st["source_pages_failed"], st["source_pages_blocked"], st["source_pages_timed_out"],
                st["source_pages_explicitly_excluded"], st["url_occurrences_extracted"],
                st["url_relationships_discovered"], st["url_relationships_persisted"],
                st["unique_destination_urls"], st["unique_http_destinations"],
                st["unique_http_destinations_validated"], st["unique_http_destinations_failed"],
                st["unique_http_destinations_blocked"], st["unique_http_destinations_timed_out"],
                st["unique_http_destinations_excluded"], st["new_http_validations"], st["reused_http_validations"],
                st["updated_at"], self.job_id,
            ),
        )
        self.conn.execute(
            "UPDATE jobs SET total_records = ?, processed_records = ?, status = ?, updated_at = ? WHERE id = ?",
            (st["source_pages_total"], st["source_pages_processed"], st["status"], st["updated_at"], self.job_id),
        )

    def _flush_pipeline_sync(
        self,
        src_updates: List[Dict[str, Any]],
        rels: List[Dict[str, Any]],
        seeds: List[Dict[str, Any]],
        incs: Counter,
        self_seeds: List[Dict[str, Any]],
        dest_updates: List[Dict[str, Any]],
        status: str,
    ) -> Dict[str, Any]:
        """
        One SQLite transaction for everything buffered (source pages + relationships + cache seeds +
        source-seeded results + destination results + the job progress row). Counter deltas are
        derived from exactly what this transaction writes, mirroring the SQL statement order, and
        are only applied to the in-memory mirror after a successful commit.
        """
        def _do():
            overlay: Dict[str, Optional[List[Any]]] = {}

            def cur(u: str) -> Optional[List[Any]]:
                if u not in overlay:
                    e = self._w_cache.get(u)
                    overlay[u] = list(e) if e is not None else None
                return overlay[u]

            d_dest: Counter = Counter()
            # (1) NEW_CHECK cache seeds: INSERT PENDING/referenced=1, or flip referenced 0 -> 1
            for sd in seeds:
                u = sd["normalized_url"]
                e = cur(u)
                if e is None:
                    overlay[u] = ["PENDING", 1]
                    d_dest["PENDING"] += 1
                elif not e[1]:
                    e[1] = 1
                    d_dest[e[0]] += 1
            # (2) source-seeded results: insert (referenced=0) or fill only a PENDING row
            for sc in self_seeds:
                u, st = sc["normalized_url"], sc["validation_state"]
                e = cur(u)
                if e is None:
                    overlay[u] = [st, 0]
                elif e[0] == "PENDING":
                    if e[1]:
                        d_dest["PENDING"] -= 1
                        d_dest[st] += 1
                    e[0] = st
            # (3) destination results: UPSERT
            for dr in dest_updates:
                u, st = dr["normalized_url"], dr["validation_state"]
                e = cur(u)
                if e is None:
                    overlay[u] = [st, 1]
                    d_dest[st] += 1
                else:
                    if e[1]:
                        d_dest[e[0]] -= 1
                        d_dest[st] += 1
                    e[0] = st

            d_src: Counter = Counter()
            occ = 0
            for su in src_updates:
                d_src["PENDING"] -= 1
                d_src[su["crawl_state"]] += 1
                occ += int(su["raw_occurrences_count"] or 0)
            d_val = Counter(r["validation_source"] for r in rels)
            nonhttp_new = {
                r["normalized_found_url"] for r in rels if r["found_url_scope"] in ("NON_HTTP", "FRAGMENT")
            } - self._mirror["nonhttp"]

            m = self._mirror
            src_c = Counter(m["src"]); dest_c = Counter(m["dest"]); val_c = Counter(m["valsrc"])
            for k, v in d_src.items():
                src_c[k] += v
            for k, v in d_dest.items():
                dest_c[k] += v
            for k, v in d_val.items():
                val_c[k] += v

            with self.conn:
                rc = self._write_source_parts(src_updates, rels, seeds, incs, self_seeds)
                if dest_updates:
                    self._write_dest_parts(dest_updates)
                stats = self._stats_from_counts(
                    src_c, m["occ"] + occ, m["rel"] + rc, val_c, dest_c, len(m["nonhttp"]) + len(nonhttp_new), status
                )
                self._write_job_row(stats)
            return overlay, src_c, occ, rc, val_c, dest_c, nonhttp_new, stats

        overlay, src_c, occ, rc, val_c, dest_c, nonhttp_new, stats = self._exec_with_retry(_do)
        for u, e in overlay.items():
            if e is not None:
                self._w_cache[u] = e
        m = self._mirror
        m["src"], m["valsrc"], m["dest"] = src_c, val_c, dest_c
        m["occ"] += occ
        m["rel"] += rc
        m["nonhttp"] |= nonhttp_new
        if rc != len(rels):
            # INSERT OR IGNORE skipped rows: per-type deltas are no longer provably exact.
            self._mirror_dirty = True
        return stats

    def _reconcile_mirror_sync(self) -> Dict[str, Any]:
        """Checkpoint reconciliation: authoritative full aggregation vs. the incremental mirror."""
        mirror = self._mirror_stats()
        db_stats = self.sync_job_counters(status_override=self._job_status)
        diffs = {k: (mirror[k], db_stats[k]) for k in self._MIRROR_KEYS if mirror[k] != db_stats[k]}
        self.metrics["reconciliations"] += 1
        if diffs or self._mirror_dirty:
            if diffs:
                self.metrics["reconciliation_mismatches"] += 1
                print(f"[RECONCILE] mirror != DB, adopting DB values: {diffs}", flush=True)
            self._mirror_from_stats(db_stats)
            self._w_cache = {
                r[0]: [r[1], r[2]] for r in self.conn.execute(
                    "SELECT normalized_url, validation_state, referenced FROM on_page_validation_cache WHERE job_id = ?",
                    (self.job_id,),
                ).fetchall()
            }
        return {"diffs": diffs, "db": db_stats}

    # =====================================================================================
    # Destination registry: O(1) fast path, single-flight validation, source seeding
    # =====================================================================================
    @staticmethod
    def _is_seedable_source_result(res: Dict[str, Any]) -> bool:
        """
        A source-page fetch may satisfy destination validation only when it produced an
        authoritative origin HTTP outcome. WAF blocks/challenges, rate limits, timeouts,
        DNS/network/SSL errors, robots and SSRF outcomes are NOT seeded — the destination
        gets its own validation under the normal policy.
        """
        return (
            res.get("terminal_state") == "VALIDATED"
            and res.get("http_status") is not None
            and not res.get("is_primary_waf")
            and res.get("status_bucket") != "WAF blocked"
            and res.get("waf_status") in ("NONE", "ORIGIN_FORBIDDEN", "", None)
        )

    def _dest_record(self, url: str, scope: str, res: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "normalized_url": url,
            "url_scope": scope,
            "validation_state": res["terminal_state"],
            "initial_http_status": res["initial_http_status"],
            "http_status": res["http_status"],
            "final_url": res["final_url"],
            "redirect_count": res["redirect_count"],
            "redirect_chain": json.dumps(res["redirect_chain"]),
            "response_time_ms": res["response_time_ms"],
            "content_type": res["content_type"],
            "status_bucket": res["status_bucket"],
            "status_classification": res["status_classification"],
            "remark": res["remark"],
            "error_type": res["error_type"],
            "error_message": res["error_message"],
            "robots_status": res["robots_status"],
            "waf_status": res["waf_status"],
            "checked_at": utc_now_iso(),
        }

    def _note_primary_result(self, res: Dict[str, Any]):
        """Non-WAF primary response: reset the WAF streak (existing semantics); only healthy
        responses (<500, not 429, no transport error) count toward the gradual RPS increase."""
        st = res.get("http_status")
        if st is not None and st < 500 and st != 429 and not res.get("error_type", "").startswith(("Timeout", "DNS", "Connection", "Network", "SSL")):
            self._note_primary_ok()
        else:
            self.consecutive_waf = 0

    def _dest_done(self):
        self._dest_outstanding -= 1
        if self._dest_outstanding <= 0:
            self._dest_outstanding = 0
            if getattr(self, "_dest_idle", None) is not None:
                self._dest_idle.set()

    async def _complete_destination(self, url: str, rec: Dict[str, Any]):
        self._dest_state[url] = rec["validation_state"]
        fut = self._inflight.pop(url, None)
        if fut is not None and not fut.done():
            fut.set_result(rec)
        if getattr(self, "_write_q", None) is not None:
            await self._write_q.put(("dest", rec))

    async def _schedule_destination(self, url: str, scope: str):
        """Called for each newly referenced HTTP destination (NEW_CHECK)."""
        if url in self._dest_state or url in self._inflight:
            return  # already validated, or exactly one validation is already queued/in flight
        if url in self._pending_source_norms:
            self._awaiting_source[url] = scope  # its own source fetch will satisfy it
            return
        self._inflight[url] = asyncio.get_running_loop().create_future()
        self._dest_outstanding += 1
        self._dest_idle.clear()
        q = self._dest_q
        await q.put({"normalized_url": url, "url_scope": scope, "waf_requeues": 0})
        self.metrics["dest_q_peak"] = max(self.metrics["dest_q_peak"], q.qsize())

    async def validate_destination(self, client: httpx.AsyncClient, url: str, scope: str = "INTERNAL") -> Dict[str, Any]:
        """
        Single-flight destination validation (need_body=False). Concurrent callers for the same
        normalized URL share ONE HTTP validation and receive the same result object; callers after
        completion get the cached terminal state without any request. Shares the registry with the
        pipeline's destination workers.
        """
        if url in self._dest_state:
            return {"normalized_url": url, "validation_state": self._dest_state[url], "reused": True}
        fut = self._inflight.get(url)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[url] = fut
        try:
            res = await self.fetch_url_manual_redirects(client, url, need_body=False)
            self.metrics["dest_validations"] += 1
            if self._is_primary_host(url):
                if res["is_primary_waf"]:
                    if self._note_primary_waf():
                        await self._trigger_primary_cooldown(client)
                else:
                    self._note_primary_result(res)
            rec = self._dest_record(url, scope, res)
            await self._complete_destination(url, rec)
            return rec
        except BaseException as e:
            self._inflight.pop(url, None)
            if not fut.done():
                fut.set_exception(e)
                fut.exception()  # mark retrieved
            raise

    def request_stop(self):
        """Graceful interruption: stop feeding sources, finish in-flight work, flush, return.
        Queued-but-unstarted destinations stay PENDING in the DB and are rescheduled on resume."""
        self._stop_requested = True

    def _parse_and_classify(self, src_url: str, final_url: str, html: str):
        """ONE Parser pass per page: links, canonical, hreflang, meta tags, data-pageType, title."""
        raw_occ, rels, parser = extract_on_page_urls(src_url, final_url, html)
        t1, t2, _ = self.classifier.extract_meta_tags(html[:32768], parser=None)
        if t1 and t2:
            return raw_occ, rels, t2, t1, "META_TAG", t1, t2
        clf = self.classifier.classify_from_html(src_url, final_url, html, parser=parser)
        return (raw_occ, rels, clf.business or "NEEDS_REVIEW", clf.page_type or "NEEDS_REVIEW",
                clf.method, clf.tag_1, clf.tag_2)

    def performance_summary(self) -> Dict[str, Any]:
        m = self.metrics
        elapsed = max(1e-6, time.monotonic() - m["started_monotonic"])
        lat = sorted(m["latencies_ms"])
        fl = sorted(m["db_flush_ms"])
        cpu = m["cpu_samples"]
        st = self._mirror_stats() if self._mirror else {}
        return {
            "elapsed_sec": round(elapsed, 1),
            "source_fetches": m["source_fetches"],
            "source_pages_per_sec": round(self._run_sources_done / elapsed, 2) if hasattr(self, "_run_sources_done") else None,
            "dest_validations_http": m["dest_validations"],
            "dest_validations_per_sec": round(m["dest_validations"] / elapsed, 2),
            "source_seeded_validations": m["source_seeded_validations"],
            "dest_skipped_already_validated": m["dest_validations_skipped_cached"],
            "relationships_persisted_this_run": (st.get("url_relationships_persisted", 0) - getattr(self, "_run_rel_base", 0)) if st else 0,
            "http_requests_total": m["http_requests"],
            "primary_requests_paced": m["primary_requests"],
            "robots_fetches": self.robots.fetch_count,
            "achieved_primary_rps": round(m["primary_requests"] / elapsed, 2),
            "target_rps_final": self.target_rps,
            "akamai_cooldowns": self.cooldown_count,
            "waf_events": m["waf_events"],
            "status_403": m["status_403"],
            "status_429": m["status_429"],
            "timeouts": m["timeouts"],
            "network_errors": m["network_errors"],
            "latency_avg_ms": round(sum(lat) / len(lat), 1) if lat else 0.0,
            "latency_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1) if lat else 0.0,
            "db_flushes": m["db_flushes"],
            "db_rows_written": m["db_rows_written"],
            "db_writes_per_sec": round(m["db_rows_written"] / elapsed, 1),
            "db_flush_avg_ms": round(sum(fl) / len(fl), 2) if fl else 0.0,
            "db_flush_p95_ms": round(fl[int(len(fl) * 0.95) - 1], 2) if fl else 0.0,
            "write_q_peak": m["write_q_peak"],
            "dest_q_peak": m["dest_q_peak"],
            "parse_q_peak": m["parse_q_peak"],
            "rss_peak_mb": m["rss_peak_mb"],
            "cpu_avg_pct": round(sum(cpu) / len(cpu), 1) if cpu else 0.0,
            "cpu_peak_pct": round(max(cpu), 1) if cpu else 0.0,
            "reconciliations": m["reconciliations"],
            "reconciliation_mismatches": m["reconciliation_mismatches"],
            "rps_history": m["rps_history"][-50:],
        }

    def _write_progress_json(self, stats: Dict[str, Any]):
        if not self.progress_json_path:
            return
        tmp = self.progress_json_path + ".tmp"
        d = os.path.dirname(self.progress_json_path)
        if d:
            os.makedirs(d, exist_ok=True)
        out = dict(stats)
        out["rss_mb"] = round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
        out["target_rps"] = self.target_rps
        out["akamai_cooldowns_triggered"] = self.cooldown_count
        out["performance"] = self.performance_summary()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
        os.replace(tmp, self.progress_json_path)

    async def _writer_loop(self, q: asyncio.Queue):
        """The only SQLite writer. Bounded input queue; flushes on size or time; O(1) progress row;
        full aggregation only at reconcile checkpoints."""
        src_u: List[Dict[str, Any]] = []
        rels: List[Dict[str, Any]] = []
        seeds: List[Dict[str, Any]] = []
        incs: Counter = Counter()
        sseeds: List[Dict[str, Any]] = []
        dests: List[Dict[str, Any]] = []
        force = False
        last_flush = time.monotonic()
        last_reconcile = time.monotonic()
        last_progress = 0.0
        last_log = time.monotonic()
        proc = psutil.Process()
        proc.cpu_percent(None)
        done = False
        m = self.metrics
        while not done:
            timeout = max(0.05, self.flush_interval_sec - (time.monotonic() - last_flush))
            try:
                pkt = await asyncio.wait_for(q.get(), timeout)
            except asyncio.TimeoutError:
                pkt = ("tick",)
            if pkt is None:
                done = True
            else:
                kind = pkt[0]
                if kind == "source":
                    _, su, rr, sd, ic = pkt
                    src_u.append(su); rels.extend(rr); seeds.extend(sd); incs.update(ic)
                elif kind == "seed":
                    sseeds.append(pkt[1])
                elif kind == "dest":
                    dests.append(pkt[1])
                elif kind == "status":
                    force = True
            m["write_q_peak"] = max(m["write_q_peak"], q.qsize())
            now = time.monotonic()
            pending = len(src_u) + len(sseeds) + len(dests)
            due = (
                done or force
                or len(src_u) >= self.batch_size
                or (len(sseeds) + len(dests)) >= self.batch_size * 4
                or (pending and now - last_flush >= self.flush_interval_sec)
            )
            if due and (pending or force or done):
                t0 = time.perf_counter()
                stats = await asyncio.to_thread(
                    self._flush_pipeline_sync, src_u, rels, seeds, incs, sseeds, dests, self._job_status
                )
                self._reservoir_add(m["db_flush_ms"], (time.perf_counter() - t0) * 1000.0)
                m["db_flushes"] += 1
                m["db_rows_written"] += len(src_u) * 2 + len(rels) + len(seeds) + len(sseeds) + len(dests) * 2
                src_u, rels, seeds, sseeds, dests = [], [], [], [], []
                incs = Counter()
                force = False
                last_flush = time.monotonic()
                if now - last_progress >= self.progress_interval_sec or done:
                    rss = proc.memory_info().rss / (1024 * 1024)
                    m["rss_peak_mb"] = round(max(m["rss_peak_mb"], rss), 1)
                    m["cpu_samples"].append(proc.cpu_percent(None))
                    await asyncio.to_thread(self._write_progress_json, stats)
                    last_progress = now
                if now - last_log >= 10.0:
                    el = max(0.01, now - m["started_monotonic"])
                    dd = sum(
                        stats[k] for k in ("unique_http_destinations_validated", "unique_http_destinations_failed",
                                           "unique_http_destinations_blocked", "unique_http_destinations_timed_out",
                                           "unique_http_destinations_excluded"))
                    print(
                        f"[PIPELINE] Sources {stats['source_pages_processed']:,}/{stats['source_pages_total']:,} "
                        f"({self._run_sources_done/el:.1f} pg/s) | Rels {stats['url_relationships_persisted']:,} | "
                        f"Dest {dd:,}/{stats['unique_http_destinations']:,} (http {m['dest_validations']:,}) | "
                        f"rps {m['primary_requests']/el:.2f}/{self.target_rps} | cooldowns {self.cooldown_count}",
                        flush=True,
                    )
                    last_log = now
            buffered = len(src_u) + len(sseeds) + len(dests)
            if not done and self.reconcile_interval_sec and now - last_reconcile >= self.reconcile_interval_sec and not buffered:
                await asyncio.to_thread(self._reconcile_mirror_sync)
                last_reconcile = time.monotonic()

    def _load_run_state(self) -> List[Tuple[str, str]]:
        """Resume-aware load: one authoritative reconciliation + in-memory registry from the DB cache."""
        stats = self.sync_job_counters(status_override="RUNNING")
        self._job_status = "RUNNING"
        self._mirror_from_stats(stats)
        self._rels_discovered = stats["url_relationships_persisted"]
        self._run_rel_base = stats["url_relationships_persisted"]
        self._w_cache = {}
        self._dest_state = {}
        self._seen_http_dests = set()
        pending_refs: List[Tuple[str, str]] = []
        for u, scope, st, ref in self.conn.execute(
            "SELECT normalized_url, url_scope, validation_state, referenced FROM on_page_validation_cache WHERE job_id = ?",
            (self.job_id,),
        ):
            self._w_cache[u] = [st, ref]
            if st != "PENDING":
                self._dest_state[u] = st
            if ref:
                self._seen_http_dests.add(u)
                if st == "PENDING":
                    pending_refs.append((u, scope))
        self._pending_source_norms = {
            r[0] for r in self.conn.execute(
                "SELECT normalized_source_url FROM on_page_source_pages WHERE job_id = ? AND crawl_state = 'PENDING'",
                (self.job_id,),
            )
        }
        self._inflight = {}
        self._awaiting_source = {}
        self._dest_outstanding = 0
        return pending_refs

    async def run(self, max_source_pages_this_run: Optional[int] = None) -> Dict[str, Any]:
        """
        Bounded producer/consumer pipeline (replaces serial Stage A -> Stage B):

          source producer -> [fetch_q] -> source fetch workers (GET + HTML, shared primary budget)
            -> [parse_q] -> parse workers (one Parser pass in a bounded thread pool, HTML dropped)
            -> relationship labeling (NEW_CHECK/REUSED) + registry fast path
            -> [dest_q] -> destination workers (need_body=False, same shared primary budget)
          all results -> [write_q] -> single batched SQLite writer (+ O(1) progress row)

        Destination validation starts as soon as a destination is discovered. A destination that
        is itself a pending sitemap source is NOT validated separately: its source fetch seeds the
        validation cache (only for authoritative non-WAF HTTP outcomes).
        If max_source_pages_this_run is set (checkpoint/interrupt tests), stops after that many
        source pages; destinations discovered so far are validated before returning.
        """
        if self.conn is None:
            self.conn = self._connect_db()
        self.metrics = self._new_metrics()
        self._stop_requested = False
        self._run_sources_done = 0
        pending_refs = self._load_run_state()

        pending_sql = """
            SELECT source_page_url, normalized_source_url, source_page_id, language
            FROM on_page_source_pages
            WHERE job_id = ? AND crawl_state = 'PENDING'
            ORDER BY source_page_id ASC
        """
        if max_source_pages_this_run is not None:
            pending_sql += f" LIMIT {int(max_source_pages_this_run)}"
        pending_sources = [
            {"source_page_url": r[0], "normalized_source_url": r[1], "source_page_id": r[2], "language": r[3], "waf_requeues": 0}
            for r in self.conn.execute(pending_sql, (self.job_id,)).fetchall()
        ]
        print(f"[PIPELINE] Pending source pages: {len(pending_sources):,} | resumed PENDING destinations: {len(pending_refs):,}", flush=True)

        max_conn = max(self.source_concurrency, self.dest_concurrency) * 2
        limits = httpx.Limits(
            max_connections=max_conn,
            # keep every connection we are allowed to open alive: churning primary keep-alive
            # connections costs a TLS handshake per request against the edge
            max_keepalive_connections=max_conn,
            keepalive_expiry=self.keepalive_expiry,
        )
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=settings.WRITE_TIMEOUT,
            pool=settings.POOL_TIMEOUT,
        )
        loop = asyncio.get_running_loop()
        parse_pool = concurrent.futures.ThreadPoolExecutor(self.parse_workers, thread_name_prefix="onpage-parse")

        async with httpx.AsyncClient(
            verify=False,
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            http2=self.http2,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Encoding": "gzip, deflate",
            },
        ) as client:
            await self._wait_for_primary_ready(client, initial_wait=False)

            fetch_q: asyncio.Queue = asyncio.Queue(maxsize=self.source_concurrency * 2)
            parse_q: asyncio.Queue = asyncio.Queue(maxsize=max(4, self.parse_workers * 4))
            self._dest_q = asyncio.Queue(maxsize=5000)
            self._write_q = asyncio.Queue(maxsize=2000)
            self._dest_idle = asyncio.Event()
            self._dest_idle.set()
            src_idle = asyncio.Event()
            src_outstanding = 0
            requeue_sources: List[Dict[str, Any]] = []
            write_q = self._write_q
            dest_q = self._dest_q

            def finish_source_item():
                nonlocal src_outstanding
                src_outstanding -= 1
                if src_outstanding <= 0:
                    src_idle.set()

            async def source_producer():
                nonlocal src_outstanding, requeue_sources
                items = pending_sources
                while items and not self._stop_requested:
                    requeue_sources = []
                    src_idle.clear()
                    src_outstanding += 1  # guard so the pass can't look idle while feeding
                    for it in items:
                        if self._stop_requested:
                            break
                        src_outstanding += 1
                        await fetch_q.put(it)
                    finish_source_item()
                    await src_idle.wait()
                    items = requeue_sources
                for _ in range(self.source_concurrency):
                    await fetch_q.put(None)

            async def on_source_fetched(item: Dict[str, Any], res: Dict[str, Any]):
                norm = item["normalized_source_url"]
                self._pending_source_norms.discard(norm)
                if self._is_seedable_source_result(res) and norm not in self._dest_state:
                    now_iso = utc_now_iso()
                    rec = {
                        "job_id": self.job_id, "normalized_url": norm, "validation_state": res["terminal_state"],
                        "initial_http_status": res["initial_http_status"], "http_status": res["http_status"],
                        "final_url": res["final_url"], "redirect_count": res["redirect_count"],
                        "redirect_chain": json.dumps(res["redirect_chain"]), "response_time_ms": res["response_time_ms"],
                        "content_type": res["content_type"], "status_bucket": res["status_bucket"],
                        "status_classification": res["status_classification"], "remark": res["remark"],
                        "error_type": res["error_type"], "error_message": res["error_message"],
                        "robots_status": res["robots_status"], "waf_status": res["waf_status"],
                        "first_seen_source_url": item["source_page_url"], "checked_at": now_iso,
                    }
                    self._dest_state[norm] = res["terminal_state"]
                    self.metrics["source_seeded_validations"] += 1
                    self._awaiting_source.pop(norm, None)
                    fut = self._inflight.get(norm)
                    if fut is not None and not fut.done():
                        fut.set_result(rec)  # a queued duplicate (resume edge case) will be skipped
                    await write_q.put(("seed", rec))
                elif norm in self._awaiting_source:
                    # Not an authoritative outcome (WAF/timeout/network...): validate it as a destination.
                    scope = self._awaiting_source.pop(norm)
                    await self._schedule_destination(norm, scope)

            async def fetch_worker():
                while True:
                    item = await fetch_q.get()
                    if item is None:
                        break
                    handed_off = False
                    try:
                        norm_src = item["normalized_source_url"]
                        res = await self.fetch_url_manual_redirects(client, norm_src, need_body=True)
                        self.metrics["source_fetches"] += 1
                        if res["is_primary_waf"]:
                            trigger = self._note_primary_waf()
                            if item["waf_requeues"] < 2:
                                item["waf_requeues"] += 1
                                requeue_sources.append(item)
                                if trigger:
                                    await self._trigger_primary_cooldown(client)
                                continue
                            elif trigger:
                                await self._trigger_primary_cooldown(client)
                        elif self._is_primary_host(norm_src):
                            self._note_primary_result(res)
                        await on_source_fetched(item, res)
                        await parse_q.put((item, res))
                        self.metrics["parse_q_peak"] = max(self.metrics["parse_q_peak"], parse_q.qsize())
                        handed_off = True
                    except Exception as e:
                        print(f"[PIPELINE] source fetch error for {item.get('source_page_url')}: {e!r} (left PENDING)", flush=True)
                    finally:
                        if not handed_off:
                            finish_source_item()

            async def parse_worker():
                while True:
                    pkt = await parse_q.get()
                    if pkt is None:
                        break
                    item, fetch_res = pkt
                    try:
                        src_url = item["source_page_url"]
                        html = fetch_res.get("html") or ""
                        fetch_res["html"] = ""  # do not retain HTML beyond extraction+classification
                        final_src_url = fetch_res["final_url"]
                        src_http = fetch_res["http_status"]
                        now_iso = utc_now_iso()
                        raw_occ = 0
                        extracted_rels: List[Dict[str, Any]] = []
                        if html:
                            raw_occ, extracted_rels, biz, pt, clf_method, tag_1, tag_2 = await loop.run_in_executor(
                                parse_pool, self._parse_and_classify, src_url, final_src_url, html
                            )
                        else:
                            clf_res = self.classifier.classify_from_html(src_url, final_src_url, "")
                            biz = clf_res.business or "NEEDS_REVIEW"
                            pt = clf_res.page_type or "NEEDS_REVIEW"
                            clf_method = clf_res.method
                            tag_1, tag_2 = clf_res.tag_1, clf_res.tag_2
                        del html

                        term = fetch_res["terminal_state"]
                        crawl_state = "COMPLETED" if term == "VALIDATED" else term

                        # NEW_CHECK vs REUSED per unique HTTP destination (no await inside: atomic)
                        rel_rows = []
                        cache_seeds = []
                        new_dests: List[Tuple[str, str]] = []
                        reuse_incs: Counter = Counter()
                        for er in extracted_rels:
                            norm_found = er["normalized_found_url"]
                            scope = er["found_url_scope"]
                            if er["is_http"]:
                                if norm_found not in self._seen_http_dests:
                                    self._seen_http_dests.add(norm_found)
                                    val_src = "NEW_CHECK"
                                    rel_remark = ""
                                    cache_seeds.append({
                                        "job_id": self.job_id,
                                        "normalized_url": norm_found,
                                        "url_scope": scope,
                                        "first_seen_source_url": src_url,
                                    })
                                    new_dests.append((norm_found, scope))
                                else:
                                    val_src = "REUSED"
                                    rel_remark = "Already validated; reused existing result"
                                    reuse_incs[norm_found] += 1
                            elif scope == "FRAGMENT":
                                val_src = "FRAGMENT_SKIPPED"
                                rel_remark = "Fragment-only URL — excluded from HTTP validation"
                            else:
                                val_src = "NON_HTTP_SKIPPED"
                                sch = er.get("non_http_scheme") or er["url_syntax_type"].lower()
                                rel_remark = f"Non-HTTP scheme ({sch}) — excluded from HTTP validation"

                            rel_rows.append({
                                "job_id": self.job_id,
                                "source_page_url": src_url,
                                "source_page_status": src_http,
                                "source_page_final_url": final_src_url,
                                "found_url": er["found_url"],
                                "normalized_found_url": norm_found,
                                "found_url_type": er["found_url_type"],
                                "url_syntax_type": er["url_syntax_type"],
                                "found_url_scope": scope,
                                "anchor_text": er["anchor_text"],
                                "anchor_texts": json.dumps(er["anchor_texts"]),
                                "link_types": json.dumps(er["link_types"]),
                                "rel": er["rel"],
                                "hreflang": er["hreflang"],
                                "occurrence_count": er["occurrence_count"],
                                "representative_location": er["representative_location"],
                                "business": biz,
                                "language": item["language"],
                                "page_type": pt,
                                "validation_source": val_src,
                                "remark": rel_remark,
                                "extracted_at": now_iso,
                            })

                        src_update = {
                            "job_id": self.job_id,
                            "source_page_url": src_url,
                            "crawl_state": crawl_state,
                            "initial_http_status": fetch_res["initial_http_status"],
                            "http_status": src_http,
                            "final_url": final_src_url,
                            "redirect_count": fetch_res["redirect_count"],
                            "redirect_chain": json.dumps(fetch_res["redirect_chain"]),
                            "content_type": fetch_res["content_type"],
                            "response_time_ms": fetch_res["response_time_ms"],
                            "error_type": fetch_res["error_type"],
                            "error_message": fetch_res["error_message"],
                            "robots_status": fetch_res["robots_status"],
                            "waf_status": fetch_res["waf_status"],
                            "business": biz,
                            "language": item["language"],
                            "page_type": pt,
                            "classification_method": clf_method,
                            "tag_1": tag_1,
                            "tag_2": tag_2,
                            "raw_occurrences_count": raw_occ,
                            "relationships_count": len(rel_rows),
                            "remark": fetch_res["remark"],
                            "checked_at": now_iso,
                        }
                        self._rels_discovered += len(rel_rows)
                        # Seeds are enqueued to the writer BEFORE any destination result can exist.
                        await write_q.put(("source", src_update, rel_rows, cache_seeds, reuse_incs))
                        self._run_sources_done += 1
                        for u, sc in new_dests:
                            await self._schedule_destination(u, sc)
                    except Exception as e:
                        print(f"[PIPELINE] parse/persist error for {item.get('source_page_url')}: {e!r} (left PENDING)", flush=True)
                    finally:
                        finish_source_item()

            async def dest_worker():
                while True:
                    item = await dest_q.get()
                    if item is None:
                        break
                    url = item["normalized_url"]
                    try:
                        if url in self._dest_state or self._stop_requested:
                            # satisfied meanwhile (e.g. source-seeded) or graceful stop: no request
                            fut = self._inflight.pop(url, None)
                            if fut is not None and not fut.done():
                                if url in self._dest_state:
                                    fut.set_result({"normalized_url": url, "validation_state": self._dest_state[url], "reused": True})
                                else:
                                    fut.cancel()
                            if url in self._dest_state:
                                self.metrics["dest_validations_skipped_cached"] += 1
                            self._dest_done()
                            continue
                        res = await self.fetch_url_manual_redirects(client, url, need_body=False)
                        self.metrics["dest_validations"] += 1
                        if res["is_primary_waf"]:
                            trigger = self._note_primary_waf()
                            if item["waf_requeues"] < 2:
                                item["waf_requeues"] += 1
                                if trigger:
                                    await self._trigger_primary_cooldown(client)
                                t = asyncio.create_task(dest_q.put(item))
                                self._bg_tasks.add(t)
                                t.add_done_callback(self._bg_tasks.discard)
                                continue
                            elif trigger:
                                await self._trigger_primary_cooldown(client)
                        elif self._is_primary_host(url):
                            self._note_primary_result(res)
                        await self._complete_destination(url, self._dest_record(url, item["url_scope"], res))
                        self._dest_done()
                    except Exception as e:
                        print(f"[PIPELINE] destination error for {url}: {e!r} (left PENDING)", flush=True)
                        fut = self._inflight.pop(url, None)
                        if fut is not None and not fut.done():
                            fut.cancel()
                        self._dest_done()

            writer_t = asyncio.create_task(self._writer_loop(write_q))
            run_task = asyncio.current_task()

            def _writer_died(t: asyncio.Task):
                # The writer is the only consumer of the bounded write queue: if it fails, every
                # producer would block forever. Abort the run instead (resume is safe: anything
                # not committed is still PENDING in SQLite).
                if not t.cancelled() and t.exception() is not None and run_task is not None:
                    run_task.cancel()

            writer_t.add_done_callback(_writer_died)
            dest_tasks = [asyncio.create_task(dest_worker()) for _ in range(self.dest_concurrency)]
            fetch_tasks: List[asyncio.Task] = []
            parse_tasks: List[asyncio.Task] = []
            try:
                # Resume: re-register referenced destinations that were still PENDING
                for u, sc in pending_refs:
                    await self._schedule_destination(u, sc)

                fetch_tasks = [asyncio.create_task(fetch_worker()) for _ in range(self.source_concurrency)]
                parse_tasks = [asyncio.create_task(parse_worker()) for _ in range(self.parse_workers)]
                await source_producer()
                await asyncio.gather(*fetch_tasks)
                for _ in parse_tasks:
                    await parse_q.put(None)
                await asyncio.gather(*parse_tasks)

                limited = max_source_pages_this_run is not None or self._stop_requested
                if not limited and self._awaiting_source:
                    # Safety net: a referenced source URL that was never fetched in this run.
                    for u, sc in list(self._awaiting_source.items()):
                        self._awaiting_source.pop(u, None)
                        self._pending_source_norms.discard(u)
                        await self._schedule_destination(u, sc)

                if self._dest_outstanding:
                    self._job_status = "VALIDATING"
                    await write_q.put(("status",))
                await self._dest_idle.wait()
                for _ in dest_tasks:
                    await dest_q.put(None)
                await asyncio.gather(*dest_tasks)
                if self._bg_tasks:
                    await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
            except asyncio.CancelledError:
                if writer_t.done() and not writer_t.cancelled() and writer_t.exception() is not None:
                    raise RuntimeError("on-page audit DB writer failed; run aborted (resumable)") from writer_t.exception()
                raise
            finally:
                for t in fetch_tasks + parse_tasks + dest_tasks + list(self._bg_tasks):
                    if not t.done():
                        t.cancel()
                if not writer_t.done():
                    try:
                        await write_q.put(None)
                        await writer_t
                    except asyncio.CancelledError:
                        writer_t.cancel()
                        raise
                parse_pool.shutdown(wait=False)
                self._write_q = None

        perf = self.performance_summary()
        if max_source_pages_this_run is not None or self._stop_requested:
            stats = self.sync_job_counters(status_override="RUNNING")
            stats["performance"] = perf
            return stats

        # Final full reconciliation: authoritative aggregation vs incremental mirror
        mirror = self._mirror_stats()
        final_stats = self.sync_job_counters()
        diffs = {k: (mirror[k], final_stats[k]) for k in self._MIRROR_KEYS if mirror[k] != final_stats[k]}
        all_sources_done = final_stats["source_pages_pending"] == 0
        dest_done = (
            final_stats["unique_http_destinations_validated"]
            + final_stats["unique_http_destinations_failed"]
            + final_stats["unique_http_destinations_blocked"]
            + final_stats["unique_http_destinations_timed_out"]
            + final_stats["unique_http_destinations_excluded"]
        )
        all_dests_done = dest_done == final_stats["unique_http_destinations"]
        terminal_job_status = "COMPLETED" if (all_sources_done and all_dests_done) else "PARTIAL"
        self._job_status = terminal_job_status
        # Counters were just written authoritatively; only the status changes (no 2nd full aggregation).
        now = utc_now_iso()
        with self.conn:
            self.conn.execute("UPDATE on_page_audit_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                              (terminal_job_status, now, self.job_id))
            self.conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (terminal_job_status, now, self.job_id))
        final_stats["status"] = terminal_job_status
        final_stats["updated_at"] = now
        final_stats["counter_reconciliation"] = {"passed": not diffs, "diffs": diffs}
        final_stats["performance"] = perf
        self._write_progress_json(final_stats)
        return final_stats

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
