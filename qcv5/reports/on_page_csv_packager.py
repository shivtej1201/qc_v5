"""
QC V5 on-page URL + image audit — streaming CSV report package (primary report format).

Every report is ONE streamed SQL query (cursor + fetchmany) written row-by-row with csv.writer, so
memory stays flat for 500K / 1M / 5M+ relationships; there are no per-row database lookups.
After writing, an independent trust gate re-counts everything from SQLite and compares it with
the CSV row counts. final_qc_status is PASS only if every check passes.

    01_summary.csv                 02_source_page_audit.csv        03_url_audit.csv
    04_unique_url_validation.csv   05_image_audit.csv              06_unique_image_validation.csv
    07_broken_urls.csv             08_redirect_urls.csv            09_broken_images.csv
    10_external_urls.csv           11_reconciliation.csv           12_issues.csv
    13_issue_summary.csv           14_page_issue_summary.csv       15_seo_page_audit.csv
    16_sitemap_audit.csv           -> <package_name>.zip

12-14 are the actionable layer: every source page / link / image problem with a severity,
plain-language detail and recommended fix, computed on the SAME streamed pass (no extra scan,
no extra HTTP requests). 15-16 are the SEO / technical-SEO layer: per-page SEO facts and per-sitemap
health; their issues (single-page and cross-page: duplicates, hreflang reciprocity, orphans, pages missing
from the sitemap, lastmod hygiene) go into 12-14 like every other issue.
"""
import csv
import json
import os
import sqlite3
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from qcv5.audit.on_page_issue_rules import (ACTIONS, BROKEN_IMAGE_TYPES, BROKEN_STATUS_TYPES, LINK_STATUS_CATEGORIES,
                                  SEVERITY_ORDER, action_for, image_issues, link_issues, source_issues)
from qcv5.audit.on_page_schema import ensure_on_page_image_schema
from qcv5.audit.issue_store import IssueSink, apply_lifecycle, compute_score, ensure_issue_schema
from qcv5.audit.seo_audit import (hreflang_reciprocity, lastmod_state, page_issues as seo_page_issues,
                       url_structure_issues)
from qcv5.crawler.url_normalizer import URLNormalizer

BROKEN_BUCKETS = ("4xx", "5xx", "Timeout", "DNS/Network", "SSL", "Robots blocked", "WAF blocked", "Other errors")
# Same rule as on_page_report_packager._is_broken_dest, expressed in SQL over the validation cache alias "c"
BROKEN_SQL = ("(c.status_bucket IN ('4xx','5xx','Timeout','DNS/Network','SSL','Robots blocked','WAF blocked','Other errors') "
              "OR (c.status_bucket = '3xx' AND (c.http_status IS NULL OR c.http_status >= 400)))")
BROKEN_IMG_SQL = ("(ic.status_bucket IN ('4xx','5xx','Timeout','DNS/Network','SSL','Robots blocked','WAF blocked','Other errors') "
                  "OR (ic.status_bucket = '3xx' AND (ic.http_status IS NULL OR ic.http_status >= 400)) "
                  "OR COALESCE(ic.image_issue, '') != '')")

# Report-level validation_source (project enums NEW_CHECK / REUSED refined with how the result was obtained)
URL_VALSRC_SQL = """CASE
    WHEN r.validation_source IN ('NEW_CHECK','REUSED') AND c.seeded_by = 'SOURCE_FETCH' THEN 'SOURCE_PAGE_REUSED'
    WHEN r.validation_source = 'REUSED' AND r.validation_detail = 'IN_FLIGHT_REUSED' THEN 'IN_FLIGHT_REUSED'
    ELSE r.validation_source END"""
IMG_VALSRC_SQL = """CASE
    WHEN i.validation_source = 'REUSED' AND i.validation_detail = 'IN_FLIGHT_REUSED' THEN 'IN_FLIGHT_REUSED'
    ELSE i.validation_source END"""
ALT_SQL = "CASE i.alt_present WHEN 1 THEN 'true' WHEN 0 THEN 'false' ELSE '' END"


def status_class_sql(a: str) -> str:
    """Fine-grained HTTP class of the URL's OWN response (first hop): 200, 301, 404, 503, 2xx…, or the
    transport failure (timeout, dns_error, ssl_error, connection_error, robots_blocked, waf_blocked, ssrf_blocked)."""
    code = f"COALESCE({a}.initial_http_status, {a}.http_status)"
    return f"""CASE
        WHEN {a}.validation_state IS NULL OR {a}.validation_state = 'PENDING' THEN ''
        WHEN {a}.status_bucket = 'Robots blocked' THEN 'robots_blocked'
        WHEN {a}.status_bucket = 'WAF blocked' THEN 'waf_blocked'
        WHEN {a}.status_classification LIKE '%SSRF%' THEN 'ssrf_blocked'
        WHEN {a}.status_bucket = 'Timeout' OR {a}.error_type = 'Timeout' THEN 'timeout'
        WHEN {a}.error_type = 'DNS Error' THEN 'dns_error'
        WHEN {a}.error_type = 'SSL Error' OR {a}.status_bucket = 'SSL' THEN 'ssl_error'
        WHEN {code} IS NULL THEN 'connection_error'
        WHEN {code} IN (200, 301, 302, 307, 308, 400, 401, 403, 404, 410, 429, 500, 502, 503, 504) THEN CAST({code} AS TEXT)
        WHEN {code} BETWEEN 200 AND 299 THEN '2xx' WHEN {code} BETWEEN 300 AND 399 THEN '3xx'
        WHEN {code} BETWEEN 400 AND 499 THEN '4xx' ELSE '5xx' END"""


# Same classes as status_class_sql, computed from the source page's own columns (no self-join: O(pages)).
SOURCE_STATUS_CLASS_SQL = """CASE
        WHEN s.crawl_state = 'PENDING' THEN ''
        WHEN s.robots_status = 'DISALLOWED' OR s.error_type = 'ROBOTS_BLOCKED' THEN 'robots_blocked'
        WHEN COALESCE(s.waf_status, 'NONE') NOT IN ('', 'NONE') THEN 'waf_blocked'
        WHEN s.error_type LIKE '%SSRF%' THEN 'ssrf_blocked'
        WHEN s.crawl_state = 'TIMED_OUT' OR s.error_type = 'Timeout' THEN 'timeout'
        WHEN s.error_type = 'DNS Error' THEN 'dns_error'
        WHEN s.error_type = 'SSL Error' THEN 'ssl_error'
        WHEN COALESCE(s.initial_http_status, s.http_status) IS NULL THEN 'connection_error'
        WHEN COALESCE(s.initial_http_status, s.http_status) IN (200, 301, 302, 307, 308, 400, 401, 403, 404, 410, 429, 500, 502, 503, 504)
             THEN CAST(COALESCE(s.initial_http_status, s.http_status) AS TEXT)
        WHEN COALESCE(s.initial_http_status, s.http_status) BETWEEN 200 AND 299 THEN '2xx'
        WHEN COALESCE(s.initial_http_status, s.http_status) BETWEEN 300 AND 399 THEN '3xx'
        WHEN COALESCE(s.initial_http_status, s.http_status) BETWEEN 400 AND 499 THEN '4xx' ELSE '5xx' END"""

IMG_FORMAT_SQL = """CASE
    WHEN LOWER(COALESCE(ic.content_type, '')) LIKE 'image/%' THEN SUBSTR(LOWER(ic.content_type), 7, 12)
    WHEN LOWER(i.normalized_image_url) LIKE '%.jpg%' OR LOWER(i.normalized_image_url) LIKE '%.jpeg%' THEN 'jpeg'
    WHEN LOWER(i.normalized_image_url) LIKE '%.png%' THEN 'png' WHEN LOWER(i.normalized_image_url) LIKE '%.webp%' THEN 'webp'
    WHEN LOWER(i.normalized_image_url) LIKE '%.avif%' THEN 'avif' WHEN LOWER(i.normalized_image_url) LIKE '%.gif%' THEN 'gif'
    WHEN LOWER(i.normalized_image_url) LIKE '%.svg%' THEN 'svg+xml' ELSE '' END"""
