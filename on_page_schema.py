"""On-page image audit DDL — dependency-free so the API server can apply it without importing the
crawler (httpx/psutil/classifier stack)."""
import sqlite3


ON_PAGE_IMAGE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS on_page_image_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        source_page_url TEXT NOT NULL,
        source_page_status INTEGER,
        source_page_final_url TEXT,
        image_url TEXT NOT NULL,
        normalized_image_url TEXT NOT NULL,
        image_type TEXT NOT NULL,
        internal_external TEXT DEFAULT '',
        attribute_source TEXT DEFAULT '',
        alt_present INTEGER DEFAULT -1,
        alt_text TEXT DEFAULT '',
        width TEXT DEFAULT '',
        height TEXT DEFAULT '',
        loading TEXT DEFAULT '',
        occurrence_count INTEGER DEFAULT 1,
        representative_location TEXT DEFAULT '',
        business TEXT DEFAULT 'NEEDS_REVIEW',
        language TEXT DEFAULT 'UNKNOWN',
        page_type TEXT DEFAULT 'NEEDS_REVIEW',
        validation_source TEXT NOT NULL,
        validation_detail TEXT DEFAULT '',
        remark TEXT DEFAULT '',
        extracted_at TIMESTAMP,
        UNIQUE (job_id, source_page_url, normalized_image_url, alt_present, alt_text)
    );
    CREATE INDEX IF NOT EXISTS idx_opir_img ON on_page_image_relationships(job_id, normalized_image_url);
    CREATE INDEX IF NOT EXISTS idx_opir_valsrc ON on_page_image_relationships(job_id, validation_source);

    CREATE TABLE IF NOT EXISTS on_page_image_validation_cache (
        job_id TEXT NOT NULL,
        normalized_url TEXT NOT NULL,
        image_type TEXT DEFAULT '',
        internal_external TEXT DEFAULT '',
        validation_state TEXT DEFAULT 'PENDING',
        initial_http_status INTEGER,
        http_status INTEGER,
        final_url TEXT,
        redirect_count INTEGER DEFAULT 0,
        redirect_chain TEXT DEFAULT '[]',
        response_time_ms REAL DEFAULT 0.0,
        content_type TEXT DEFAULT '',
        content_length INTEGER,
        status_bucket TEXT DEFAULT 'PENDING',
        status_classification TEXT DEFAULT 'Pending',
        remark TEXT DEFAULT 'Pending Validation',
        image_issue TEXT DEFAULT '',
        error_type TEXT DEFAULT '',
        error_message TEXT DEFAULT '',
        robots_status TEXT DEFAULT 'ALLOWED',
        waf_status TEXT DEFAULT 'NONE',
        first_seen_source_url TEXT DEFAULT '',
        reuse_count INTEGER DEFAULT 0,
        checked_at TIMESTAMP,
        PRIMARY KEY (job_id, normalized_url)
    );
    CREATE INDEX IF NOT EXISTS idx_opivc_state ON on_page_image_validation_cache(job_id, validation_state);
"""

ON_PAGE_IMAGE_ALTERS = (
    "ALTER TABLE on_page_link_relationships ADD COLUMN validation_detail TEXT DEFAULT ''",
    "ALTER TABLE on_page_validation_cache ADD COLUMN seeded_by TEXT DEFAULT ''",
    "ALTER TABLE on_page_source_pages ADD COLUMN raw_image_occurrences_count INTEGER DEFAULT 0",
    "ALTER TABLE on_page_source_pages ADD COLUMN image_relationships_count INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN image_occurrences_extracted INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN image_relationships_persisted INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN unique_image_urls INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN unique_images_validated INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN unique_images_failed INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN unique_images_blocked INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN unique_images_timed_out INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN unique_images_pending INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN new_image_validations INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN reused_image_validations INTEGER DEFAULT 0",
    "ALTER TABLE on_page_audit_jobs ADD COLUMN live_metrics TEXT DEFAULT ''",
    # detailed per-page validation evidence (for the issue report)
    "ALTER TABLE on_page_source_pages ADD COLUMN page_title TEXT DEFAULT ''",
    "ALTER TABLE on_page_source_pages ADD COLUMN html_bytes INTEGER DEFAULT 0",
    "ALTER TABLE on_page_source_pages ADD COLUMN html_truncated INTEGER DEFAULT 0",
    "ALTER TABLE on_page_source_pages ADD COLUMN attempts INTEGER DEFAULT 0",
)


def ensure_on_page_image_schema(conn: sqlite3.Connection):
    """Additive, idempotent schema for the image audit (new tables + nullable/defaulted columns)."""
    conn.executescript(ON_PAGE_IMAGE_SCHEMA_SQL)
    for stmt in ON_PAGE_IMAGE_ALTERS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
