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

    -- input lines that did not become a source page (invalid URL, exact or normalized duplicate)
    CREATE TABLE IF NOT EXISTS on_page_input_exceptions (
        job_id TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        input_url TEXT DEFAULT '',
        normalized_url TEXT DEFAULT '',
        reason TEXT NOT NULL,
        duplicate_of TEXT DEFAULT '',
        PRIMARY KEY (job_id, line_no)
    );

    -- SEO / technical-SEO facts: one row per parsed 2xx HTML sitemap page (seo_audit.page_facts)
    CREATE TABLE IF NOT EXISTS on_page_seo_pages (
        job_id TEXT NOT NULL,
        source_page_url TEXT NOT NULL,
        final_url TEXT,
        title TEXT DEFAULT '',
        title_length INTEGER DEFAULT 0,
        title_count INTEGER DEFAULT 0,
        meta_description TEXT DEFAULT '',
        meta_description_length INTEGER DEFAULT 0,
        meta_description_count INTEGER DEFAULT 0,
        h1_count INTEGER DEFAULT 0,
        h1_text TEXT DEFAULT '',
        h2_count INTEGER DEFAULT 0,
        heading_skips TEXT DEFAULT '',
        meta_robots TEXT DEFAULT '',
        x_robots_tag TEXT DEFAULT '',
        indexable INTEGER DEFAULT 1,
        noindex INTEGER DEFAULT 0,
        nofollow INTEGER DEFAULT 0,
        html_lang TEXT,
        has_viewport INTEGER DEFAULT 0,
        has_charset INTEGER DEFAULT 0,
        meta_refresh TEXT DEFAULT '',
        canonical_outside_head INTEGER DEFAULT 0,
        jsonld_blocks INTEGER DEFAULT 0,
        jsonld_invalid INTEGER DEFAULT 0,
        schema_types TEXT DEFAULT '',
        og_title TEXT DEFAULT '',
        og_description TEXT DEFAULT '',
        og_image TEXT DEFAULT '',
        twitter_card TEXT DEFAULT '',
        word_count INTEGER DEFAULT 0,
        content_hash TEXT DEFAULT '',
        hreflang_count INTEGER DEFAULT 0,
        hreflang_json TEXT DEFAULT '[]',
        hreflang_x_default INTEGER DEFAULT 0,
        insecure_resources INTEGER DEFAULT 0,
        insecure_examples TEXT DEFAULT '',
        internal_links INTEGER DEFAULT 0,
        external_links INTEGER DEFAULT 0,
        internal_nofollow_links INTEGER DEFAULT 0,
        url_length INTEGER DEFAULT 0,
        checked_at TIMESTAMP,
        script_count INTEGER DEFAULT 0,
        js_rendering_suspected INTEGER DEFAULT 0,
        PRIMARY KEY (job_id, source_page_url)
    );

    -- Sitemap provenance when an audit is built from sitemaps (sitemap_config): one row per page URL
    CREATE TABLE IF NOT EXISTS on_page_sitemap_entries (
        job_id TEXT NOT NULL,
        normalized_url TEXT NOT NULL,
        sitemap_url TEXT DEFAULT '',
        lastmod TEXT DEFAULT '',
        changefreq TEXT DEFAULT '',
        priority REAL,
        PRIMARY KEY (job_id, normalized_url)
    );
    CREATE INDEX IF NOT EXISTS idx_opse_sitemap ON on_page_sitemap_entries(job_id, sitemap_url);

    -- every sitemap file fetched for the audit (index and urlset), with fetch + hygiene stats
    CREATE TABLE IF NOT EXISTS on_page_sitemap_sources (
        job_id TEXT NOT NULL,
        sitemap_url TEXT NOT NULL,
        parent_sitemap_url TEXT DEFAULT '',
        depth INTEGER DEFAULT 0,
        kind TEXT DEFAULT '',
        status TEXT DEFAULT '',
        http_status INTEGER,
        classification TEXT DEFAULT '',
        urls_listed INTEGER DEFAULT 0,
        duplicates INTEGER DEFAULT 0,
        rejected INTEGER DEFAULT 0,
        size_bytes INTEGER DEFAULT 0,
        selected INTEGER DEFAULT 1,
        error_message TEXT DEFAULT '',
        PRIMARY KEY (job_id, sitemap_url)
    );
"""

ON_PAGE_IMAGE_ALTERS = (
    "ALTER TABLE on_page_audit_jobs ADD COLUMN input_urls_total INTEGER",
    "ALTER TABLE on_page_sitemap_sources ADD COLUMN content_type TEXT DEFAULT ''",
    "ALTER TABLE on_page_sitemap_sources ADD COLUMN response_time_ms REAL DEFAULT 0",
    "ALTER TABLE on_page_sitemap_sources ADD COLUMN child_sitemap_count INTEGER DEFAULT 0",
    "ALTER TABLE on_page_seo_pages ADD COLUMN script_count INTEGER DEFAULT 0",
    "ALTER TABLE on_page_seo_pages ADD COLUMN js_rendering_suspected INTEGER DEFAULT 0",
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