LAZY_SQL = ("CASE WHEN LOWER(COALESCE(i.loading, '')) = 'lazy' OR i.attribute_source LIKE '%data-%' "
            "OR i.attribute_source LIKE '%lazy%' THEN 'true' ELSE 'false' END")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe(v: Any) -> Any:
    """CSV/Excel formula-injection guard (same policy as the rest of QC V5 exports)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") and not v.startswith(("http://", "https://")):
        return "'" + v
    return "" if v is None else v


def _stream(conn: sqlite3.Connection, path: str, header: List[str], sql: str, args: tuple,
            batch: int = 5000, on_row: Optional[Callable[[Dict[str, Any]], None]] = None) -> int:
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        cur = conn.execute(sql, args)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            w.writerows([[_safe(v) for v in row] for row in rows])
            if on_row is not None:
                for row in rows:
                    on_row(dict(zip(header, row)))
            n += len(rows)
        cur.close()
    return n


ISSUE_HEADER = ["issue_id", "severity", "category", "issue_type", "source_page_url", "element_type", "found_url",
                "normalized_url", "anchor_or_alt_text", "location", "http_status", "initial_status", "final_url",
                "hop_count", "detail", "recommended_action", "validation_source", "issue_key", "status", "score_dimension"]


DATASETS = {
    "00_manifest.csv": "Report manifest: rows, bytes, SHA-256 and validation status of every file",
    "01_summary.csv": "Run summary, totals, score, reconciliation status",
    "02_source_page_audit.csv": "URL audit: one row per sitemap/source page",
    "03_url_audit.csv": "Link audit: one row per page -> URL relationship (links, SEO signals, resources, forms)",
    "04_unique_url_validation.csv": "One row per unique URL validated",
    "05_image_audit.csv": "Image audit: one row per page -> image placement",
    "06_unique_image_validation.csv": "One row per unique image validated",
    "07_broken_urls.csv": "Broken URL relationships",
    "08_redirect_urls.csv": "Redirecting URL relationships",
    "09_broken_images.csv": "Broken image placements",
    "10_external_urls.csv": "External URL relationships",
    "11_reconciliation.csv": "Trust-gate checks (expected vs actual)",
    "12_issues.csv": "Issues (one per page + rule + subject) with severity, fix and lifecycle status",
    "13_issue_summary.csv": "Issues per type",
    "14_page_issue_summary.csv": "Issues per page, worst first",
    "15_seo_page_audit.csv": "SEO facts per 2xx HTML page",
    "16_sitemap_audit.csv": "Health per sitemap file",
    "17_input_exceptions.csv": "Input URLs not crawled as pages (invalid or duplicate) with the reason",
}


def _validate_csv(path: str) -> Dict[str, Any]:
    """Re-read a written CSV: UTF-8 decodable, every row has the header's column count, SHA-256 of the bytes.
    Streams (bounded memory) so it also works for multi-GB files."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fb:
        for chunk in iter(lambda: fb.read(1 << 20), b""):
            h.update(chunk)
    rows, bad_cols, ncols, encoding_ok = 0, 0, None, True
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for i, row in enumerate(csv.reader(f)):
                if i == 0:
                    ncols = len(row)
                    continue
                rows += 1
                if len(row) != ncols:
                    bad_cols += 1
    except UnicodeDecodeError:
        encoding_ok = False
    return {"rows": rows, "columns": ncols or 0, "bad_rows": bad_cols, "encoding_ok": encoding_ok,
            "bytes": os.path.getsize(path), "sha256": h.hexdigest()}


