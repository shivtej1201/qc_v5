"""
QC V5 on-page URL + image audit — streaming CSV report package (primary report format).

Every report is ONE streamed SQL query (cursor + fetchmany) written row-by-row with csv.writer, so
memory stays flat for 500K / 1M / 5M+ relationships; there are no per-row database lookups.
After writing, an independent trust gate re-counts everything from SQLite and compares it with
the CSV row counts. final_qc_status is PASS only if every check passes.

    01_summary.csv                 02_source_page_audit.csv        03_url_audit.csv
    04_unique_url_validation.csv   05_image_audit.csv              06_unique_image_validation.csv
    07_broken_urls.csv             08_redirect_urls.csv            09_broken_images.csv
    10_external_urls.csv           11_reconciliation.csv           -> <package_name>.zip
"""
import csv
import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from on_page_audit_engine import ensure_on_page_image_schema

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
            batch: int = 5000) -> int:
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
            n += len(rows)
        cur.close()
    return n


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

    # ---------------------------------------------------------------- 02 source pages
    files["02_source_page_audit.csv"] = _stream(conn, out("02_source_page_audit.csv"), [
        "source_page_id", "source_page_url", "normalized_source_url", "source_sitemap", "crawl_state",
        "initial_http_status", "http_status", "final_url", "redirect_count", "redirect_chain", "content_type",
        "response_time_ms", "robots_status", "waf_status", "business", "language", "page_type",
        "classification_method", "raw_url_occurrences", "url_relationships", "raw_image_occurrences",
        "image_relationships", "error_type", "error_message", "remark", "checked_at",
    ], """SELECT source_page_id, source_page_url, normalized_source_url, source_sitemap, crawl_state,
              initial_http_status, http_status, final_url, redirect_count, redirect_chain, content_type,
              response_time_ms, robots_status, waf_status, business, language, page_type, classification_method,
              raw_occurrences_count, relationships_count, raw_image_occurrences_count, image_relationships_count,
              error_type, error_message, remark, checked_at
         FROM on_page_source_pages WHERE job_id = ? ORDER BY source_page_id""", J)

    url_join = ("FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c "
                "ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url WHERE r.job_id = ?")

    # ---------------------------------------------------------------- 03 URL relationships
    files["03_url_audit.csv"] = _stream(conn, out("03_url_audit.csv"), [
        "source_page_url", "found_url", "normalized_url", "url_type", "internal_external", "element_type",
        "anchor_text", "occurrence_count", "http_status", "initial_status", "final_url", "redirect_chain",
        "status_bucket", "validation_state", "validation_source", "base_validation_source", "remarks", "checked_at",
    ], f"""SELECT r.source_page_url, r.found_url, r.normalized_found_url, r.url_syntax_type, r.found_url_scope,
              r.found_url_type, r.anchor_text, r.occurrence_count, c.http_status, c.initial_http_status, c.final_url,
              c.redirect_chain, c.status_bucket, c.validation_state, {URL_VALSRC_SQL}, r.validation_source,
              TRIM(COALESCE(r.remark, '') || CASE WHEN c.remark IS NOT NULL THEN ' | ' || c.remark ELSE '' END, ' |'),
              c.checked_at
         {url_join} ORDER BY r.id""", J)

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
    ], f"""SELECT i.source_page_url, i.image_url, i.normalized_image_url, i.image_type, i.attribute_source, {ALT_SQL},
              i.alt_text, i.width, i.height, i.loading, i.internal_external, i.occurrence_count, ic.http_status,
              ic.final_url, ic.content_type, ic.content_length, ic.validation_state, ic.image_issue, {IMG_VALSRC_SQL},
              TRIM(COALESCE(i.remark, '') || CASE WHEN ic.remark IS NOT NULL THEN ' | ' || ic.remark ELSE '' END, ' |'),
              ic.checked_at
         {img_join} ORDER BY i.id""", J)

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
    return {
        "zip_path": zip_path, "zip_verified": zip_ok, "zip_bytes": os.path.getsize(zip_path),
        "csv_bytes": sum(os.path.getsize(out(n)) for n in names), "files": files, "work_dir": work,
        "reconciliation": [dict(zip(("check", "expected", "actual", "status"), r)) for r in recon_rows],
        "reconciliation_status": reconciliation_status, "database_integrity": integrity,
        "final_qc_status": final, "summary": dict(summary), "build_seconds": round(time.time() - t0, 2),
    }
