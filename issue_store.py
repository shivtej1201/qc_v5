"""
QC V5 — persisted issues, issue lifecycle and the deterministic health score.

Dependency-free (sqlite3 only) so the worker, the packager and the API share it.

* on_page_issues: one row per (job, issue_key). issue_key = hash(page_url | rule_code | subject_url), so
  re-packaging or re-processing a job can never create duplicates (UNIQUE + INSERT OR IGNORE). Recommendation
  and title are derived from rule_code at read time (no repeated text stored per row).
* on_page_issue_status: lifecycle per PROJECT (primary domain) and issue_key, so a status survives re-audits:
  OPEN -> IN_PROGRESS -> VERIFIED -> RESOLVED (+ DISMISSED). Rows exist only once a status was set or changed.
  After a COMPLETE audit: an OPEN/IN_PROGRESS issue whose page was audited and that is no longer detected becomes
  VERIFIED (fix confirmed by the crawl); a VERIFIED/RESOLVED issue that is detected again is re-OPENED (regression).
  Only audits OTHER than the one the status was last set/changed in can move it (rebuilding a package is a no-op).
* on_page_scores: SEO health score, formula HEALTH_FORMULA_VERSION (weights below), stored per job.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

ISSUE_STATUSES = ("OPEN", "IN_PROGRESS", "VERIFIED", "RESOLVED", "DISMISSED")
SEVERITY_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2, "INFO": 0.0}
HEALTH_FORMULA_VERSION = "qcv5-health-1.0"
DIMENSION_WEIGHTS = {"technical": 0.35, "links": 0.25, "content": 0.20, "performance": 0.10, "security": 0.10}

ISSUE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS on_page_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    issue_key TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    dimension TEXT NOT NULL,
    page_url TEXT NOT NULL,
    element_type TEXT DEFAULT '',
    found_url TEXT DEFAULT '',
    subject_url TEXT DEFAULT '',
    anchor_or_alt_text TEXT DEFAULT '',
    location TEXT DEFAULT '',
    http_status INTEGER,
    initial_status INTEGER,
    final_url TEXT DEFAULT '',
    hop_count INTEGER,
    detail TEXT DEFAULT '',
    validation_source TEXT DEFAULT '',
    UNIQUE (job_id, issue_key)
);
CREATE INDEX IF NOT EXISTS idx_opi_sev ON on_page_issues(job_id, severity);
CREATE INDEX IF NOT EXISTS idx_opi_rule ON on_page_issues(job_id, rule_code);
CREATE INDEX IF NOT EXISTS idx_opi_cat ON on_page_issues(job_id, category);
CREATE INDEX IF NOT EXISTS idx_opi_page ON on_page_issues(job_id, page_url);
CREATE INDEX IF NOT EXISTS idx_opi_key ON on_page_issues(issue_key);

CREATE TABLE IF NOT EXISTS on_page_issue_status (
    project_id TEXT NOT NULL,
    issue_key TEXT NOT NULL,
    status TEXT NOT NULL,
    page_url TEXT DEFAULT '',
    rule_code TEXT DEFAULT '',
    note TEXT DEFAULT '',
    last_job_id TEXT DEFAULT '',
    updated_at TIMESTAMP,
    PRIMARY KEY (project_id, issue_key)
);

CREATE TABLE IF NOT EXISTS on_page_scores (
    job_id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    score REAL,
    technical REAL, links REAL, content REAL, performance REAL, security REAL,
    pages_scored INTEGER DEFAULT 0,
    formula_version TEXT NOT NULL,
    weights TEXT NOT NULL,
    calculated_at TIMESTAMP
);
"""

ISSUE_COLUMNS = ["job_id", "issue_key", "rule_code", "category", "severity", "dimension", "page_url", "element_type",
                 "found_url", "subject_url", "anchor_or_alt_text", "location", "http_status", "initial_status",
                 "final_url", "hop_count", "detail", "validation_source"]

# rule -> score dimension (deterministic; category decides unless the rule is listed explicitly)
_SECURITY_RULES = {"MIXED_CONTENT", "INSECURE_HTTP_LINK", "UNSAFE_DESTINATION", "SSL_ERROR", "IMAGE_SSL_ERROR",
                   "SOURCE_UNSAFE", "IMAGE_UNSAFE_DESTINATION", "MIXED_CONTENT_IMAGE"}
_PERFORMANCE_RULES = {"SLOW_RESPONSE", "PAGE_TRUNCATED", "LARGE_IMAGE", "JS_RENDERING_SUSPECTED",
                      "REDIRECT_CHAIN", "IMAGE_REDIRECT_CHAIN"}
