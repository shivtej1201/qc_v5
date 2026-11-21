"""Link and image facts in Parquet, queried with DuckDB (blueprint §4.1).

A 300K-page run has ~11M link and ~7.5M image relationships. They don't belong in Postgres: after the coverage
crawl they are streamed from the engine's SQLite into Parquet (100K-row row groups, zstd) under
<data>/runs/<run>/facts/{links,images}.parquet, joined with each unique URL's validation result, and the explorer
queries them with DuckDB (columnar scans; filters and paging push down into the file).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from qc6.settings import settings

BATCH = 100_000

LINKS_SQL = """
SELECT r.source_page_url, r.found_url, r.normalized_found_url AS url, r.found_url_type AS kind, r.found_url_scope AS scope,
       COALESCE(r.anchor_text, '') AS anchor_text, r.occurrence_count AS occurrences, COALESCE(r.representative_location, '') AS location,
       c.http_status, c.initial_http_status, COALESCE(c.final_url, '') AS final_url, COALESCE(c.redirect_count, 0) AS redirects,
       COALESCE(c.status_bucket, '') AS status_bucket, COALESCE(c.response_time_ms, 0) AS response_ms
  FROM on_page_link_relationships r
  LEFT JOIN on_page_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
 WHERE r.job_id = ? AND r.id > ? ORDER BY r.id LIMIT ?"""
IMAGES_SQL = """
SELECT r.source_page_url, r.image_url, r.normalized_image_url AS url, r.image_type AS kind, r.internal_external AS scope,
       r.alt_present, COALESCE(r.alt_text, '') AS alt_text, r.width, r.height, COALESCE(r.loading, '') AS loading,
       r.occurrence_count AS occurrences, c.http_status, COALESCE(c.status_bucket, '') AS status_bucket,
       COALESCE(c.content_type, '') AS content_type, c.content_length, COALESCE(c.image_issue, '') AS image_issue
  FROM on_page_image_relationships r
  LEFT JOIN on_page_image_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_image_url
 WHERE r.job_id = ? AND r.id > ? ORDER BY r.id LIMIT ?"""

LINK_SCHEMA = pa.schema([("source_page_url", pa.string()), ("found_url", pa.string()), ("url", pa.string()), ("kind", pa.string()),
                         ("scope", pa.string()), ("anchor_text", pa.string()), ("occurrences", pa.int32()), ("location", pa.string()),
                         ("http_status", pa.int32()), ("initial_http_status", pa.int32()), ("final_url", pa.string()),
                         ("redirects", pa.int32()), ("status_bucket", pa.string()), ("response_ms", pa.float64())])
IMAGE_SCHEMA = pa.schema([("source_page_url", pa.string()), ("image_url", pa.string()), ("url", pa.string()), ("kind", pa.string()),
                          ("scope", pa.string()), ("alt_present", pa.int32()), ("alt_text", pa.string()), ("width", pa.string()),
                          ("height", pa.string()), ("loading", pa.string()), ("occurrences", pa.int32()), ("http_status", pa.int32()),
                          ("status_bucket", pa.string()), ("content_type", pa.string()), ("content_length", pa.int64()),
                          ("image_issue", pa.string())])


def facts_dir(run_id: int) -> str:
    return os.path.join(settings.data_dir, "runs", str(run_id), "facts")


def _coerce(rows: List[Tuple], schema: pa.Schema) -> pa.Table:
    cols = list(zip(*rows)) if rows else [[] for _ in schema]
    arrays = []
    for f, col in zip(schema, cols):
        if pa.types.is_integer(f.type):
            col = [None if v in (None, "") else int(float(v)) for v in col]
        elif pa.types.is_floating(f.type):
            col = [None if v in (None, "") else float(v) for v in col]
        else:
            col = [None if v is None else str(v) for v in col]
        arrays.append(pa.array(col, type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def export(db_path: str, job: str, run_id: int) -> Dict[str, int]:
    """Stream relationships from the engine's SQLite into Parquet. Memory stays bounded (one 100K batch)."""
    out = facts_dir(run_id)
    os.makedirs(out, exist_ok=True)
    counts = {}
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for name, sql, schema in (("links", LINKS_SQL, LINK_SCHEMA), ("images", IMAGES_SQL, IMAGE_SCHEMA)):
            path = os.path.join(out, f"{name}.parquet")
            tmp = path + ".tmp"
            writer = pq.ParquetWriter(tmp, schema, compression="zstd")
            last, n = 0, 0
            try:
                id_sql = sql.replace("SELECT ", "SELECT r.id AS _id, ", 1)
                while True:
                    rows = c.execute(id_sql, (job, last, BATCH)).fetchall()
                    if not rows:
                        break
                    last = rows[-1][0]
                    writer.write_table(_coerce([r[1:] for r in rows], schema), row_group_size=BATCH)
                    n += len(rows)
            finally:
                writer.close()
            os.replace(tmp, path)
            counts[name] = n
    finally:
        c.close()
    return counts