class IssueWriter:
    """Issues go to SQLite first (on_page_issues, UNIQUE per issue_key => no duplicates even if a rule fires twice
    or the package is rebuilt); 12/13/14 are then streamed from the database, so CSVs and DB always agree."""

    def __init__(self, path: str, conn: sqlite3.Connection, job_id: str, project_id: str = ""):
        self.path, self.conn, self.job_id, self.project_id = path, conn, job_id, project_id
        self.sink = IssueSink(conn, job_id)
        self.n = 0
        self.offered = 0
        self.by_sev = {k: 0 for k in SEVERITY_ORDER}
        self.pages_with_issues = 0
        self.broken_link_rows = 0
        self.broken_image_rows = 0

    def add(self, category: str, itype: str, sev: str, detail: str, page: str, element: str, found: Any,
            norm: Any, text: Any, location: Any, status: Any, initial: Any, final: Any, hops: Any, vsrc: Any):
        self.sink.add(category, itype, sev, detail, page, element, found, norm, text, location, status, initial,
                      final, hops, vsrc)

    def close(self):
        """Flush to the DB, then stream 12_issues.csv (one row per stored issue, with lifecycle status)."""
        self.n = self.sink.stored()
        self.offered = self.sink.offered
        J = (self.job_id,)
        pending = self.conn.execute("SELECT COUNT(*) FROM on_page_source_pages WHERE job_id = ? AND crawl_state = 'PENDING'",
                                    J).fetchone()[0]
        self.lifecycle = apply_lifecycle(self.conn, self.job_id, self.project_id, complete=(pending == 0))
        for sev, cnt in self.conn.execute("SELECT severity, COUNT(*) FROM on_page_issues WHERE job_id = ? GROUP BY severity", J):
            self.by_sev[sev] = cnt
        lc = ",".join(f"'{c}'" for c in sorted(LINK_STATUS_CATEGORIES))
        bs = ",".join(f"'{t}'" for t in sorted(BROKEN_STATUS_TYPES))
        bi = ",".join(f"'{t}'" for t in sorted(BROKEN_IMAGE_TYPES))
        self.broken_link_rows = self.conn.execute(
            f"SELECT COUNT(*) FROM on_page_issues WHERE job_id = ? AND category IN ({lc}) AND rule_code IN ({bs})", J).fetchone()[0]
        self.broken_image_rows = self.conn.execute(
            f"SELECT COUNT(*) FROM on_page_issues WHERE job_id = ? AND category = 'IMAGE' AND rule_code IN ({bi})", J).fetchone()[0]
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(ISSUE_HEADER)
            cur = self.conn.execute(
                """SELECT i.severity, i.category, i.rule_code, i.page_url, i.element_type, i.found_url, i.subject_url,
                          i.anchor_or_alt_text, i.location, i.http_status, i.initial_status, i.final_url, i.hop_count,
                          i.detail, i.validation_source, i.issue_key, COALESCE(st.status, 'OPEN'), i.dimension
                     FROM on_page_issues i
                     LEFT JOIN on_page_issue_status st ON st.project_id = ? AND st.issue_key = i.issue_key
                    WHERE i.job_id = ? ORDER BY i.id""", (self.project_id, self.job_id))
            n = 0
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for r in rows:
                    n += 1
                    w.writerow([_safe(v) for v in (n, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10],
                                                   r[11], r[12], r[13], action_for(r[2], r[1]), r[14], r[15], r[16], r[17])])
            cur.close()
        self.csv_rows = n

    def write_summaries(self, type_path: str, page_path: str) -> Tuple[int, int, int]:
        sev_case = "CASE severity " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in SEVERITY_ORDER.items()) + " END"
        total = n_types = 0
        with open(type_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["severity", "category", "issue_type", "relationships", "unique_urls", "source_pages_affected",
                        "recommended_action", "example_source_page", "example_url"])
            for sev, cat, t, rows, urls, pages, ex_page, ex_url in self.conn.execute(
                    f"""SELECT severity, category, rule_code, COUNT(*), COUNT(DISTINCT NULLIF(subject_url, '')),
                               COUNT(DISTINCT page_url), MIN(page_url), MIN(NULLIF(subject_url, ''))
                          FROM on_page_issues WHERE job_id = ? GROUP BY severity, category, rule_code
                         ORDER BY {sev_case}, COUNT(*) DESC, rule_code""", (self.job_id,)):
                w.writerow([_safe(v) for v in (sev, cat, t, rows, urls, pages, action_for(t, cat), ex_page, ex_url or "")])
                total += rows
                n_types += 1
        pages: Dict[str, Dict[str, Any]] = {}
        for page, sev, rule, cnt in self.conn.execute(
                "SELECT page_url, severity, rule_code, COUNT(*) FROM on_page_issues WHERE job_id = ? "
                "GROUP BY page_url, severity, rule_code", (self.job_id,)):
            pg = pages.get(page)
            if pg is None:
                pg = pages[page] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0, "_types": {}}
            pg[sev] += cnt
            pg["_types"][rule] = pg["_types"].get(rule, 0) + cnt
        page_total = 0
        with open(page_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source_page_url", "high", "medium", "low", "info", "total_issues", "top_issue_types"])
            for page, c in sorted(pages.items(), key=lambda kv: (-kv[1]["HIGH"], -kv[1]["MEDIUM"], -kv[1]["LOW"], kv[0])):
                t = c["HIGH"] + c["MEDIUM"] + c["LOW"] + c["INFO"]
                page_total += t
                top = sorted(c["_types"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
                w.writerow([_safe(page), c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"], t,
                            "; ".join(f"{k} x{v}" for k, v in top)])
        self.pages_with_issues = len(pages)
        return n_types, total, page_total


def build_on_page_csv_package(
    db_path: str,
    job_id: str,
    output_dir: str,
    package_name: str = "QC_V5_10K_AUDIT",
    performance: Optional[Dict[str, Any]] = None,
    full_integrity_check: bool = True,
    project_id: str = "",
) -> Dict[str, Any]:
    t0 = time.time()
    os.makedirs(output_dir, exist_ok=True)
    work = os.path.join(output_dir, package_name)
    os.makedirs(work, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000;")
    ensure_on_page_image_schema(conn)
    J = (job_id,)
    files: Dict[str, int] = {}

    def out(name: str) -> str:
        return os.path.join(work, name)

    # canonical evidence per source page (small: one row per page)
    canon: Dict[str, Tuple[int, str]] = {
        r[0]: (r[1], r[2]) for r in conn.execute(
            """SELECT source_page_url, COUNT(DISTINCT normalized_found_url), MIN(normalized_found_url)
                 FROM on_page_link_relationships
                WHERE job_id = ? AND (found_url_type = 'CANONICAL' OR link_types LIKE '%"CANONICAL"%')
                GROUP BY source_page_url""", J)
    }
    anchors: Dict[str, int] = {
        r[0]: r[1] for r in conn.execute(
            """SELECT source_page_url, COUNT(*) FROM on_page_link_relationships
                WHERE job_id = ? AND (found_url_type IN ('ANCHOR', 'AREA') OR link_types LIKE '%"ANCHOR"%'
                                      OR link_types LIKE '%"AREA"%')
                GROUP BY source_page_url""", J)
    }
    ensure_issue_schema(conn)
    if not project_id:
        first = conn.execute("SELECT source_page_url FROM on_page_source_pages WHERE job_id = ? ORDER BY source_page_id LIMIT 1", J).fetchone()
        host = (urllib.parse.urlsplit(first[0]).hostname or "") if first else ""
        project_id = host[4:] if host.startswith("www.") else host
    issues = IssueWriter(out("12_issues.csv"), conn, job_id, project_id)

    # per-page facts gathered while streaming 02 (one small entry per page) for SEO + sitemap reports
    page_info: Dict[str, Dict[str, Any]] = {}
    sm_agg: Dict[str, Dict[str, int]] = {}
    slash_groups: Dict[str, List[str]] = {}

    def _sm(key: str) -> Dict[str, int]:
        a = sm_agg.get(key)
        if a is None:
            a = sm_agg[key] = {k: 0 for k in ("pages_audited", "pages_2xx", "pages_redirected", "pages_4xx_5xx",
                                              "pages_failed_or_blocked", "pages_pending", "noindex_pages",
                                              "non_canonical_pages", "issues")}
        return a

    def on_source(r: Dict[str, Any]):
        page = r["source_page_url"]
        found = source_issues(r, canon.get(page), anchors.get(page, 0))
        if r["crawl_state"] != "PENDING":
            found = found + url_structure_issues(page)
        for cat, t, sev, detail in found:
            issues.add(cat, t, sev, detail, page, "SOURCE_PAGE", page,
                       r["normalized_source_url"], r.get("page_title"), "", r["http_status"],
                       r["initial_http_status"], r["final_url"], r["redirect_count"], "")
        sm_key = r.get("source_sitemap") or ""
        a = _sm(sm_key)
        a["pages_audited"] += 1
        a["issues"] += len(found)
        code, state = r["http_status"], r["crawl_state"]
        if state == "PENDING":
            a["pages_pending"] += 1
        elif state in ("FAILED", "BLOCKED", "TIMED_OUT"):
            a["pages_failed_or_blocked"] += 1
        elif code is not None and code >= 400:
            a["pages_4xx_5xx"] += 1
        elif code is not None and 200 <= code <= 299:
            a["pages_2xx"] += 1
        if int(r.get("redirect_count") or 0) > 0:
            a["pages_redirected"] += 1
        non_canon = any(t == "SITEMAP_URL_NOT_CANONICAL" for _, t, _, _ in found)
        if non_canon:
            a["non_canonical_pages"] += 1
        nf = URLNormalizer.normalize(str(r.get("final_url") or "")) if r.get("final_url") else ""
        page_info[page] = {"ns": r["normalized_source_url"] or "", "nf": nf, "sitemap": sm_key,
                           "non_canonical": non_canon, "ok": code is not None and 200 <= code <= 299,
                           "status": code, "final": r.get("final_url"), "title": r.get("page_title")}
        ns = str(r["normalized_source_url"] or "")
        slash_groups.setdefault(ns.rstrip("/"), []).append(page)

    seo_rows_for_cross: List[Tuple[str, str, str, str]] = []
    dupe_eligible: set = set()

    def on_seo(r: Dict[str, Any]):
        page = r["source_page_url"]
        info = page_info.get(page, {})
        found = seo_page_issues(r, {info.get("ns", ""), info.get("nf", "")} - {""})
        for cat, t, sev, detail in found:
            issues.add(cat, t, sev, detail, page, "SOURCE_PAGE", page, info.get("ns"), r.get("title"), "",
                       info.get("status"), None, info.get("final"), None, "")
        a = _sm(info.get("sitemap", ""))
        a["issues"] += len(found)
        if int(r.get("noindex") or 0):
            a["noindex_pages"] += 1
        if int(r.get("hreflang_count") or 0):
            seo_rows_for_cross.append((page, info.get("ns", ""), info.get("nf", ""), r.get("hreflang_json") or "[]"))
        if int(r.get("indexable") or 0) and not info.get("non_canonical"):
            dupe_eligible.add(page)

    def on_url(r: Dict[str, Any]):
        for cat, t, sev, detail in link_issues(r):
            issues.add(cat, t, sev, detail, r["source_page_url"], r["element_type"], r["found_url"],
                       r["normalized_url"], r["anchor_text"], r["location"], r["http_status"], r["initial_status"],
                       r["final_url"], r["hop_count"], r["validation_source"])

    def on_image(r: Dict[str, Any]):
        for cat, t, sev, detail in image_issues(r):
            issues.add(cat, t, sev, detail, r["source_page_url"], r["attribute_source"], r["image_url"],
                       r["normalized_image_url"], r["alt_text"], r["location"], r["http_status"], r["initial_status"],
                       r["final_url"], r["hop_count"], r["validation_source"])

    # one canonical per page, computed ONCE (a correlated subquery per page scanned every relationship: O(N*R))
    conn.execute("DROP TABLE IF EXISTS temp._canon")
    conn.execute("""CREATE TEMP TABLE _canon AS SELECT source_page_url AS page, MIN(normalized_found_url) AS url
                      FROM on_page_link_relationships WHERE job_id = ? AND found_url_type = 'CANONICAL'
                     GROUP BY source_page_url""", J)
    conn.execute("CREATE UNIQUE INDEX temp._canon_page ON _canon(page)")

    # ---------------------------------------------------------------- 02 source pages
    files["02_source_page_audit.csv"] = _stream(conn, out("02_source_page_audit.csv"), [
        "source_page_id", "source_page_url", "normalized_source_url", "source_sitemap", "crawl_state",
        "initial_http_status", "http_status", "final_url", "redirect_count", "redirect_chain", "content_type",
        "response_time_ms", "robots_status", "waf_status", "business", "language", "page_type",
        "classification_method", "raw_url_occurrences", "url_relationships", "raw_image_occurrences",
        "image_relationships", "error_type", "error_message", "remark", "checked_at",
        "page_title", "html_bytes", "html_truncated", "attempts",
        "status_class", "canonical_url", "canonical_status", "meta_robots", "x_robots_tag", "indexable",
        "meta_description", "meta_description_length", "h1", "h1_count", "html_lang", "word_count",
        "internal_link_count", "external_link_count", "structured_data_types", "title_length",
    ], f"""SELECT s.source_page_id, s.source_page_url, s.normalized_source_url, s.source_sitemap, s.crawl_state,
              s.initial_http_status, s.http_status, s.final_url, s.redirect_count, s.redirect_chain, s.content_type,
              s.response_time_ms, s.robots_status, s.waf_status, s.business, s.language, s.page_type, s.classification_method,
              s.raw_occurrences_count, s.relationships_count, s.raw_image_occurrences_count, s.image_relationships_count,
              s.error_type, s.error_message, s.remark, s.checked_at, s.page_title, s.html_bytes, s.html_truncated, s.attempts,
              {SOURCE_STATUS_CLASS_SQL},
              k.url, COALESCE(CAST(cc.http_status AS TEXT), cc.status_bucket),
              p.meta_robots, p.x_robots_tag, p.indexable, p.meta_description, p.meta_description_length, p.h1_text,
              p.h1_count, p.html_lang, p.word_count, p.internal_links, p.external_links, p.schema_types, p.title_length
         FROM on_page_source_pages s
         LEFT JOIN on_page_seo_pages p ON p.job_id = s.job_id AND p.source_page_url = s.source_page_url
         LEFT JOIN temp._canon k ON k.page = s.source_page_url
         LEFT JOIN on_page_validation_cache cc ON cc.job_id = s.job_id AND cc.normalized_url = k.url
        WHERE s.job_id = ? ORDER BY s.source_page_id""", J, on_row=on_source)

    url_join = ("FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c "
                "ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url WHERE r.job_id = ?")

    # ---------------------------------------------------------------- 03 URL relationships
    files["03_url_audit.csv"] = _stream(conn, out("03_url_audit.csv"), [
        "source_page_url", "found_url", "normalized_url", "url_type", "internal_external", "element_type",
        "anchor_text", "occurrence_count", "http_status", "initial_status", "final_url", "redirect_chain",
        "status_bucket", "validation_state", "validation_source", "base_validation_source", "remarks", "checked_at",
        "location", "hop_count", "error_type", "error_message", "status_classification", "response_time_ms",
        "status_class", "url_category", "rel", "nofollow", "hreflang", "language", "business", "page_type",
    ], f"""SELECT r.source_page_url, r.found_url, r.normalized_found_url, r.url_syntax_type, r.found_url_scope,
              r.found_url_type, r.anchor_text, r.occurrence_count, c.http_status, c.initial_http_status, c.final_url,
              c.redirect_chain, c.status_bucket, c.validation_state, {URL_VALSRC_SQL}, r.validation_source,
              TRIM(COALESCE(r.remark, '') || CASE WHEN c.remark IS NOT NULL THEN ' | ' || c.remark ELSE '' END, ' |'),
              c.checked_at, r.representative_location, c.redirect_count, c.error_type, c.error_message,
              c.status_classification, c.response_time_ms, {status_class_sql("c")},
              CASE WHEN r.found_url_type IN ('ANCHOR','AREA','IFRAME','META_REFRESH','EMBED') THEN 'NAVIGATION'
                   WHEN r.found_url_type IN ('CANONICAL','HREFLANG','LINK_META','SOCIAL_URL','JSONLD_URL') THEN 'SEO_SIGNAL'
                   WHEN r.found_url_type = 'FORM' THEN 'FORM' ELSE 'RESOURCE' END,
              r.rel, CASE WHEN (' ' || LOWER(COALESCE(r.rel, '')) || ' ') LIKE '% nofollow %' THEN 'true' ELSE 'false' END,
              r.hreflang, r.language, r.business, r.page_type
         {url_join} ORDER BY r.id""", J, on_row=on_url)

    # ---------------------------------------------------------------- 04 unique URL validations
    files["04_unique_url_validation.csv"] = _stream(conn, out("04_unique_url_validation.csv"), [
        "normalized_url", "original_url", "url_scope", "http_status", "initial_status", "final_url", "redirect_chain",
        "validation_state", "status_bucket", "first_seen_source_url", "reuse_count", "validation_source", "error",
        "robots_status", "waf_status", "checked_at",
    ], """SELECT c.normalized_url,
              (SELECT r.found_url FROM on_page_link_relationships r WHERE r.job_id = c.job_id
                  AND r.normalized_found_url = c.normalized_url ORDER BY r.id LIMIT 1),
              c.url_scope, c.http_status, c.initial_http_status, c.final_url, c.redirect_chain, c.validation_state,
              c.status_bucket, c.first_seen_source_url, c.reuse_count,
              CASE WHEN c.seeded_by = 'SOURCE_FETCH' THEN 'SOURCE_PAGE_REUSED' ELSE 'NEW_CHECK' END,
              TRIM(COALESCE(c.error_type, '') || CASE WHEN COALESCE(c.error_message, '') != '' THEN ': ' || c.error_message ELSE '' END),
              c.robots_status, c.waf_status, c.checked_at
         FROM on_page_validation_cache c WHERE c.job_id = ? AND c.referenced = 1 ORDER BY c.normalized_url""", J)

    img_join = ("FROM on_page_image_relationships i LEFT JOIN on_page_image_validation_cache ic "
                "ON ic.job_id = i.job_id AND ic.normalized_url = i.normalized_image_url WHERE i.job_id = ?")

    # ---------------------------------------------------------------- 05 image relationships
    files["05_image_audit.csv"] = _stream(conn, out("05_image_audit.csv"), [
        "source_page_url", "image_url", "normalized_image_url", "image_type", "attribute_source", "alt_present",
        "alt_text", "width", "height", "loading", "internal_external", "occurrence_count", "http_status", "final_url",
        "content_type", "content_length", "validation_state", "image_issue", "validation_source", "remarks", "checked_at",
        "location", "initial_status", "hop_count", "status_bucket", "error_type", "error_message", "status_classification",
        "status_class", "image_format", "lazy_loaded", "error_reason", "language", "business", "page_type",
    ], f"""SELECT i.source_page_url, i.image_url, i.normalized_image_url, i.image_type, i.attribute_source, {ALT_SQL},
              i.alt_text, i.width, i.height, i.loading, i.internal_external, i.occurrence_count, ic.http_status,
              ic.final_url, ic.content_type, ic.content_length, ic.validation_state, ic.image_issue, {IMG_VALSRC_SQL},
              TRIM(COALESCE(i.remark, '') || CASE WHEN ic.remark IS NOT NULL THEN ' | ' || ic.remark ELSE '' END, ' |'),
              ic.checked_at, i.representative_location, ic.initial_http_status, ic.redirect_count, ic.status_bucket,
              ic.error_type, ic.error_message, ic.status_classification, {status_class_sql("ic")}, {IMG_FORMAT_SQL},
              {LAZY_SQL},
              TRIM(COALESCE(NULLIF(ic.image_issue, ''), '') || CASE WHEN COALESCE(ic.error_type, '') != ''
                   THEN ' ' || ic.error_type || ': ' || COALESCE(ic.error_message, '') ELSE '' END),
              i.language, i.business, i.page_type
         {img_join} ORDER BY i.id""", J, on_row=on_image)

    # ---------------------------------------------------------------- 06 unique image validations
    files["06_unique_image_validation.csv"] = _stream(conn, out("06_unique_image_validation.csv"), [
        "normalized_image_url", "image_type", "internal_external", "http_status", "initial_status", "final_url",
        "redirect_chain", "content_type", "content_length", "validation_state", "status_bucket", "image_issue",
        "first_seen_source_url", "reuse_count", "validation_source", "error", "robots_status", "waf_status", "checked_at",
    ], """SELECT ic.normalized_url, ic.image_type, ic.internal_external, ic.http_status, ic.initial_http_status,
              ic.final_url, ic.redirect_chain, ic.content_type, ic.content_length, ic.validation_state, ic.status_bucket,
              ic.image_issue, ic.first_seen_source_url, ic.reuse_count, 'NEW_CHECK',
              TRIM(COALESCE(ic.error_type, '') || CASE WHEN COALESCE(ic.error_message, '') != '' THEN ': ' || ic.error_message ELSE '' END),
              ic.robots_status, ic.waf_status, ic.checked_at
         FROM on_page_image_validation_cache ic WHERE ic.job_id = ? ORDER BY ic.normalized_url""", J)

    http_url = "r.found_url_scope IN ('INTERNAL','EXTERNAL')"
    # ---------------------------------------------------------------- 07 broken URLs (every relationship)
    files["07_broken_urls.csv"] = _stream(conn, out("07_broken_urls.csv"), [
        "source_page_url", "found_url", "normalized_url", "http_status", "final_url", "status_bucket", "error",
        "validation_source", "remarks",
    ], f"""SELECT r.source_page_url, r.found_url, r.normalized_found_url, c.http_status, c.final_url, c.status_bucket,
              TRIM(COALESCE(c.error_type, '') || CASE WHEN COALESCE(c.error_message, '') != '' THEN ': ' || c.error_message ELSE '' END),
              {URL_VALSRC_SQL}, c.remark
         {url_join} AND {http_url} AND {BROKEN_SQL} ORDER BY r.id""", J)

    # ---------------------------------------------------------------- 08 redirects (every relationship)
    files["08_redirect_urls.csv"] = _stream(conn, out("08_redirect_urls.csv"), [
        "source_page_url", "found_url", "normalized_url", "initial_status", "http_status", "final_url",
        "redirect_chain", "hop_count", "validation_source",
    ], f"""SELECT r.source_page_url, r.found_url, r.normalized_found_url, c.initial_http_status, c.http_status, c.final_url,
              c.redirect_chain, c.redirect_count, {URL_VALSRC_SQL}
         {url_join} AND {http_url} AND COALESCE(c.redirect_count, 0) > 0 ORDER BY r.id""", J)

    # ---------------------------------------------------------------- 09 broken images (every relationship)
    files["09_broken_images.csv"] = _stream(conn, out("09_broken_images.csv"), [
        "source_page_url", "image_url", "normalized_image_url", "http_status", "final_url", "content_type",
        "status_bucket", "image_issue", "error", "validation_source", "remarks",
    ], f"""SELECT i.source_page_url, i.image_url, i.normalized_image_url, ic.http_status, ic.final_url, ic.content_type,
              ic.status_bucket, ic.image_issue,
              TRIM(COALESCE(ic.error_type, '') || CASE WHEN COALESCE(ic.error_message, '') != '' THEN ': ' || ic.error_message ELSE '' END),
              {IMG_VALSRC_SQL}, ic.remark
         {img_join} AND i.validation_source IN ('NEW_CHECK','REUSED') AND {BROKEN_IMG_SQL} ORDER BY i.id""", J)

    # ---------------------------------------------------------------- 10 external URLs (every relationship)
    files["10_external_urls.csv"] = _stream(conn, out("10_external_urls.csv"), [
        "source_page_url", "found_url", "normalized_url", "element_type", "anchor_text", "http_status", "final_url",
        "status_bucket", "validation_source", "remarks",
    ], f"""SELECT r.source_page_url, r.found_url, r.normalized_found_url, r.found_url_type, r.anchor_text, c.http_status,
              c.final_url, c.status_bucket, {URL_VALSRC_SQL}, c.remark
         {url_join} AND r.found_url_scope = 'EXTERNAL' ORDER BY r.id""", J)

    # ---------------------------------------------------------------- 15 SEO page audit (+ SEO issues)
    seo_cols = ["source_page_url", "final_url", "http_status", "indexable", "noindex", "nofollow", "meta_robots",
                "x_robots_tag", "title", "title_length", "title_count", "meta_description", "meta_description_length",
                "meta_description_count", "h1_count", "h1_text", "h2_count", "heading_skips", "html_lang",
                "has_viewport", "has_charset", "meta_refresh", "canonical_outside_head", "jsonld_blocks",
                "jsonld_invalid", "schema_types", "og_title", "og_description", "og_image", "twitter_card",
                "word_count", "content_hash", "hreflang_count", "hreflang_x_default", "hreflang_json",
                "insecure_resources", "insecure_examples", "internal_links", "external_links",
                "internal_nofollow_links", "url_length", "checked_at", "script_count", "js_rendering_suspected"]
    files["15_seo_page_audit.csv"] = _stream(conn, out("15_seo_page_audit.csv"), seo_cols,
        "SELECT " + ", ".join("s.http_status" if c == "http_status" else "p." + c for c in seo_cols) +
        """ FROM on_page_seo_pages p JOIN on_page_source_pages s
                ON s.job_id = p.job_id AND s.source_page_url = p.source_page_url
             WHERE p.job_id = ? ORDER BY s.source_page_id""", J, on_row=on_seo)

    def cross(cat: str, itype: str, sev: str, page: str, detail: str, text: Any = ""):
        info = page_info.get(page, {})
        issues.add(cat, itype, sev, detail, page, "SOURCE_PAGE", page, info.get("ns"), text, "",
                   info.get("status"), None, info.get("final"), None, "")
        if info:
            _sm(info["sitemap"])["issues"] += 1

    # cross-page duplicates (indexable, self-canonical pages only: canonicalized duplicates are intended)
    for col, itype, sev, extra in (("title", "DUPLICATE_TITLE", "MEDIUM", "title != ''"),
                                   ("meta_description", "DUPLICATE_META_DESCRIPTION", "LOW", "meta_description != ''"),
                                   ("h1_text", "DUPLICATE_H1", "LOW", "h1_count = 1 AND h1_text != ''"),
                                   ("content_hash", "DUPLICATE_CONTENT", "MEDIUM", "content_hash != '' AND word_count >= 50")):
        groups: Dict[str, List[str]] = {}
        for val, page in conn.execute(
                f"""SELECT {col}, source_page_url FROM on_page_seo_pages WHERE job_id = ? AND {extra} AND {col} IN (
                        SELECT {col} FROM on_page_seo_pages WHERE job_id = ? AND {extra}
                         GROUP BY {col} HAVING COUNT(*) > 1) ORDER BY {col}, source_page_url""", (job_id, job_id)):
            if page in dupe_eligible:
                groups.setdefault(val, []).append(page)
        for val, pages in groups.items():
            if len(pages) < 2:
                continue
            for pg in pages:
                other = next(p for p in pages if p != pg)
                label = "identical visible text" if col == "content_hash" else f"'{str(val)[:80]}'"
                cross("CONTENT" if col == "content_hash" else "ON_PAGE_SEO", itype, sev, pg,
                      f"{label} shared by {len(pages)} pages, e.g. {other}", "" if col == "content_hash" else val)

    # hreflang reciprocity between audited pages
    for pg, target, code in hreflang_reciprocity(seo_rows_for_cross):
        cross("INTERNATIONAL", "HREFLANG_NOT_RECIPROCAL", "MEDIUM", pg, f"hreflang={code} -> {target} has no return link")

    # trailing-slash duplicates inside the sitemap
    for key, pages in slash_groups.items():
        if len(pages) > 1:
            for pg in pages:
                cross("URL_STRUCTURE", "TRAILING_SLASH_DUPLICATE", "MEDIUM", pg,
                      "Listed with and without trailing slash: " + " , ".join(p for p in pages if p != pg)[:200])

    # orphan pages: no <a>/<area> link from ANOTHER audited page (needs 2+ audited pages)
    ok_pages = [p for p, i in page_info.items() if i.get("ok")]
    if len(ok_pages) > 1:
        inbound: Dict[str, Tuple[int, str, str]] = {
            r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                """SELECT normalized_found_url, COUNT(DISTINCT source_page_url), MIN(source_page_url), MAX(source_page_url)
                     FROM on_page_link_relationships
                    WHERE job_id = ? AND found_url_scope = 'INTERNAL'
                      AND (found_url_type IN ('ANCHOR', 'AREA') OR link_types LIKE '%"ANCHOR"%' OR link_types LIKE '%"AREA"%')
                    GROUP BY normalized_found_url""", J)}
        for pg in ok_pages:
            info = page_info[pg]
            linked = False
            for k in {info["ns"], info["nf"]} - {""}:
                n, lo, hi = inbound.get(k, (0, "", ""))
                if n >= 2 or (n == 1 and lo != pg):
                    linked = True
                    break
            if not linked:
                cross("TECH_SEO", "ORPHAN_PAGE", "LOW", pg,
                      f"No <a> link to this page from the other {len(page_info) - 1} audited page(s)")
        del inbound

    # internal 200 HTML pages linked from the site but not in the audited sitemap (one row per unique URL)
    if page_info:
        sitemap_norms = {i["ns"] for i in page_info.values()} | {i["nf"] for i in page_info.values()}
        for url, first_src, reuse in conn.execute(
                """SELECT c.normalized_url, c.first_seen_source_url, c.reuse_count FROM on_page_validation_cache c
                    WHERE c.job_id = ? AND c.referenced = 1 AND c.url_scope = 'INTERNAL'
                      AND c.http_status BETWEEN 200 AND 299 AND COALESCE(c.redirect_count, 0) = 0
                      AND LOWER(COALESCE(c.content_type, '')) LIKE '%html%'
                      AND EXISTS (SELECT 1 FROM on_page_link_relationships r WHERE r.job_id = c.job_id
                                   AND r.normalized_found_url = c.normalized_url
                                   AND (r.found_url_type IN ('ANCHOR', 'AREA') OR r.link_types LIKE '%"ANCHOR"%'))
                    ORDER BY c.normalized_url""", J):
            if url in sitemap_norms:
                continue
            info = page_info.get(first_src, {})
            issues.add("SITEMAP", "NOT_IN_SITEMAP", "INFO",
                       f"Internal 200 HTML page linked from {int(reuse or 0) + 1} relationship(s) but not in the sitemap",
                       first_src, "ANCHOR", url, url, "", "", 200, None, url, 0, "")
            if info:
                _sm(info["sitemap"])["issues"] += 1

    # ---------------------------------------------------------------- 16 sitemap audit (+ sitemap issues)
    now_iso = _utc()
    lm: Dict[str, Dict[str, int]] = {}
    for page_norm, sm_url, lastmod in conn.execute(
            "SELECT normalized_url, sitemap_url, lastmod FROM on_page_sitemap_entries WHERE job_id = ?", J):
        st = lastmod_state(lastmod or "", now_iso)
        c = lm.setdefault(sm_url or "", {"MISSING": 0, "INVALID": 0, "FUTURE": 0, "": 0})
        c[st] += 1
        if st in ("INVALID", "FUTURE"):
            issues.add("SITEMAP", f"SITEMAP_LASTMOD_{st}", "LOW", f"<lastmod>{str(lastmod)[:40]}</lastmod>",
                       sm_url, "SITEMAP", page_norm, page_norm, "", "", None, None, "", None, "")
    sources = [dict(zip([d[0] for d in cur.description], row)) for cur in
               [conn.execute("""SELECT sitemap_url, parent_sitemap_url, depth, kind, status, http_status, classification,
                                       urls_listed, duplicates, rejected, size_bytes, selected, error_message,
                                       content_type, response_time_ms, child_sitemap_count
                                  FROM on_page_sitemap_sources WHERE job_id = ? ORDER BY depth, sitemap_url""", J)]
               for row in cur.fetchall()]
    known = {s_["sitemap_url"] for s_ in sources}
    for key in sorted(k for k in sm_agg if k not in known):     # URL-list audits: group by the input name
        sources.append({"sitemap_url": key, "parent_sitemap_url": "", "depth": 0, "kind": "URL_LIST" if key else "",
                        "status": "", "http_status": None, "classification": "", "urls_listed": sm_agg[key]["pages_audited"],
                        "duplicates": 0, "rejected": 0, "size_bytes": 0, "selected": 1, "error_message": "",
                        "content_type": "", "response_time_ms": None, "child_sitemap_count": 0})
    sm_header = ["sitemap_url", "parent_sitemap_url", "depth", "kind", "fetch_status", "http_status", "classification",
                 "urls_listed", "duplicates", "rejected", "size_bytes", "selected", "pages_audited", "pages_2xx",
                 "pages_redirected", "pages_4xx_5xx", "pages_failed_or_blocked", "pages_pending", "noindex_pages",
                 "non_canonical_pages", "lastmod_missing", "lastmod_invalid", "lastmod_future", "issues", "health",
                 "error_message", "content_type", "response_time_ms", "child_sitemap_count"]
    sm_rows = []
    for s_ in sources:
        url = s_["sitemap_url"]
        a = sm_agg.get(url) or _sm(url)
        c = lm.get(url, {"MISSING": 0, "INVALID": 0, "FUTURE": 0})
        failed = s_["status"] == "FAILED"
        sm_issues = []
        if failed:
            sm_issues.append(("SITEMAP_FETCH_FAILED", "HIGH", f"{s_['classification']}: {str(s_['error_message'])[:150]}"))
        if int(s_["urls_listed"] or 0) > 50000 or int(s_["size_bytes"] or 0) > 50 * 1024 * 1024:
            sm_issues.append(("SITEMAP_TOO_LARGE", "HIGH", f"{s_['urls_listed']} URLs, {s_['size_bytes']} bytes"))
        if int(s_["duplicates"] or 0):
            sm_issues.append(("SITEMAP_DUPLICATE_URLS", "LOW", f"{s_['duplicates']} duplicate URL entr(y/ies)"))
        if c["MISSING"]:
            sm_issues.append(("SITEMAP_LASTMOD_MISSING", "INFO", f"{c['MISSING']} URL(s) without <lastmod>"))
        for t, sev, detail in sm_issues:
            issues.add("SITEMAP", t, sev, detail, url, "SITEMAP", url, url, "", "", s_["http_status"], None, "", None, "")
        n_issues = a["issues"] + len(sm_issues) + c["INVALID"] + c["FUTURE"]
        problems = (a["pages_4xx_5xx"] + a["pages_failed_or_blocked"] + a["pages_redirected"] + a["noindex_pages"]
                    + a["non_canonical_pages"] + c["INVALID"] + c["FUTURE"] + len(sm_issues))
        health = "FAILED" if failed else ("ISSUES" if problems else "OK")
        sm_rows.append([url, s_["parent_sitemap_url"], s_["depth"], s_["kind"], s_["status"], s_["http_status"],
                        s_["classification"], s_["urls_listed"], s_["duplicates"], s_["rejected"], s_["size_bytes"],
                        s_["selected"], a["pages_audited"], a["pages_2xx"], a["pages_redirected"], a["pages_4xx_5xx"],
                        a["pages_failed_or_blocked"], a["pages_pending"], a["noindex_pages"], a["non_canonical_pages"],
                        c["MISSING"], c["INVALID"], c["FUTURE"], n_issues, health, s_["error_message"],
                        s_["content_type"], s_["response_time_ms"], s_["child_sitemap_count"]])
    with open(out("16_sitemap_audit.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(sm_header)
        w.writerows([[_safe(v) for v in row] for row in sm_rows])
    files["16_sitemap_audit.csv"] = len(sm_rows)
    sm_pages_total = sum(r_[12] for r_ in sm_rows)

    issues.close()
    files["12_issues.csv"] = issues.n
    n_types, summary_total, page_total = issues.write_summaries(out("13_issue_summary.csv"), out("14_page_issue_summary.csv"))
    files["13_issue_summary.csv"] = n_types
    files["14_page_issue_summary.csv"] = issues.pages_with_issues

    # ---------------------------------------------------------------- 17 input exceptions (nothing disappears silently)
    files["17_input_exceptions.csv"] = _stream(conn, out("17_input_exceptions.csv"), [
        "line_no", "input_url", "normalized_url", "reason", "duplicate_of",
    ], """SELECT line_no, input_url, normalized_url, reason, duplicate_of FROM on_page_input_exceptions
           WHERE job_id = ? ORDER BY line_no""", J)
    score = compute_score(conn, job_id, project_id)
    csv_seconds = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- independent DB counts (trust gate)
    q1 = lambda sql, a=J: conn.execute(sql, a).fetchone()[0]
    src = dict(conn.execute("SELECT crawl_state, COUNT(*) FROM on_page_source_pages WHERE job_id = ? GROUP BY crawl_state", J).fetchall())
    src_total = sum(src.values())
    vs = dict(conn.execute("SELECT validation_source, COUNT(*) FROM on_page_link_relationships WHERE job_id = ? GROUP BY validation_source", J).fetchall())
    scope = dict(conn.execute("SELECT found_url_scope, COUNT(*) FROM on_page_link_relationships WHERE job_id = ? GROUP BY found_url_scope", J).fetchall())
    rel_total = sum(vs.values())
    url_new, url_reused = vs.get("NEW_CHECK", 0), vs.get("REUSED", 0)
    url_not_requested = vs.get("NOT_REQUESTED", 0)
    http_rels = scope.get("INTERNAL", 0) + scope.get("EXTERNAL", 0)
    url_states = dict(conn.execute("SELECT validation_state, COUNT(*) FROM on_page_validation_cache WHERE job_id = ? AND referenced = 1 GROUP BY validation_state", J).fetchall())
    uniq_http = sum(url_states.values())
    ivs = dict(conn.execute("SELECT validation_source, COUNT(*) FROM on_page_image_relationships WHERE job_id = ? GROUP BY validation_source", J).fetchall())
    img_rel_total = sum(ivs.values())
    img_new, img_reused = ivs.get("NEW_CHECK", 0), ivs.get("REUSED", 0)
    img_http_rels = q1("SELECT COUNT(*) FROM on_page_image_relationships WHERE job_id = ? AND image_type IN ('HTTP_IMAGE','HTTPS_IMAGE')")
    img_states = dict(conn.execute("SELECT validation_state, COUNT(*) FROM on_page_image_validation_cache WHERE job_id = ? GROUP BY validation_state", J).fetchall())
    uniq_img = sum(img_states.values())
    url_occ = q1("SELECT COALESCE(SUM(raw_occurrences_count), 0) FROM on_page_source_pages WHERE job_id = ?")
    img_occ = q1("SELECT COALESCE(SUM(raw_image_occurrences_count), 0) FROM on_page_source_pages WHERE job_id = ?")
    orphan_url = q1(f"SELECT COUNT(*) {url_join} AND {http_url} AND r.validation_source != 'NOT_REQUESTED' AND c.normalized_url IS NULL")
    orphan_img = q1(f"SELECT COUNT(*) {img_join} AND i.validation_source IN ('NEW_CHECK','REUSED') AND ic.normalized_url IS NULL")
    missing_ends = q1("SELECT COUNT(*) FROM on_page_link_relationships WHERE job_id = ? AND (COALESCE(source_page_url,'') = '' OR COALESCE(normalized_found_url,'') = '')")
    missing_img_ends = q1("SELECT COUNT(*) FROM on_page_image_relationships WHERE job_id = ? AND (COALESCE(source_page_url,'') = '' OR COALESCE(normalized_image_url,'') = '')")
    rels_without_source_page = q1("SELECT COUNT(*) FROM on_page_link_relationships r WHERE r.job_id = ? AND NOT EXISTS (SELECT 1 FROM on_page_source_pages s WHERE s.job_id = r.job_id AND s.source_page_url = r.source_page_url)")
    broken_db = q1(f"SELECT COUNT(*) {url_join} AND {http_url} AND {BROKEN_SQL}")
    redirect_db = q1(f"SELECT COUNT(*) {url_join} AND {http_url} AND COALESCE(c.redirect_count, 0) > 0")
    external_db = scope.get("EXTERNAL", 0)
    broken_img_db = q1(f"SELECT COUNT(*) {img_join} AND i.validation_source IN ('NEW_CHECK','REUSED') AND {BROKEN_IMG_SQL}")
    redirect_img_rels = q1(f"SELECT COUNT(*) {img_join} AND i.validation_source IN ('NEW_CHECK','REUSED') AND COALESCE(ic.redirect_count, 0) > 0")
    job = conn.execute("SELECT * FROM on_page_audit_jobs WHERE job_id = ?", J).fetchone()
    job_cols = [d[0] for d in conn.execute("SELECT * FROM on_page_audit_jobs LIMIT 0").description]
    job_row = dict(zip(job_cols, job)) if job else {}
    integrity = conn.execute("PRAGMA integrity_check" if full_integrity_check else "PRAGMA quick_check").fetchone()[0]
    seo_db = q1("SELECT COUNT(*) FROM on_page_seo_pages WHERE job_id = ?")
    seo_expected = q1("""SELECT COUNT(*) FROM on_page_source_pages WHERE job_id = ? AND crawl_state = 'COMPLETED'
                          AND http_status BETWEEN 200 AND 299 AND COALESCE(html_bytes, 0) > 0
                          AND (COALESCE(content_type, '') = '' OR LOWER(content_type) LIKE '%html%')""") if seo_db else 0

    terminal_src = ("COMPLETED", "FAILED", "BLOCKED", "TIMED_OUT", "EXPLICITLY_EXCLUDED")
    input_total = job_row.get("input_urls_total")
    exceptions = files["17_input_exceptions.csv"]
    # manifest pre-check of every data file written so far (01/11/00 are validated after they are written)
    manifest: Dict[str, Dict[str, Any]] = {n: _validate_csv(out(n)) for n in sorted(files)}
    manifest_problems = sum(1 for n, m in manifest.items()
                            if m["rows"] != files[n] or m["bad_rows"] or not m["encoding_ok"])
    checks: List[Tuple[str, Any, Any]] = [
        ("every_source_page_has_terminal_state", src_total, sum(src.get(k, 0) for k in terminal_src)),
        ("every_http_url_has_terminal_validation_state", 0, url_states.get("PENDING", 0)),
        ("every_image_url_has_terminal_validation_state", 0, img_states.get("PENDING", 0)),
        ("url_http_relationships_eq_new_plus_reused_plus_not_requested", http_rels, url_new + url_reused + url_not_requested),
        ("unique_http_urls_eq_new_url_validations", uniq_http, url_new),
        ("unique_http_urls_eq_unique_url_validation_csv_rows", uniq_http, files["04_unique_url_validation.csv"]),
        ("image_http_relationships_eq_new_plus_reused", img_http_rels, img_new + img_reused),
        ("unique_image_urls_eq_new_image_validations", uniq_img, img_new),
        ("unique_image_urls_eq_unique_image_csv_rows", uniq_img, files["06_unique_image_validation.csv"]),
        ("every_relationship_has_source_and_destination", 0, missing_ends + missing_img_ends),
        ("every_relationship_source_is_a_source_page", 0, rels_without_source_page),
        ("every_reused_url_result_points_to_real_validation", 0, orphan_url),
        ("every_reused_image_result_points_to_real_validation", 0, orphan_img),
        ("url_csv_rows_eq_db_relationships", rel_total, files["03_url_audit.csv"]),
        ("image_csv_rows_eq_db_relationships", img_rel_total, files["05_image_audit.csv"]),
        ("source_csv_rows_eq_db_source_pages", src_total, files["02_source_page_audit.csv"]),
        ("broken_url_csv_rows_eq_db", broken_db, files["07_broken_urls.csv"]),
        ("redirect_csv_rows_eq_db", redirect_db, files["08_redirect_urls.csv"]),
        ("broken_image_csv_rows_eq_db", broken_img_db, files["09_broken_images.csv"]),
        ("external_csv_rows_eq_db", external_db, files["10_external_urls.csv"]),
        ("job_progress_row_eq_db_url_relationships", rel_total, job_row.get("url_relationships_persisted")),
        ("job_progress_row_eq_db_image_relationships", img_rel_total, job_row.get("image_relationships_persisted")),
        ("job_progress_row_eq_db_unique_images", uniq_img, job_row.get("unique_image_urls")),
        ("issue_rows_eq_issue_summary_total", issues.n, summary_total),
        ("issue_rows_eq_page_issue_summary_total", issues.n, page_total),
        ("broken_link_issues_eq_broken_url_csv_rows", files["07_broken_urls.csv"], issues.broken_link_rows),
        ("broken_image_issues_eq_broken_image_csv_rows", files["09_broken_images.csv"], issues.broken_image_rows),
        ("seo_csv_rows_eq_db_seo_pages", seo_db, files["15_seo_page_audit.csv"]),
        ("every_parsed_2xx_html_page_has_seo_row", seo_expected, seo_db),
        ("sitemap_csv_pages_eq_source_pages", src_total, sm_pages_total),
        ("source_pages_eq_completed_failed_blocked_timedout_excluded_pending", src_total,
         sum(src.get(k, 0) for k in terminal_src) + src.get("PENDING", 0)),
        ("input_urls_eq_source_pages_plus_input_exceptions",
         src_total + exceptions if input_total is None else input_total, src_total + exceptions),
        ("issue_csv_rows_eq_db_issues", issues.n, issues.csv_rows),
        ("csv_files_rows_columns_encoding_valid", 0, manifest_problems),
        ("database_integrity", "ok", integrity),
    ]
    recon_rows = [(name, exp, act, "PASS" if exp == act else "FAIL") for name, exp, act in checks]
    reconciliation_status = "PASS" if all(r[3] == "PASS" for r in recon_rows) else "FAIL"
    with open(out("11_reconciliation.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["check", "expected", "actual", "status"])
        w.writerows(recon_rows)
    files["11_reconciliation.csv"] = len(recon_rows)

    # ---------------------------------------------------------------- 01 summary
    perf = dict(performance or {})
    if not perf and job_row.get("live_metrics"):
        try:
            perf = json.loads(job_row["live_metrics"])
        except ValueError:
            perf = {}
    dur = perf.get("elapsed_sec")
    summary = [
        ("job_id", job_id), ("generated_at", _utc()),
        ("source_pages_total", src_total), ("source_pages_completed", src.get("COMPLETED", 0)),
        ("source_pages_failed", src.get("FAILED", 0)), ("source_pages_blocked", src.get("BLOCKED", 0)),
        ("source_pages_timed_out", src.get("TIMED_OUT", 0)), ("source_pages_excluded", src.get("EXPLICITLY_EXCLUDED", 0)),
        ("source_pages_pending", src.get("PENDING", 0)),
        ("total_urls_extracted", url_occ), ("total_url_relationships", rel_total), ("http_url_relationships", http_rels),
        ("non_http_fragment_invalid_url_relationships", rel_total - http_rels),
        ("unique_http_urls", uniq_http), ("new_url_validations", url_new), ("reused_url_validations", url_reused),
        ("url_destinations_satisfied_by_source_fetch", q1("SELECT COUNT(*) FROM on_page_validation_cache WHERE job_id = ? AND referenced = 1 AND seeded_by = 'SOURCE_FETCH'")),
        ("total_images_extracted", img_occ), ("total_image_relationships", img_rel_total),
        ("http_image_relationships", img_http_rels), ("non_http_invalid_image_relationships", img_rel_total - img_http_rels),
        ("unique_image_urls", uniq_img), ("new_image_validations", img_new), ("reused_image_validations", img_reused),
        ("broken_urls", broken_db), ("redirect_urls", redirect_db), ("external_urls", external_db),
        ("broken_images", broken_img_db), ("redirect_images", redirect_img_rels),
        ("issues_total", issues.n), ("issues_high", issues.by_sev["HIGH"]), ("issues_medium", issues.by_sev["MEDIUM"]),
        ("issues_low", issues.by_sev["LOW"]), ("issues_info", issues.by_sev["INFO"]),
        ("issue_types_found", n_types), ("source_pages_with_issues", issues.pages_with_issues),
        ("issue_duplicates_suppressed", issues.offered - issues.n),
        ("issues_reopened_regressions", issues.lifecycle["reopened"]), ("issues_verified_fixed", issues.lifecycle["verified"]),
        ("health_score", score["score"]), ("health_formula_version", score["formula_version"]),
        ("health_technical", score["technical"]), ("health_links", score["links"]), ("health_content", score["content"]),
        ("health_performance", score["performance"]), ("health_security", score["security"]),
        ("input_urls_total", input_total), ("input_exceptions", exceptions),
        ("seo_pages_analyzed", seo_db), ("sitemaps_reported", files["16_sitemap_audit.csv"]),
        ("crawl_duration_seconds", dur),
        ("source_pages_per_second", perf.get("source_pages_per_sec")),
        ("url_validation_rps", perf.get("url_validations_per_sec", perf.get("dest_validations_per_sec"))),
        ("image_validation_rps", perf.get("image_validations_per_sec")),
        ("total_request_rate", perf.get("total_requests_per_sec", perf.get("total_request_rate"))),
        ("primary_request_rate", perf.get("primary_requests_per_sec", perf.get("achieved_primary_rps"))),
        ("peak_memory_mb", perf.get("rss_peak_mb")),
        ("waf_events", perf.get("waf_events")), ("status_403", perf.get("status_403")),
        ("status_429", perf.get("status_429")), ("timeouts", perf.get("timeouts")),
        ("akamai_cooldowns", perf.get("akamai_cooldowns", perf.get("cooldowns"))),
        ("reconciliation_status", reconciliation_status),
        ("database_integrity_status", "PASS" if integrity == "ok" else f"FAIL ({integrity})"),
        ("final_qc_status", "PASS" if reconciliation_status == "PASS" else "FAILED"),
    ]
    with open(out("01_summary.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows([(k, "" if v is None else v) for k, v in summary])
    files["01_summary.csv"] = len(summary)
    conn.close()

    # ---------------------------------------------------------------- 00 manifest
    gen = _utc()
    for n in ("01_summary.csv", "11_reconciliation.csv"):
        manifest[n] = _validate_csv(out(n))
    man_rows = []
    for n in sorted(files):
        m = manifest[n]
        ok = m["rows"] == files[n] and not m["bad_rows"] and m["encoding_ok"]
        man_rows.append([n, DATASETS.get(n, ""), m["rows"], files[n], m["columns"], m["bytes"], m["sha256"], gen,
                         "OK" if ok else "INVALID"])
    with open(out("00_manifest.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filename", "dataset", "row_count", "expected_rows", "columns", "file_size", "sha256",
                    "generated_at", "status"])
        w.writerows(man_rows)
    files["00_manifest.csv"] = len(man_rows)

    # ---------------------------------------------------------------- ZIP (deflate, file-by-file) + verification
    t_zip = time.time()
    zip_path = os.path.join(output_dir, f"{package_name}.zip")
    names = sorted(files)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in names:
            z.write(out(name), arcname=name)
    zip_seconds = round(time.time() - t_zip, 2)
    import hashlib
    with zipfile.ZipFile(zip_path) as z:
        zip_ok = z.testzip() is None and sorted(z.namelist()) == names
        checksum_ok = True
        for name in names:
            if name == "00_manifest.csv":
                continue
            h = hashlib.sha256()
            with z.open(name) as zf:
                for chunk in iter(lambda: zf.read(1 << 20), b""):
                    h.update(chunk)
            checksum_ok = checksum_ok and h.hexdigest() == manifest[name]["sha256"]
    zip_ok = zip_ok and checksum_ok
    final = "PASS" if (reconciliation_status == "PASS" and zip_ok) else "FAILED"
    return {
        "manifest": [dict(zip(("filename", "dataset", "row_count", "expected_rows", "columns", "file_size", "sha256",
                               "generated_at", "status"), r)) for r in man_rows],
        "zip_checksums_verified": checksum_ok, "csv_seconds": csv_seconds, "zip_seconds": zip_seconds,
        "score": score, "issue_lifecycle": issues.lifecycle,
        "zip_path": zip_path, "zip_verified": zip_ok, "zip_bytes": os.path.getsize(zip_path),
        "csv_bytes": sum(os.path.getsize(out(n)) for n in names), "files": files, "work_dir": work,
        "reconciliation": [dict(zip(("check", "expected", "actual", "status"), r)) for r in recon_rows],
        "reconciliation_status": reconciliation_status, "database_integrity": integrity,
        "final_qc_status": final, "summary": dict(summary), "build_seconds": round(time.time() - t0, 2),
    }