_CONTENT_RULES = {"MISSING_ALT", "EMPTY_ALT", "MISSING_TITLE", "SOFT_404"}
_LINK_CATEGORIES = {"LINK", "IFRAME", "RESOURCE", "METADATA_URL", "FORM", "IMAGE"}
_CONTENT_CATEGORIES = {"ON_PAGE_SEO", "CONTENT"}


def dimension_for(category: str, rule: str) -> str:
    if rule in _SECURITY_RULES:
        return "security"
    if rule in _PERFORMANCE_RULES:
        return "performance"
    if rule in _CONTENT_RULES or category in _CONTENT_CATEGORIES:
        return "content"
    if category in _LINK_CATEGORIES:
        return "links"
    return "technical"    # SOURCE_PAGE, TECH_SEO, CANONICAL, HREFLANG, INTERNATIONAL, URL_STRUCTURE, SITEMAP


def issue_key(page_url: str, rule: str, subject: str) -> str:
    return hashlib.blake2b(f"{page_url}\x1f{rule}\x1f{subject}".encode("utf-8", "ignore"), digest_size=10).hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_issue_schema(conn: sqlite3.Connection):
    conn.executescript(ISSUE_SCHEMA_SQL)
    conn.commit()


class IssueSink:
    """Batched INSERT OR IGNORE of issues for one job (dedup by issue_key). Re-running replaces the job's rows.

    Rows are first spooled into a TEMP table (temp store, outside the main database and its WAL) and moved into
    on_page_issues in ONE transaction by publish()/stored(). The packager adds issues while it streams CSV rows from
    a long-running SELECT on the same connection; that open read stops SQLite from resetting the WAL, so committing
    straight into the indexed table every batch appended fresh copies of the same index pages each time (300K pages /
    1.6M issues: the WAL reached 9.6 GB in under two minutes and filled the disk)."""

    _SPOOL = "temp.qc_issue_spool"

    def __init__(self, conn: sqlite3.Connection, job_id: str, batch: int = 5000):
        self.conn, self.job_id, self.batch = conn, job_id, batch
        self.buf: List[Tuple] = []
        self.offered = 0
        ensure_issue_schema(conn)
        with conn:
            conn.execute("DELETE FROM on_page_issues WHERE job_id = ?", (job_id,))
            conn.execute(f"DROP TABLE IF EXISTS {self._SPOOL}")
            conn.execute(f"CREATE TEMP TABLE {self._SPOOL.split('.')[1]} ({', '.join(ISSUE_COLUMNS)})")

    def add(self, category: str, rule: str, sev: str, detail: str, page: str, element: Any, found: Any, norm: Any,
            text: Any, location: Any, status: Any, initial: Any, final: Any, hops: Any, vsrc: Any):
        self.offered += 1
        subject = str(norm or found or "")
        self.buf.append((self.job_id, issue_key(page or "", rule, subject), rule, category, sev,
                         dimension_for(category, rule), page or "", element or "", str(found or "")[:2000],
                         subject[:2000], str(text or "")[:300], str(location or "")[:120], _int(status), _int(initial),
                         str(final or "")[:2000], _int(hops), str(detail or "")[:600], str(vsrc or "")))
        if len(self.buf) >= self.batch:
            self.flush()

    def flush(self):
        """Move buffered rows to the temp spool (no main-database write)."""
        if self.buf:
            with self.conn:
                self.conn.executemany(
                    f"INSERT INTO {self._SPOOL} ({', '.join(ISSUE_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(ISSUE_COLUMNS))})", self.buf)
            self.buf = []

    def publish(self):
        """Copy the spool into on_page_issues in one transaction (first row per issue_key wins, in arrival order).
        Call when no long-running read is open on the connection."""
        self.flush()
        cols = ", ".join(ISSUE_COLUMNS)
        with self.conn:
            self.conn.execute(f"INSERT OR IGNORE INTO on_page_issues ({cols}) SELECT {cols} FROM {self._SPOOL} ORDER BY rowid")
            self.conn.execute(f"DELETE FROM {self._SPOOL}")

    def stored(self) -> int:
        self.publish()
        return self.conn.execute("SELECT COUNT(*) FROM on_page_issues WHERE job_id = ?", (self.job_id,)).fetchone()[0]


def _int(v: Any) -> Optional[int]:
    try:
        return None if v in (None, "") else int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ lifecycle
