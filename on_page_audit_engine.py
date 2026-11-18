import asyncio
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

    def __init__(self, allow_local: bool = False):
        self.allow_local = allow_local
        self._cache: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def can_fetch(self, client: httpx.AsyncClient, url: str) -> bool:
        if not getattr(settings, "RESPECT_ROBOTS", True):
            return True
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin not in self._cache:
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
        batch_size: int = 50,
        progress_json_path: Optional[str] = None,
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

        self.classifier = Classifier()
        self.robots = RobotsCache(allow_local=allow_local)
        self.conn: Optional[sqlite3.Connection] = None

        # Primary-domain polite pacemaker & Akamai cooldown gate
        self._pace_lock = asyncio.Lock()
        self._next_slot_ts = 0.0
        self._cooldown_lock = asyncio.Lock()
        self._cooldown_gate = asyncio.Event()
        self._cooldown_gate.set()
        self.consecutive_waf = 0
        self.cooldown_count = 0
        self._window_ok = 0

        # Per-host semaphores for external & secondary hosts
        self._host_sems: Dict[str, asyncio.Semaphore] = {}
        self._host_sem_lock = asyncio.Lock()

        # In-memory job-scoped validation cache tracking (loaded from DB on resume)
        self._seen_http_dests: Set[str] = set()
        self._seen_lock = asyncio.Lock()

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

    def _is_primary_host(self, url: str) -> bool:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
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
        await self._cooldown_gate.wait()
        async with self._pace_lock:
            now = time.monotonic()
            interval = 1.0 / max(0.5, self.target_rps)
            if self._next_slot_ts > now:
                wait_s = self._next_slot_ts - now
                self._next_slot_ts += interval
            else:
                wait_s = 0.0
                self._next_slot_ts = now + interval
        if wait_s > 0:
            await asyncio.sleep(wait_s)
        await self._cooldown_gate.wait()

    async def _wait_for_primary_ready(self, client: httpx.AsyncClient, initial_wait: bool = False):
        if not self.primary_domain or self.primary_domain in ("127.0.0.1", "localhost"):
            return
        if initial_wait and self.akamai_cooldown_sec > 0:
            print(f"[COOLDOWN] Waiting {self.akamai_cooldown_sec:.0f}s for Akamai edge window reset...", flush=True)
            await asyncio.sleep(self.akamai_cooldown_sec)
        probe_url = f"https://www.{self.primary_domain}/robots.txt"
        for attempt in range(1, 12):
            try:
                r = await client.get(probe_url, timeout=10.0)
                if r.status_code == 200:
                    print(f"[READY] Primary edge open ({probe_url} -> 200 on probe #{attempt})", flush=True)
                    return
                print(f"[COOLDOWN] Probe #{attempt}: HTTP {r.status_code}; waiting 45s...", flush=True)
            except Exception as e:
                print(f"[COOLDOWN] Probe #{attempt} error: {e}; waiting 45s...", flush=True)
            await asyncio.sleep(45.0)

    async def _trigger_primary_cooldown(self, client: httpx.AsyncClient):
        if not self._cooldown_gate.is_set():
            await self._cooldown_gate.wait()
            return
        async with self._cooldown_lock:
            if not self._cooldown_gate.is_set():
                return
            self._cooldown_gate.clear()
            self.cooldown_count += 1
            old_rps = self.target_rps
            self.target_rps = max(self.min_rps, round(self.target_rps * 0.85, 2))
            self.consecutive_waf = 0
            self._window_ok = 0
            print(
                f"\n[AKAMAI COOLDOWN #{self.cooldown_count}] Stepping target_rps {old_rps} -> {self.target_rps} req/s...",
                flush=True,
            )
            try:
                await self._wait_for_primary_ready(client, initial_wait=True)
            finally:
                self._next_slot_ts = time.monotonic()
                self._cooldown_gate.set()

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
        - Polite Akamai pacing on primary domain
        - Manual hop-by-hop redirects with loop detection
        - Bounded retries with backoff on transient 429/502/503/504/network errors
        - Diagnostics403 WAF classification
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
            if is_primary:
                await self._acquire_primary_pace()

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
                async with host_sem:
                    for _hop in range(max_hops + 1):
                        if not URLNormalizer.is_safe_url(current_url, allow_local=self.allow_local):
                            ssrf_safe = False
                            break

                        resp = await client.get(current_url)
                        st = resp.status_code

                        if st in (301, 302, 303, 307, 308):
                            location = resp.headers.get("Location") or resp.headers.get("location")
                            redirect_chain.append({"url": current_url, "status": st})
                            if not location:
                                final_status = st
                                final_url = current_url
                                content_type = resp.headers.get("content-type", "")
                                resp_headers = dict(resp.headers)
                                break
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
                                raw_bytes = resp.content[: settings.MAX_PAYLOAD_SIZE_BYTES]
                            break

                initial_status = redirect_chain[0]["status"] if redirect_chain else final_status

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
                retries += 1
                if retries <= self.max_retries:
                    await asyncio.sleep(1.0 * retries)
            except Exception as e:
                err_l = str(e).lower()
                if "timeout" in err_l or "timed out" in err_l:
                    error_type = "Timeout"
                elif "dns" in err_l or "getaddrinfo" in err_l or "name_not_resolved" in err_l or "resolve" in err_l:
                    error_type = "DNS Error"
                elif "ssl" in err_l or "cert" in err_l or "tls" in err_l:
                    error_type = "SSL Error"
                elif "connect" in err_l:
                    error_type = "Connection Error"
                else:
                    error_type = "Network Error"
                error_message = str(e)[:200]
                retries += 1
                if retries <= self.max_retries:
                    await asyncio.sleep(1.0 * retries)

        elapsed_ms = round((time.time() - t0) * 1000.0, 2)
        html_text = ""
        if raw_bytes and ("html" in content_type.lower() or not content_type or final_status in (403, 429)):
            html_text = raw_bytes.decode("utf-8", errors="ignore")

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
                    self.conn.executemany(
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
                    )

                if cache_reuse_increments:
                    self.conn.executemany(
                        "UPDATE on_page_validation_cache SET reuse_count = reuse_count + ? WHERE job_id = ? AND normalized_url = ?",
                        [(cnt, self.job_id, u) for u, cnt in cache_reuse_increments.items()],
                    )

        self._exec_with_retry(_do)

    def _flush_dest_batch_sync(self, dest_updates: List[Dict[str, Any]]):
        if not dest_updates:
            return

        def _do():
            with self.conn:
                self.conn.executemany(
                    """
                    UPDATE on_page_validation_cache SET
                        validation_state = :validation_state,
                        initial_http_status = :initial_http_status,
                        http_status = :http_status,
                        final_url = :final_url,
                        redirect_count = :redirect_count,
                        redirect_chain = :redirect_chain,
                        response_time_ms = :response_time_ms,
                        content_type = :content_type,
                        status_bucket = :status_bucket,
                        status_classification = :status_classification,
                        remark = :remark,
                        error_type = :error_type,
                        error_message = :error_message,
                        robots_status = :robots_status,
                        waf_status = :waf_status,
                        checked_at = :checked_at
                    WHERE job_id = :job_id AND normalized_url = :normalized_url
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

        self._exec_with_retry(_do)

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

    async def run(self, max_source_pages_this_run: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute Stage A (Source Page Fetch + On-Page URL Extraction) and
        Stage B (Unique HTTP Destination Validation) with checkpoint/resume!
        If max_source_pages_this_run is set (for checkpoint/interrupt tests),
        stops cleanly after that many source pages without running Stage B.
        """
        if self.conn is None:
            self.conn = self._connect_db()

        # Pre-load seen HTTP destinations from existing relationships for exact NEW_CHECK vs REUSED on resume
        existing_claimed = self.conn.execute(
            """
            SELECT DISTINCT normalized_found_url
            FROM on_page_link_relationships
            WHERE job_id = ? AND found_url_scope IN ('INTERNAL', 'EXTERNAL')
            """,
            (self.job_id,),
        ).fetchall()
        self._seen_http_dests = {r[0] for r in existing_claimed}

        self.sync_job_counters(status_override="RUNNING")

        limits = httpx.Limits(
            max_connections=max(self.source_concurrency, self.dest_concurrency) * 2,
            max_keepalive_connections=max(self.source_concurrency, self.dest_concurrency),
            keepalive_expiry=30.0,
        )
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=settings.WRITE_TIMEOUT,
            pool=settings.POOL_TIMEOUT,
        )

        async with httpx.AsyncClient(
            verify=False,
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Encoding": "gzip, deflate",
            },
        ) as client:
            # Ensure primary Akamai edge is ready before starting Stage A
            await self._wait_for_primary_ready(client, initial_wait=False)

            # ===================== STAGE A: SOURCE PAGE FETCH + EXTRACTION =====================
            pending_sql = """
                SELECT source_page_url, normalized_source_url, source_page_id, language
                FROM on_page_source_pages
                WHERE job_id = ? AND crawl_state = 'PENDING'
                ORDER BY source_page_id ASC
            """
            if max_source_pages_this_run is not None:
                pending_sql += f" LIMIT {int(max_source_pages_this_run)}"
            pending_sources = [
                {
                    "source_page_url": r[0],
                    "normalized_source_url": r[1],
                    "source_page_id": r[2],
                    "language": r[3],
                    "waf_requeues": 0,
                }
                for r in self.conn.execute(pending_sql, (self.job_id,)).fetchall()
            ]

            t_stage_a = time.time()
            processed_stage_a = 0
            print(f"[Stage A] Pending source pages: {len(pending_sources):,}", flush=True)

            while pending_sources:
                requeue_sources: List[Dict[str, Any]] = []
                work_q: asyncio.Queue = asyncio.Queue(maxsize=500)
                res_q: asyncio.Queue = asyncio.Queue(maxsize=1000)

                async def src_producer(items):
                    for it in items:
                        await work_q.put(it)
                    for _ in range(self.source_concurrency):
                        await work_q.put(None)

                async def src_worker():
                    while True:
                        item = await work_q.get()
                        if item is None:
                            work_q.task_done()
                            break
                        try:
                            src_url = item["source_page_url"]
                            norm_src = item["normalized_source_url"]
                            fetch_res = await self.fetch_url_manual_redirects(client, norm_src, need_body=True)

                            if fetch_res["is_primary_waf"]:
                                self.consecutive_waf += 1
                                if item["waf_requeues"] < 2:
                                    item["waf_requeues"] += 1
                                    requeue_sources.append(item)
                                    if self.consecutive_waf >= 5:
                                        await self._trigger_primary_cooldown(client)
                                    continue
                                elif self.consecutive_waf >= 5:
                                    await self._trigger_primary_cooldown(client)
                            else:
                                self.consecutive_waf = 0
                                self._window_ok += 1
                                if self._window_ok >= 2000 and self.target_rps < self.max_rps:
                                    self.target_rps = min(self.max_rps, round(self.target_rps + 0.25, 2))
                                    self._window_ok = 0

                            html = fetch_res["html"]
                            final_src_url = fetch_res["final_url"]
                            src_http = fetch_res["http_status"]
                            now_iso = utc_now_iso()

                            # Extract on-page URLs ONLY when HTML was actually obtained
                            raw_occ = 0
                            extracted_rels: List[Dict[str, Any]] = []
                            clf_res = None
                            if html:
                                raw_occ, extracted_rels, parser = await asyncio.to_thread(
                                    extract_on_page_urls, src_url, final_src_url, html
                                )
                                # Fast-path Level 1 meta tags then full Classifier fallback
                                t1, t2, _ = self.classifier.extract_meta_tags(html[:32768], parser=None)
                                if t1 and t2:
                                    biz, pt, clf_method, tag_1, tag_2 = t2, t1, "META_TAG", t1, t2
                                else:
                                    clf_res = await asyncio.to_thread(
                                        self.classifier.classify_from_html, src_url, final_src_url, html
                                    )
                                    biz = clf_res.business or "NEEDS_REVIEW"
                                    pt = clf_res.page_type or "NEEDS_REVIEW"
                                    clf_method = clf_res.method
                                    tag_1, tag_2 = clf_res.tag_1, clf_res.tag_2
                            else:
                                clf_res = self.classifier.classify_from_html(src_url, final_src_url, "")
                                biz = clf_res.business or "NEEDS_REVIEW"
                                pt = clf_res.page_type or "NEEDS_REVIEW"
                                clf_method = clf_res.method
                                tag_1, tag_2 = clf_res.tag_1, clf_res.tag_2

                            # Map fetch terminal state to source crawl_state
                            term = fetch_res["terminal_state"]
                            crawl_state = "COMPLETED" if term == "VALIDATED" else term

                            # Assign NEW_CHECK vs REUSED atomically per unique HTTP destination
                            rel_rows = []
                            cache_seeds = []
                            reuse_incs = Counter()
                            async with self._seen_lock:
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

                            # Store live source fetch into same-job validation cache so cross-links
                            # to this exact sitemap page reuse this fresh live validation without re-hitting origin!
                            self_cache_rec = {
                                "job_id": self.job_id,
                                "normalized_url": norm_src,
                                "validation_state": term,
                                "initial_http_status": fetch_res["initial_http_status"],
                                "http_status": src_http,
                                "final_url": final_src_url,
                                "redirect_count": fetch_res["redirect_count"],
                                "redirect_chain": json.dumps(fetch_res["redirect_chain"]),
                                "response_time_ms": fetch_res["response_time_ms"],
                                "content_type": fetch_res["content_type"],
                                "status_bucket": fetch_res["status_bucket"],
                                "status_classification": fetch_res["status_classification"],
                                "remark": fetch_res["remark"],
                                "error_type": fetch_res["error_type"],
                                "error_message": fetch_res["error_message"],
                                "robots_status": fetch_res["robots_status"],
                                "waf_status": fetch_res["waf_status"],
                                "first_seen_source_url": src_url,
                                "checked_at": now_iso,
                            }

                            await res_q.put((src_update, rel_rows, cache_seeds, reuse_incs, self_cache_rec))
                        finally:
                            work_q.task_done()

                async def src_writer():
                    nonlocal processed_stage_a
                    s_buf, r_buf, c_buf, sc_buf = [], [], [], []
                    inc_buf = Counter()
                    last_log = 0

                    while True:
                        pkt = await res_q.get()
                        if pkt is None:
                            if s_buf:
                                await asyncio.to_thread(
                                    self._flush_source_batch_sync, s_buf, r_buf, c_buf, inc_buf, sc_buf
                                )
                                await asyncio.to_thread(self.sync_job_counters, "RUNNING")
                            res_q.task_done()
                            break

                        su, rls, seeds, incs, sc = pkt
                        s_buf.append(su)
                        r_buf.extend(rls)
                        c_buf.extend(seeds)
                        inc_buf.update(incs)
                        sc_buf.append(sc)
                        processed_stage_a += 1

                        if len(s_buf) >= self.batch_size:
                            await asyncio.to_thread(
                                self._flush_source_batch_sync, s_buf, r_buf, c_buf, inc_buf, sc_buf
                            )
                            stats = await asyncio.to_thread(self.sync_job_counters, "RUNNING")
                            s_buf, r_buf, c_buf, sc_buf = [], [], [], []
                            inc_buf = Counter()
                            if processed_stage_a - last_log >= max(self.batch_size, 250):
                                dt = max(0.01, time.time() - t_stage_a)
                                print(
                                    f"[Stage A] Sources: {stats['source_pages_processed']:,}/{stats['source_pages_total']:,} "
                                    f"({processed_stage_a/dt:.1f} pg/s) | Rels: {stats['url_relationships_persisted']:,} | "
                                    f"UniqHTTP: {stats['unique_http_destinations']:,} | Reused: {stats['reused_http_validations']:,}",
                                    flush=True,
                                )
                                last_log = processed_stage_a

                        res_q.task_done()

                prod_t = asyncio.create_task(src_producer(pending_sources))
                writ_t = asyncio.create_task(src_writer())
                w_tasks = [asyncio.create_task(src_worker()) for _ in range(self.source_concurrency)]
                await prod_t
                await asyncio.gather(*w_tasks)
                await res_q.put(None)
                await writ_t

                pending_sources = requeue_sources

            if max_source_pages_this_run is not None:
                return self.sync_job_counters(status_override="RUNNING")

            # ===================== STAGE B: UNIQUE HTTP DESTINATION VALIDATION =====================
            self.sync_job_counters(status_override="VALIDATING")
            pending_dests = [
                {"normalized_url": r[0], "url_scope": r[1], "waf_requeues": 0}
                for r in self.conn.execute(
                    """
                    SELECT normalized_url, url_scope
                    FROM on_page_validation_cache
                    WHERE job_id = ? AND referenced = 1 AND validation_state = 'PENDING'
                    """,
                    (self.job_id,),
                ).fetchall()
            ]
            t_stage_b = time.time()
            validated_stage_b = 0
            print(f"[Stage B] Unique PENDING HTTP destinations to validate: {len(pending_dests):,}", flush=True)

            while pending_dests:
                requeue_dests: List[Dict[str, Any]] = []
                d_work_q: asyncio.Queue = asyncio.Queue(maxsize=1000)
                d_res_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

                async def dest_producer(items):
                    for it in items:
                        await d_work_q.put(it)
                    for _ in range(self.dest_concurrency):
                        await d_work_q.put(None)

                async def dest_worker():
                    while True:
                        item = await d_work_q.get()
                        if item is None:
                            d_work_q.task_done()
                            break
                        try:
                            dest_url = item["normalized_url"]
                            fetch_res = await self.fetch_url_manual_redirects(client, dest_url, need_body=False)

                            if fetch_res["is_primary_waf"]:
                                self.consecutive_waf += 1
                                if item["waf_requeues"] < 2:
                                    item["waf_requeues"] += 1
                                    requeue_dests.append(item)
                                    if self.consecutive_waf >= 5:
                                        await self._trigger_primary_cooldown(client)
                                    continue
                                elif self.consecutive_waf >= 5:
                                    await self._trigger_primary_cooldown(client)
                            elif self._is_primary_host(dest_url):
                                self.consecutive_waf = 0

                            await d_res_q.put({
                                "job_id": self.job_id,
                                "normalized_url": dest_url,
                                "validation_state": fetch_res["terminal_state"],
                                "initial_http_status": fetch_res["initial_http_status"],
                                "http_status": fetch_res["http_status"],
                                "final_url": fetch_res["final_url"],
                                "redirect_count": fetch_res["redirect_count"],
                                "redirect_chain": json.dumps(fetch_res["redirect_chain"]),
                                "response_time_ms": fetch_res["response_time_ms"],
                                "content_type": fetch_res["content_type"],
                                "status_bucket": fetch_res["status_bucket"],
                                "status_classification": fetch_res["status_classification"],
                                "remark": fetch_res["remark"],
                                "error_type": fetch_res["error_type"],
                                "error_message": fetch_res["error_message"],
                                "robots_status": fetch_res["robots_status"],
                                "waf_status": fetch_res["waf_status"],
                                "checked_at": utc_now_iso(),
                            })
                        finally:
                            d_work_q.task_done()

                async def dest_writer():
                    nonlocal validated_stage_b
                    d_buf = []
                    last_log = 0
                    while True:
                        rec = await d_res_q.get()
                        if rec is None:
                            if d_buf:
                                await asyncio.to_thread(self._flush_dest_batch_sync, d_buf)
                                await asyncio.to_thread(self.sync_job_counters, "VALIDATING")
                            d_res_q.task_done()
                            break
                        d_buf.append(rec)
                        validated_stage_b += 1
                        if len(d_buf) >= max(self.batch_size, 100):
                            await asyncio.to_thread(self._flush_dest_batch_sync, d_buf)
                            stats = await asyncio.to_thread(self.sync_job_counters, "VALIDATING")
                            d_buf = []
                            if validated_stage_b - last_log >= 500:
                                dt = max(0.01, time.time() - t_stage_b)
                                done_d = (
                                    stats["unique_http_destinations_validated"]
                                    + stats["unique_http_destinations_failed"]
                                    + stats["unique_http_destinations_blocked"]
                                    + stats["unique_http_destinations_timed_out"]
                                    + stats["unique_http_destinations_excluded"]
                                )
                                print(
                                    f"[Stage B] Validated: {done_d:,}/{stats['unique_http_destinations']:,} "
                                    f"({validated_stage_b/dt:.1f} dest/s) | cooldowns={self.cooldown_count}",
                                    flush=True,
                                )
                                last_log = validated_stage_b
                        d_res_q.task_done()

                prod_t = asyncio.create_task(dest_producer(pending_dests))
                writ_t = asyncio.create_task(dest_writer())
                w_tasks = [asyncio.create_task(dest_worker()) for _ in range(self.dest_concurrency)]
                await prod_t
                await asyncio.gather(*w_tasks)
                await d_res_q.put(None)
                await writ_t

                pending_dests = requeue_dests

        # Finalize job status
        final_stats = self.sync_job_counters()
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
        final_stats = self.sync_job_counters(status_override=terminal_job_status)
        return final_stats

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