# ------------------------------------------------------------------ queries (DuckDB)
_local = threading.local()

FILTERS = {
    "broken": "status_bucket IN ('BROKEN','CLIENT_ERROR','SERVER_ERROR','NOT_FOUND') OR http_status >= 400",
    "redirect": "redirects > 0 OR (http_status BETWEEN 300 AND 399)",
    "external": "scope = 'EXTERNAL'",
    "internal": "scope = 'INTERNAL'",
    "ok": "http_status BETWEEN 200 AND 299 AND redirects = 0",
    "missing_alt": "alt_present = 0",
}
IMAGE_FILTERS = {**FILTERS, "redirect": "http_status BETWEEN 300 AND 399",
                 "ok": "http_status BETWEEN 200 AND 299"}


def _filters(dataset: str) -> Dict[str, str]:
    return IMAGE_FILTERS if dataset == "images" else {k: v for k, v in FILTERS.items() if k != "missing_alt"}


SORTS = {"page": "source_page_url", "url": "url", "status": "http_status", "occurrences": "occurrences"}


def _db() -> duckdb.DuckDBPyConnection:
    con = getattr(_local, "con", None)
    if con is None:
        con = _local.con = duckdb.connect(":memory:")
        con.execute("SET threads TO 2")
    return con


def query(run_id: int, dataset: str, flt: str = "", q: str = "", page_url: str = "", sort: str = "page",
          desc: bool = False, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
    if dataset not in ("links", "images"):
        raise ValueError("dataset must be links or images")
    path = os.path.join(facts_dir(run_id), f"{dataset}.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError(dataset)
    where, args = ["true"], []
    if flt:
        if flt not in _filters(dataset):
            raise ValueError("unknown filter")
        where.append(f"({_filters(dataset)[flt]})")
    if q:
        where.append("(url ILIKE ? OR source_page_url ILIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if page_url:
        where.append("source_page_url = ?")
        args.append(page_url)
    order = SORTS.get(sort, "source_page_url") + (" DESC" if desc else "") + ", url"
    w = " AND ".join(where)
    con = _db()
    total = con.execute(f"SELECT COUNT(*) FROM read_parquet(?) WHERE {w}", [path, *args]).fetchone()[0]
    cur = con.execute(f"SELECT * FROM read_parquet(?) WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                      [path, *args, max(1, min(int(limit), 500)), max(0, int(offset))])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    summary = con.execute(f"""SELECT COUNT(*) AS n_rows, COUNT(DISTINCT url) AS n_unique,
                              COUNT(*) FILTER (WHERE {_filters(dataset)['broken']}) AS n_broken,
                              COUNT(*) FILTER (WHERE {_filters(dataset)['redirect']}) AS n_redirects FROM read_parquet(?)""", [path]).fetchone()
    return {"total": total, "rows": rows, "summary": dict(zip(("rows", "unique_urls", "broken", "redirects"), summary))}


def export_csv(run_id: int, dataset: str, dest: str, flt: str = "") -> int:
    """CSV straight from Parquet with DuckDB's COPY (fast, streams). Returns rows written."""
    if dataset not in ("links", "images"):
        raise ValueError("dataset must be links or images")
    path = os.path.join(facts_dir(run_id), f"{dataset}.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError(dataset)
    w = f"WHERE {_filters(dataset)[flt]}" if flt in _filters(dataset) else ""
    schema = LINK_SCHEMA if dataset == "links" else IMAGE_SCHEMA
    # Spreadsheet formula injection: a text cell starting with = + - @ tab or CR gets a leading apostrophe (as in V5).
    cols = ", ".join(
        "CASE WHEN regexp_matches(\"%s\", '^[=+@\\t\\r-]') THEN chr(39) || \"%s\" ELSE \"%s\" END AS \"%s\"" % ((f.name,) * 4)
        if pa.types.is_string(f.type) else '"%s"' % f.name for f in schema)
    src = path.replace("'", "''")
    dst = dest.replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"COPY (SELECT {cols} FROM (SELECT * FROM read_parquet('{src}') {w})) TO '{dst}' (HEADER, DELIMITER ',')")
        return con.execute(f"SELECT COUNT(*) FROM read_parquet('{path}') {w}").fetchone()[0]
    finally:
        con.close()