def apply_lifecycle(conn: sqlite3.Connection, job_id: str, project_id: str, complete: bool) -> Dict[str, int]:
    """Carry statuses across audits of the same project (see module docstring). Returns counts changed."""
    ensure_issue_schema(conn)
    now = _utc()
    with conn:
        reopened = conn.execute(
            """UPDATE on_page_issue_status SET status = 'OPEN', note = 'Regression: detected again in ' || ?,
                      last_job_id = ?, updated_at = ?
                WHERE project_id = ? AND status IN ('VERIFIED', 'RESOLVED') AND last_job_id != ?
                  AND issue_key IN (SELECT issue_key FROM on_page_issues WHERE job_id = ?)""",
            (job_id, job_id, now, project_id, job_id, job_id)).rowcount
        verified = 0
        if complete:
            verified = conn.execute(
                """UPDATE on_page_issue_status SET status = 'VERIFIED', note = 'Not detected in ' || ?,
                          last_job_id = ?, updated_at = ?
                    WHERE project_id = ? AND status IN ('OPEN', 'IN_PROGRESS') AND last_job_id != ?
                      AND issue_key NOT IN (SELECT issue_key FROM on_page_issues WHERE job_id = ?)
                      AND page_url IN (SELECT source_page_url FROM on_page_source_pages
                                        WHERE job_id = ? AND crawl_state = 'COMPLETED')""",
                (job_id, job_id, now, project_id, job_id, job_id, job_id)).rowcount
    return {"reopened": reopened, "verified": verified}


def set_status(conn: sqlite3.Connection, job_id: str, project_id: str, key: str, status: str, note: str = "") -> Dict[str, Any]:
    status = (status or "").strip().upper()
    if status not in ISSUE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(ISSUE_STATUSES)}")
    ensure_issue_schema(conn)
    row = conn.execute("SELECT page_url, rule_code FROM on_page_issues WHERE job_id = ? AND issue_key = ?",
                       (job_id, key)).fetchone()
    if row is None:
        raise KeyError(key)
    with conn:
        conn.execute(
            """INSERT INTO on_page_issue_status (project_id, issue_key, status, page_url, rule_code, note, last_job_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, issue_key) DO UPDATE SET status = excluded.status, note = excluded.note,
                   last_job_id = excluded.last_job_id, updated_at = excluded.updated_at""",
            (project_id, key, status, row[0], row[1], (note or "")[:500], job_id, _utc()))
    return {"issue_key": key, "status": status, "note": note}


# ------------------------------------------------------------------ health score
def compute_score(conn: sqlite3.Connection, job_id: str, project_id: str = "") -> Dict[str, Any]:
    """Deterministic: for each dimension, every audited page gets a penalty = min(1, sum of severity weights of its
    issues in that dimension); dimension score = 100 * (1 - mean penalty); overall = weighted sum (35/25/20/10/10).
    Same database rows -> same score, independent of order or timing."""
    ensure_issue_schema(conn)
    pages = conn.execute("""SELECT COUNT(*) FROM on_page_source_pages WHERE job_id = ? AND crawl_state != 'PENDING'""",
                         (job_id,)).fetchone()[0]
    dims = {d: 100.0 for d in DIMENSION_WEIGHTS}
    if pages:
        case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in SEVERITY_WEIGHT.items())
        for dim, total in conn.execute(
                f"""SELECT dimension, SUM(MIN(1.0, w)) FROM (
                        SELECT i.dimension, i.page_url, SUM(CASE i.severity {case} ELSE 0 END) AS w
                          FROM on_page_issues i
                          JOIN on_page_source_pages s ON s.job_id = i.job_id AND s.source_page_url = i.page_url
                         WHERE i.job_id = ? GROUP BY i.dimension, i.page_url)
                    GROUP BY dimension""", (job_id,)):
            if dim in dims:
                dims[dim] = round(100.0 * (1.0 - (total or 0.0) / pages), 2)
    score = round(sum(dims[d] * w for d, w in DIMENSION_WEIGHTS.items()), 2)
    rec = {"job_id": job_id, "project_id": project_id, "score": score, **dims, "pages_scored": pages,
           "formula_version": HEALTH_FORMULA_VERSION, "weights": json.dumps(DIMENSION_WEIGHTS, sort_keys=True),
           "calculated_at": _utc()}
    with conn:
        conn.execute(f"INSERT OR REPLACE INTO on_page_scores ({', '.join(rec)}) VALUES ({', '.join('?' * len(rec))})",
                     tuple(rec.values()))
    rec["weights"] = DIMENSION_WEIGHTS
    return rec


def get_score(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    ensure_issue_schema(conn)
    cur = conn.execute("SELECT * FROM on_page_scores WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        return None
    rec = dict(zip([d[0] for d in cur.description], row))
    rec["weights"] = json.loads(rec["weights"])
    return rec


def has_issues_table_rows(conn: sqlite3.Connection, job_id: str) -> bool:
    ensure_issue_schema(conn)
    return conn.execute("SELECT 1 FROM on_page_issues WHERE job_id = ? LIMIT 1", (job_id,)).fetchone() is not None
