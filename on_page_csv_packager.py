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
    13_issue_summary.csv           14_page_issue_summary.csv       -> <package_name>.zip

12-14 are the actionable layer: every source page / link / image problem with a severity,
plain-language detail and recommended fix, computed on the SAME streamed pass (no extra scan,
no extra HTTP requests).
"""
import csv
import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from on_page_issue_rules import (ACTIONS, BROKEN_IMAGE_TYPES, BROKEN_STATUS_TYPES, SEVERITY_ORDER,
                                  action_for, image_issues, link_issues, source_issues)
from on_page_schema import ensure_on_page_image_schema

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
                "hop_count", "detail", "recommended_action", "validation_source"]


class IssueWriter:
    """Streams 12_issues.csv and keeps only compact aggregates (per type / per page) in memory."""

    def __init__(self, path: str):
        self.f = open(path, "w", encoding="utf-8", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(ISSUE_HEADER)
        self.n = 0
        self.by_type: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.by_page: Dict[str, Dict[str, int]] = {}
        self.by_sev = {k: 0 for k in SEVERITY_ORDER}
        self.broken_link_rows = 0
        self.broken_image_rows = 0

    def add(self, category: str, itype: str, sev: str, detail: str, page: str, element: str, found: Any,
            norm: Any, text: Any, location: Any, status: Any, initial: Any, final: Any, hops: Any, vsrc: Any):
        self.n += 1
        self.w.writerow([_safe(v) for v in (self.n, sev, category, itype, page, element, found, norm, text, location,
                                            status, initial, final, hops, detail, action_for(itype, category), vsrc)])
        key = (sev, category, itype)
        agg = self.by_type.get(key)
        if agg is None:
            agg = self.by_type[key] = {"rows": 0, "urls": set(), "pages": set(), "ex_page": page, "ex_url": norm or found}
        agg["rows"] += 1
        if norm or found:
            agg["urls"].add(hash(norm or found))
        agg["pages"].add(hash(page))
        pg = self.by_page.get(page)
        if pg is None:
            pg = self.by_page[page] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0, "_types": {}}
        pg[sev] += 1
        pg["_types"][itype] = pg["_types"].get(itype, 0) + 1
        self.by_sev[sev] += 1
        if category in ("LINK", "CANONICAL", "HREFLANG", "IFRAME") and itype in BROKEN_STATUS_TYPES:
            self.broken_link_rows += 1
        if category == "IMAGE" and itype in BROKEN_IMAGE_TYPES:
            self.broken_image_rows += 1

    def close(self):
        self.f.close()

    def write_summaries(self, type_path: str, page_path: str) -> Tuple[int, int, int]:
        rows = sorted(self.by_type.items(), key=lambda kv: (SEVERITY_ORDER[kv[0][0]], -kv[1]["rows"], kv[0][2]))
        total = 0
        with open(type_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["severity", "category", "issue_type", "relationships", "unique_urls", "source_pages_affected",
                        "recommended_action", "example_source_page", "example_url"])
            for (sev, cat, t), a in rows:
                w.writerow([_safe(v) for v in (sev, cat, t, a["rows"], len(a["urls"]), len(a["pages"]),
                                               action_for(t, cat), a["ex_page"], a["ex_url"])])
                total += a["rows"]
        pages = sorted(self.by_page.items(), key=lambda kv: (-kv[1]["HIGH"], -kv[1]["MEDIUM"], -kv[1]["LOW"], kv[0]))
        page_total = 0
        with open(page_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source_page_url", "high", "medium", "low", "info", "total_issues", "top_issue_types"])
            for page, c in pages:
                t = c["HIGH"] + c["MEDIUM"] + c["LOW"] + c["INFO"]
                page_total += t
                top = sorted(c["_types"].items(), key=lambda kv: -kv[1])[:5]
                w.writerow([_safe(page), c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"], t,
                            "; ".join(f"{k} x{v}" for k, v in top)])
        return len(rows), total, page_total


def build_on_page_csv_package(
    db_path: str,
    job_id: str,
    output_dir: str,
    package_name: str = "QC_V5_10K_AUDIT",
    performance: Optional[Dict[str, Any]] = None,
    full_integrity_check: bool = True,
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
    issues = IssueWriter(out("12_issues.csv"))

    def on_source(r: Dict[str, Any]):
        for cat, t, sev, detail in source_issues(r, canon.get(r["source_page_url"]), anchors.get(r["source_page_url"], 0)):
            issues.add(cat, t, sev, detail, r["source_page_url"], "SOURCE_PAGE", r["source_page_url"],
                       r["normalized_source_url"], r.get("page_title"), "", r["http_status"],
                       r["initial_http_status"], r["final_url"], r["redirect_count"], "")

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

    # ---------------------------------------------------------------- 02 source pages
    files["02_source_page_audit.csv"] = _stream(conn, out("02_source_page_audit.csv"), [
        "source_page_id", "source_page_url", "normalized_source_url", "source_sitemap", "crawl_state",
        "initial_http_status", "http_status", "final_url", "redirect_count", "redirect_chain", "content_type",
        "response_time_ms", "robots_status", "waf_status", "business", "language", "page_type",
        "classification_method", "raw_url_occurrences", "url_relationships", "raw_image_occurrences",
        "image_relationships", "error_type", "error_message", "remark", "checked_at",
        "page_title", "html_bytes", "html_truncated", "attempts",
        "canonical_url", "robots_meta", "h1", "h1_count", "title_length", "meta_description_length", "word_count", "structured_data_types",
    ], """SELECT source_page_id, source_page_url, normalized_source_url, source_sitemap, crawl_state,
              initial_http_status, http_status, final_url, redirect_count, redirect_chain, content_type,
              response_time_ms, robots_status, waf_status, business, language, page_type, classification_method,
              raw_occurrences_count, relationships_count, raw_image_occurrences_count, image_relationships_count,
              error_type, error_message, remark, checked_at, page_title, html_bytes, html_truncated, attempts,
              COALESCE(canonical_url, ''), COALESCE(robots_meta, ''), COALESCE(h1, ''), COALESCE(h1_count, 0),
              COALESCE(title_length, 0), COALESCE(meta_description_length, 0), COALESCE(word_count, 0), COALESCE(structured_data_types, '')
         FROM on_page_source_pages WHERE job_id = ? ORDER BY source_page_id""", J, on_row=on_source)

    url_join = ("FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c "
                "ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url WHERE r.job_id = ?")

    # ---------------------------------------------------------------- 03 URL relationships
    files["03_url_audit.csv"] = _stream(conn, out("03_url_audit.csv"), [
        "source_page_url", "found_url", "normalized_url", "url_type", "internal_external", "element_type",
        "anchor_text", "occurrence_count", "http_status", "initial_status", "final_url", "redirect_chain",
        "status_bucket", "validation_state", "validation_source", "base_validation_source", "remarks", "checked_at",
        "location", "hop_count", "error_type", "error_message", "status_classification", "response_time_ms",
    ], f"""SELECT r.source_page_url, r.found_url, r.normalized_found_url, r.url_syntax_type, r.found_url_scope,
              r.found_url_type, r.anchor_text, r.occurrence_count, c.http_status, c.initial_http_status, c.final_url,
              c.redirect_chain, c.status_bucket, c.validation_state, {URL_VALSRC_SQL}, r.validation_source,
              TRIM(COALESCE(r.remark, '') || CASE WHEN c.remark IS NOT NULL THEN ' | ' || c.remark ELSE '' END, ' |'),
              c.checked_at, r.representative_location, c.redirect_count, c.error_type, c.error_message,
              c.status_classification, c.response_time_ms
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
    ], f"""SELECT i.source_page_url, i.image_url, i.normalized_image_url, i.image_type, i.attribute_source, {ALT_SQL},
              i.alt_text, i.width, i.height, i.loading, i.internal_external, i.occurrence_count, ic.http_status,
              ic.final_url, ic.content_type, ic.content_length, ic.validation_state, ic.image_issue, {IMG_VALSRC_SQL},
              TRIM(COALESCE(i.remark, '') || CASE WHEN ic.remark IS NOT NULL THEN ' | ' || ic.remark ELSE '' END, ' |'),
              ic.checked_at, i.representative_location, ic.initial_http_status, ic.redirect_count, ic.status_bucket,
              ic.error_type, ic.error_message, ic.status_classification
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

    issues.close()
    files["12_issues.csv"] = issues.n
    n_types, summary_total, page_total = issues.write_summaries(out("13_issue_summary.csv"), out("14_page_issue_summary.csv"))
    files["13_issue_summary.csv"] = n_types
    files["14_page_issue_summary.csv"] = len(issues.by_page)

    # ---------------------------------------------------------------- independent DB counts (trust gate)
    q1 = lambda sql, a=J: conn.execute(sql, a).fetchone()[0]
    src = dict(conn.execute("SELECT crawl_state, COUNT(*) FROM on_page_source_pages WHERE job_id = ? GROUP BY crawl_state", J).fetchall())
    src_total = sum(src.values())
    vs = dict(conn.execute("SELECT validation_source, COUNT(*) FROM on_page_link_relationships WHERE job_id = ? GROUP BY validation_source", J).fetchall())
    scope = dict(conn.execute("SELECT found_url_scope, COUNT(*) FROM on_page_link_relationships WHERE job_id = ? GROUP BY found_url_scope", J).fetchall())
    rel_total = sum(vs.values())
    url_new, url_reused = vs.get("NEW_CHECK", 0), vs.get("REUSED", 0)
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
    orphan_url = q1(f"SELECT COUNT(*) {url_join} AND {http_url} AND c.normalized_url IS NULL")
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

    terminal_src = ("COMPLETED", "FAILED", "BLOCKED", "TIMED_OUT", "EXPLICITLY_EXCLUDED")
    checks: List[Tuple[str, Any, Any]] = [
        ("every_source_page_has_terminal_state", src_total, sum(src.get(k, 0) for k in terminal_src)),
        ("every_http_url_has_terminal_validation_state", 0, url_states.get("PENDING", 0)),
        ("every_image_url_has_terminal_validation_state", 0, img_states.get("PENDING", 0)),
        ("url_http_relationships_eq_new_plus_reused", http_rels, url_new + url_reused),
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
        ("issue_types_found", n_types), ("source_pages_with_issues", len(issues.by_page)),
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

    # ---------------------------------------------------------------- ZIP (deflate) + verification
    zip_path = os.path.join(output_dir, f"{package_name}.zip")
    names = sorted(files)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in names:
            z.write(out(name), arcname=name)
    with zipfile.ZipFile(zip_path) as z:
        zip_ok = z.testzip() is None and sorted(z.namelist()) == names
    final = "PASS" if (reconciliation_status == "PASS" and zip_ok) else "FAILED"

    # ---------------------------------------------------------------- Report Manifest (Section 22)
    dataset_mapping = {
        "01_summary.csv": ("Summary", "Executive Metrics & KPIs"),
        "02_source_page_audit.csv": ("SourcePages", "Source Page Audit with SEO Signals"),
        "03_url_audit.csv": ("Links", "Discovered Link Relationships"),
        "04_unique_url_validation.csv": ("UniqueLinks", "Unique Destination URL Validations"),
        "05_image_audit.csv": ("Images", "Image Placements & SEO Attributes"),
        "06_unique_image_validation.csv": ("UniqueImages", "Unique Image Asset Validations"),
        "07_broken_urls.csv": ("BrokenLinks", "404 and Broken Destination Endpoints"),
        "08_redirect_urls.csv": ("Redirects", "3xx Redirect Hops and Chains"),
        "09_broken_images.csv": ("BrokenImages", "Missing or Corrupted Image Assets"),
        "10_external_urls.csv": ("ExternalLinks", "Outbound Third-Party URLs"),
        "11_reconciliation.csv": ("TrustGate", "28-Point Mathematical Reconciliation"),
        "12_issues.csv": ("Issues", "Granular Issue Records with Remediation Actions"),
        "13_issue_summary.csv": ("IssueRollup", "Issue Summary Grouped by Issue Type"),
        "14_page_issue_summary.csv": ("PageIssues", "Source Page Issue Density Rankings"),
    }
    manifest_entries = []
    gen_time = _utc()
    for fname in names:
        fpath = out(fname)
        dset, rtitle = dataset_mapping.get(fname, ("General", fname))
        fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0
        manifest_entries.append({
            "report_name": rtitle,
            "dataset": dset,
            "filename": fname,
            "row_count": files.get(fname, 0),
            "file_size": fsize,
            "generated_at": gen_time,
            "status": "READY" if os.path.exists(fpath) else "MISSING",
        })
    manifest_doc = {
        "job_id": job_id,
        "package_name": package_name,
        "generated_at": gen_time,
        "total_files": len(manifest_entries),
        "manifest": manifest_entries,
        "files": manifest_entries,
    }
    try:
        with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as mf:
            json.dump(manifest_doc, mf, indent=2)
        with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as mf2:
            json.dump(manifest_doc, mf2, indent=2)
    except Exception:
        pass

    return {
        "zip_path": zip_path, "zip_verified": zip_ok, "zip_bytes": os.path.getsize(zip_path),
        "csv_bytes": sum(os.path.getsize(out(n)) for n in names), "files": files, "work_dir": work,
        "manifest": manifest_entries, "manifest_doc": manifest_doc,
        "reconciliation": [dict(zip(("check", "expected", "actual", "status"), r)) for r in recon_rows],
        "reconciliation_status": reconciliation_status, "database_integrity": integrity,
        "final_qc_status": final, "summary": dict(summary), "build_seconds": round(time.time() - t0, 2),
    }
