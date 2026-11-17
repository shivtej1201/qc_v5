import aiosqlite
import aiosqlite.core
import asyncio
import json
import time
import random
from config import settings
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from db_metrics import db_metrics

# Ensure background SQLite worker threads are daemon threads so test runners
# and subprocesses exit cleanly without hanging on unclosed or cached connections.
_orig_conn_init = aiosqlite.core.Connection.__init__
def _daemon_conn_init(self, *args, **kwargs):
    _orig_conn_init(self, *args, **kwargs)
    self._thread.daemon = True
aiosqlite.core.Connection.__init__ = _daemon_conn_init

class Database:
    def __init__(self, db_path: str = settings.DB_FILE):
        self.db_path = db_path
        self.conn = None
        self._link_stats_cache = {}
        
    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path, isolation_level="IMMEDIATE")
        # Production WAL & Performance Optimizations
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA synchronous=NORMAL;")
        await self.conn.execute(f"PRAGMA busy_timeout={settings.DB_BUSY_TIMEOUT};")
        await self.conn.execute("PRAGMA cache_size=-262144;")
        await self.conn.execute("PRAGMA mmap_size=268435456;")
        await self.conn.execute("PRAGMA temp_store=MEMORY;")
        await self.conn.execute("PRAGMA wal_autocheckpoint=50000;")

        
        await self.conn.execute("""
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
            )
        """)
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS image_results (
                normalized_url TEXT PRIMARY KEY,
                initial_status_code INTEGER,
                final_status_code INTEGER,
                status TEXT,
                image_format TEXT,
                image_size_kb REAL,
                is_dam_asset BOOLEAN,
                source_platform TEXT,
                error_type TEXT,
                error_message TEXT,
                checked_at TIMESTAMP
            )
        """)
        
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_checked_at ON url_results(checked_at)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_img_checked_at ON image_results(checked_at)")
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY,
                filename TEXT,
                mode TEXT,
                status TEXT,
                created_at TIMESTAMP
            )
        """)
        
        await self.conn.execute("""
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
                updated_at TIMESTAMP
            )
        """)
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                batch_index INTEGER,
                input_file TEXT,
                total_records INTEGER DEFAULT 0,
                processed_records INTEGER DEFAULT 0,
                status TEXT DEFAULT 'QUEUED',
                worker_id TEXT,
                lease_expires_at TIMESTAMP,
                error TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS extracted_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                batch_id TEXT,
                source_url TEXT,
                original_url TEXT,
                normalized_url TEXT,
                anchor_text TEXT,
                component_name TEXT,
                image_name TEXT,
                loading_type TEXT,
                extracted_timestamp TEXT
            )
        """)
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS source_classifications (
                source_url TEXT PRIMARY KEY,
                page_type TEXT,
                business TEXT,
                classification_method TEXT,
                tag_1 TEXT,
                tag_2 TEXT,
                diagnostic_notes TEXT,
                classified_at TEXT
            )
        """)
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS page_audit_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                batch_id TEXT,
                page_url TEXT,
                final_url TEXT,
                page_status TEXT,
                http_status INTEGER,
                response_time_ms REAL,
                error_message TEXT,
                screenshot_path TEXT,
                api_count INTEGER DEFAULT 0,
                failed_api_count INTEGER DEFAULT 0,
                summary_reason TEXT,
                evaluated_at TIMESTAMP
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_page_audit_job ON page_audit_results(job_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_page_audit_url ON page_audit_results(page_url)")
        await self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_page_audit_job_url ON page_audit_results(job_id, page_url)")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS page_api_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                page_url TEXT,
                request_url TEXT,
                method TEXT,
                resource_type TEXT,
                http_status INTEGER,
                api_status TEXT,
                response_time_ms REAL,
                content_type TEXT,
                error_message TEXT,
                response_snippet TEXT,
                observed_at TIMESTAMP
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_api_responses_job ON page_api_responses(job_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_api_responses_page ON page_api_responses(page_url)")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS job_destinations (
                job_id TEXT,
                normalized_url TEXT,
                initial_status_code INTEGER,
                final_status_code INTEGER,
                status TEXT,
                final_url TEXT,
                redirect_count INTEGER DEFAULT 0,
                redirect_chain TEXT,
                response_time_ms REAL,
                content_type TEXT,
                content_length INTEGER,
                error_type TEXT,
                error_message TEXT,
                check_method TEXT,
                browser_verified TEXT,
                checked_at TIMESTAMP,
                retries_count INTEGER DEFAULT 0,
                PRIMARY KEY (job_id, normalized_url)
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_job_dest_job ON job_destinations(job_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_job_dest_status ON job_destinations(job_id, final_status_code)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_extracted_job_norm ON extracted_links(job_id, normalized_url)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_extracted_job_source ON extracted_links(job_id, source_url)")
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS remediations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                remediation_id TEXT NOT NULL,
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                priority TEXT NOT NULL,
                priority_score INTEGER NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                review_notes TEXT,
                target_url TEXT NOT NULL,
                source_urls TEXT,
                source_count INTEGER DEFAULT 1,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                impact TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                priority_rationale TEXT NOT NULL,
                technical_details TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                UNIQUE (job_id, category, target_url)
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_remediations_job ON remediations(job_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_remediations_job_prio ON remediations(job_id, priority)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_remediations_job_status ON remediations(job_id, status)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_remediations_job_cat ON remediations(job_id, category)")
        await self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_remediations_job_rem_id ON remediations(job_id, remediation_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_remediations_job_score ON remediations(job_id, priority_score DESC)")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS remediation_explanations_cache (
                cache_key TEXT PRIMARY KEY,
                explanation_json TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                created_at TIMESTAMP
            )
        """)

        # Phase 10.12 Sitemap & 403 Diagnostics Tables
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sitemap_imports (
                id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT DEFAULT 'INITIALIZING',
                total_sitemaps INTEGER DEFAULT 0,
                processed_sitemaps INTEGER DEFAULT 0,
                total_urls INTEGER DEFAULT 0,
                unique_urls INTEGER DEFAULT 0,
                duplicate_urls INTEGER DEFAULT 0,
                rejected_urls INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                crawl_job_id TEXT,
                error_message TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sitemap_imports_created ON sitemap_imports(created_at)")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sitemap_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id TEXT NOT NULL,
                sitemap_url TEXT NOT NULL,
                parent_sitemap_url TEXT,
                depth INTEGER DEFAULT 0,
                status TEXT DEFAULT 'PENDING',
                http_status INTEGER,
                urls_found INTEGER DEFAULT 0,
                classification TEXT,
                error_message TEXT,
                created_at TIMESTAMP
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sitemap_sources_import ON sitemap_sources(import_id)")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sitemap_extracted_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id TEXT NOT NULL,
                source_sitemap_url TEXT,
                raw_url TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                status TEXT DEFAULT 'VALID',
                robots_allowed BOOLEAN DEFAULT 1,
                selected_for_crawl BOOLEAN DEFAULT 1,
                rejection_reason TEXT,
                lastmod TEXT,
                changefreq TEXT,
                priority REAL,
                created_at TIMESTAMP,
                UNIQUE (import_id, normalized_url)
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sitemap_urls_import ON sitemap_extracted_urls(import_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sitemap_urls_status ON sitemap_extracted_urls(import_id, status)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sitemap_urls_selected ON sitemap_extracted_urls(import_id, selected_for_crawl)")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS request_diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id TEXT,
                job_id TEXT,
                url TEXT NOT NULL,
                final_url TEXT,
                stage TEXT NOT NULL,
                http_status INTEGER,
                classification TEXT NOT NULL,
                evidence_type TEXT DEFAULT 'VERIFIED_EVIDENCE',
                what_happened TEXT,
                where_it_happened TEXT,
                what_verified TEXT,
                recommended_action TEXT,
                retry_appropriate BOOLEAN DEFAULT 0,
                safe_headers TEXT,
                details TEXT,
                created_at TIMESTAMP
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_diagnostics_import ON request_diagnostics(import_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_diagnostics_job ON request_diagnostics(job_id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_diagnostics_class ON request_diagnostics(classification)")

        # Migrations for existing tables
        try:
            await self.conn.execute("ALTER TABLE request_diagnostics ADD COLUMN evidence_type TEXT DEFAULT 'VERIFIED_EVIDENCE'")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE extracted_links ADD COLUMN extracted_timestamp TEXT")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN force_recheck BOOLEAN DEFAULT 0")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE batches ADD COLUMN worker_id TEXT")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE batches ADD COLUMN lease_expires_at TIMESTAMP")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE url_results ADD COLUMN page_type TEXT")
            await self.conn.execute("ALTER TABLE url_results ADD COLUMN business TEXT")
            await self.conn.execute("ALTER TABLE url_results ADD COLUMN classification_method TEXT")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE source_classifications ADD COLUMN tag_1 TEXT")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE source_classifications ADD COLUMN tag_2 TEXT")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE source_classifications ADD COLUMN diagnostic_notes TEXT")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN worker_id TEXT")
        except Exception: pass
        try:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN lease_expires_at TIMESTAMP")
        except Exception: pass
        
        await self.conn.commit()
        
    async def reset_running_batches(self, worker_id: Optional[str] = None):
        now = datetime.utcnow().isoformat() + "Z"
        if worker_id:
            prefix = f"{worker_id}%"
            # Only reset batches previously held by THIS worker (or sub-worker), or batches whose lease expired
            await self.conn.execute("""
                UPDATE batches SET status = 'QUEUED', worker_id = NULL, lease_expires_at = NULL 
                WHERE status = 'RUNNING' AND (worker_id = ? OR worker_id LIKE ? OR lease_expires_at IS NULL OR lease_expires_at < ?)
            """, (worker_id, prefix, now))
            await self.conn.execute("""
                UPDATE jobs SET worker_id = NULL, lease_expires_at = NULL 
                WHERE (worker_id = ? OR worker_id LIKE ? OR (lease_expires_at IS NOT NULL AND lease_expires_at < ?))
            """, (worker_id, prefix, now))
        else:
            # Only reset abandoned batches whose lease has expired or have no assigned worker
            await self.conn.execute("""
                UPDATE batches SET status = 'QUEUED', worker_id = NULL, lease_expires_at = NULL 
                WHERE status = 'RUNNING' AND (worker_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?)
            """, (now,))
            await self.conn.execute("""
                UPDATE jobs SET worker_id = NULL, lease_expires_at = NULL 
                WHERE worker_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?
            """, (now,))

        # 1. Finalize RUNNING jobs that have NO remaining RUNNING or QUEUED batches
        await self.conn.execute("""
            UPDATE jobs SET status = CASE 
                WHEN id IN (SELECT DISTINCT job_id FROM batches WHERE status = 'FAILED') THEN 'PARTIAL'
                ELSE 'COMPLETED'
            END, worker_id = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE status = 'RUNNING' 
              AND id NOT IN (SELECT DISTINCT job_id FROM batches WHERE status IN ('RUNNING', 'QUEUED'))
              AND id IN (SELECT DISTINCT job_id FROM batches)
        """, (now,))

        # 2. Only reset jobs back to QUEUED if they have remaining QUEUED batches and no active RUNNING batches
        await self.conn.execute("""
            UPDATE jobs SET status = 'QUEUED', worker_id = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE status = 'RUNNING' 
              AND id NOT IN (SELECT DISTINCT job_id FROM batches WHERE status = 'RUNNING')
              AND id IN (SELECT DISTINCT job_id FROM batches WHERE status = 'QUEUED')
        """, (now,))

        # 3. Mark empty or abandoned QUEUED jobs with 0 batches as FAILED so they do not hang indefinitely
        await self.conn.execute("""
            UPDATE jobs SET status = 'FAILED', updated_at = ?
            WHERE status = 'QUEUED' 
              AND id NOT IN (SELECT DISTINCT job_id FROM batches)
        """, (now,))
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def get_cached_result(self, normalized_url: str):
        if not settings.CACHE_ENABLED:
            return None
            
        async with self.conn.execute(
            "SELECT * FROM url_results WHERE normalized_url = ?", (normalized_url,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                cols = [description[0] for description in cursor.description]
                result = dict(zip(cols, row))
                # Check TTL
                if result['checked_at']:
                    checked_at = datetime.fromisoformat(result['checked_at'])
                    from datetime import timezone
                    now_utc = datetime.now(timezone.utc)
                    # If checked_at is naive, make it aware (for old data)
                    if checked_at.tzinfo is None:
                        checked_at = checked_at.replace(tzinfo=timezone.utc)
                    if now_utc - checked_at < timedelta(hours=settings.CACHE_TTL_HOURS):
                        return result
            return None

    async def _execute_with_retry(self, sql: str, parameters: list):
        max_retries = 5
        base_delay = 0.1
        
        for attempt in range(max_retries):
            start_time = time.time()
            try:
                await self.conn.executemany(sql, parameters)
                await self.conn.commit()
                
                duration = time.time() - start_time
                db_metrics.record_write(duration, len(parameters))
                return True
            except aiosqlite.OperationalError as e:
                if "database is locked" in str(e).lower() or "busy" in str(e).lower():
                    db_metrics.lock_errors += 1
                    if attempt < max_retries - 1:
                        db_metrics.retries += 1
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
                        await asyncio.sleep(delay)
                    else:
                        db_metrics.exhausted_retries += 1
                        db_metrics.failed_operations += len(parameters)
                        raise
                else:
                    db_metrics.failed_operations += len(parameters)
                    raise

    async def batch_insert_results(self, results: List[Dict[str, Any]]):
        if not results:
            return
            
        # Ensure new columns exist in results dict (default None)
        for r in results:
            r.setdefault('original_url', r.get('normalized_url'))
            r.setdefault('page_type', None)
            r.setdefault('business', None)
            r.setdefault('classification_method', None)
            
        sql = """
            INSERT INTO url_results (
                normalized_url, original_url, initial_status_code, final_status_code, status,
                final_url, redirect_count, redirect_chain, response_time_ms, content_type,
                content_length, error_type, error_message, check_method, browser_verified,
                page_type, business, classification_method, checked_at
            ) VALUES (
                :normalized_url, :original_url, :initial_status_code, :final_status_code, :status,
                :final_url, :redirect_count, :redirect_chain, :response_time_ms, :content_type,
                :content_length, :error_type, :error_message, :check_method, :browser_verified,
                :page_type, :business, :classification_method, :checked_at
            )
            ON CONFLICT(normalized_url) DO UPDATE SET
                original_url = excluded.original_url,
                initial_status_code = excluded.initial_status_code,
                final_status_code = excluded.final_status_code,
                status = excluded.status,
                final_url = excluded.final_url,
                redirect_count = excluded.redirect_count,
                redirect_chain = excluded.redirect_chain,
                response_time_ms = excluded.response_time_ms,
                content_type = excluded.content_type,
                content_length = excluded.content_length,
                error_type = excluded.error_type,
                error_message = excluded.error_message,
                check_method = excluded.check_method,
                browser_verified = excluded.browser_verified,
                page_type = COALESCE(excluded.page_type, url_results.page_type),
                business = COALESCE(excluded.business, url_results.business),
                classification_method = COALESCE(excluded.classification_method, url_results.classification_method),
                checked_at = excluded.checked_at
        """
        await self._execute_with_retry(sql, results)

    async def batch_insert_results_and_destinations(
        self,
        results: Optional[List[Dict[str, Any]]] = None,
        extracted: Optional[List[Dict[str, Any]]] = None
    ):
        """Single-transaction combined insertion of url_results, job_destinations, and extracted_links to eliminate commit overhead."""
        if not results and not extracted:
            return
            
        if results:
            for r in results:
                r.setdefault('original_url', r.get('normalized_url'))
                r.setdefault('page_type', None)
                r.setdefault('business', None)
                r.setdefault('classification_method', None)
            
        sql_results = """
            INSERT INTO url_results (
                normalized_url, original_url, initial_status_code, final_status_code, status,
                final_url, redirect_count, redirect_chain, response_time_ms, content_type,
                content_length, error_type, error_message, check_method, browser_verified,
                page_type, business, classification_method, checked_at
            ) VALUES (
                :normalized_url, :original_url, :initial_status_code, :final_status_code, :status,
                :final_url, :redirect_count, :redirect_chain, :response_time_ms, :content_type,
                :content_length, :error_type, :error_message, :check_method, :browser_verified,
                :page_type, :business, :classification_method, :checked_at
            )
            ON CONFLICT(normalized_url) DO UPDATE SET
                original_url = excluded.original_url,
                initial_status_code = excluded.initial_status_code,
                final_status_code = excluded.final_status_code,
                status = excluded.status,
                final_url = excluded.final_url,
                redirect_count = excluded.redirect_count,
                redirect_chain = excluded.redirect_chain,
                response_time_ms = excluded.response_time_ms,
                content_type = excluded.content_type,
                content_length = excluded.content_length,
                error_type = excluded.error_type,
                error_message = excluded.error_message,
                check_method = excluded.check_method,
                browser_verified = excluded.browser_verified,
                page_type = COALESCE(excluded.page_type, url_results.page_type),
                business = COALESCE(excluded.business, url_results.business),
                classification_method = COALESCE(excluded.classification_method, url_results.classification_method),
                checked_at = excluded.checked_at
        """

        sql_dest = """
            INSERT INTO job_destinations (
                job_id, normalized_url, initial_status_code, final_status_code,
                status, final_url, redirect_count, redirect_chain,
                response_time_ms, content_type, content_length,
                error_type, error_message, check_method, browser_verified,
                checked_at, retries_count
            ) VALUES (
                :job_id, :normalized_url, :initial_status_code, :final_status_code,
                :status, :final_url, :redirect_count, :redirect_chain,
                :response_time_ms, :content_type, :content_length,
                :error_type, :error_message, :check_method, :browser_verified,
                :checked_at, :retries_count
            )
            ON CONFLICT(job_id, normalized_url) DO UPDATE SET
                initial_status_code = excluded.initial_status_code,
                final_status_code = excluded.final_status_code,
                status = excluded.status,
                final_url = excluded.final_url,
                redirect_count = excluded.redirect_count,
                redirect_chain = excluded.redirect_chain,
                response_time_ms = excluded.response_time_ms,
                content_type = excluded.content_type,
                content_length = excluded.content_length,
                error_type = excluded.error_type,
                error_message = excluded.error_message,
                check_method = excluded.check_method,
                browser_verified = excluded.browser_verified,
                checked_at = excluded.checked_at,
                retries_count = excluded.retries_count
        """

        sql_extracted = """
            INSERT INTO extracted_links (
                job_id, batch_id, source_url, original_url, normalized_url,
                anchor_text, component_name, image_name, loading_type, extracted_timestamp
            ) VALUES (
                :job_id, :batch_id, :source_url, :original_url, :normalized_url,
                :anchor_text, :component_name, :image_name, :loading_type, :extracted_timestamp
            )
        """

        clean_dest = []
        if results:
            for r in results:
                clean_dest.append({
                    "job_id": r.get("job_id", ""),
                    "normalized_url": r.get("normalized_url", ""),
                    "initial_status_code": r.get("initial_status_code"),
                    "final_status_code": r.get("final_status_code"),
                    "status": r.get("status", "Pending Validation"),
                    "final_url": r.get("final_url", r.get("normalized_url", "")),
                    "redirect_count": r.get("redirect_count", 0),
                    "redirect_chain": r.get("redirect_chain", "[]"),
                    "response_time_ms": r.get("response_time_ms", 0.0),
                    "content_type": r.get("content_type", ""),
                    "content_length": r.get("content_length", 0),
                    "error_type": r.get("error_type", ""),
                    "error_message": r.get("error_message", ""),
                    "check_method": r.get("check_method", "HTTP"),
                    "browser_verified": r.get("browser_verified", "NO"),
                    "checked_at": r.get("checked_at"),
                    "retries_count": r.get("retries_count", 0)
                })

        total_ops = (len(results) * 2 if results else 0) + (len(extracted) if extracted else 0)
        max_retries = 5
        base_delay = 0.1
        for attempt in range(max_retries):
            start_time = time.time()
            try:
                if results:
                    await self.conn.executemany(sql_results, results)
                    await self.conn.executemany(sql_dest, clean_dest)
                if extracted:
                    await self.conn.executemany(sql_extracted, extracted)
                await self.conn.commit()
                duration = time.time() - start_time
                db_metrics.record_write(duration, total_ops)
                return True
            except aiosqlite.OperationalError as e:
                try:
                    await self.conn.rollback()
                except Exception:
                    pass
                if "database is locked" in str(e).lower() or "busy" in str(e).lower():
                    db_metrics.lock_errors += 1
                    if attempt < max_retries - 1:
                        db_metrics.retries += 1
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
                        await asyncio.sleep(delay)
                    else:
                        db_metrics.exhausted_retries += 1
                        db_metrics.failed_operations += total_ops
                        raise
                else:
                    db_metrics.failed_operations += total_ops
                    raise
            except Exception as e:
                try:
                    await self.conn.rollback()
                except Exception:
                    pass
                db_metrics.failed_operations += total_ops
                raise

    async def batch_insert_image_results(self, results: List[Dict[str, Any]]):
        if not results: return
        sql = """
            INSERT OR REPLACE INTO image_results (
                normalized_url, initial_status_code, final_status_code, status,
                image_format, image_size_kb, is_dam_asset, source_platform, error_type, error_message, checked_at
            ) VALUES (
                :normalized_url, :initial_status_code, :final_status_code, :status,
                :image_format, :image_size_kb, :is_dam_asset, :source_platform, :error_type, :error_message, :checked_at
            )
        """
        await self._execute_with_retry(sql, results)

    async def get_all_results(self):
        async with self.conn.execute("SELECT * FROM url_results") as cursor:
            cols = [description[0] for description in cursor.description]
            rows = await cursor.fetchall()
            return [dict(zip(cols, row)) for row in rows]
            
    async def get_cached_image_result(self, normalized_url: str):
        if not settings.CACHE_ENABLED: return None
        async with self.conn.execute("SELECT * FROM image_results WHERE normalized_url = ?", (normalized_url,)) as cursor:
            row = await cursor.fetchone()
            if row:
                cols = [description[0] for description in cursor.description]
                result = dict(zip(cols, row))
                if result['checked_at']:
                    checked_at = datetime.fromisoformat(result['checked_at'])
                    from datetime import timezone
                    now_utc = datetime.now(timezone.utc)
                    if checked_at.tzinfo is None:
                        checked_at = checked_at.replace(tzinfo=timezone.utc)
                    if now_utc - checked_at < timedelta(hours=settings.CACHE_TTL_HOURS):
                        return result
            return None
            
    async def get_all_image_results(self):
        async with self.conn.execute("SELECT * FROM image_results") as cursor:
            cols = [description[0] for description in cursor.description]
            rows = await cursor.fetchall()
            return [dict(zip(cols, row)) for row in rows]

    async def get_unique_destinations_for_job(self, job_id: str):
        sql = "SELECT DISTINCT normalized_url FROM extracted_links WHERE job_id = ?"
        async with self.conn.execute(sql, (job_id,)) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def iter_unique_destinations_for_job(self, job_id: str, chunk_size: int = 5000):
        """Streams unique destinations in bounded chunks to avoid loading 500k URLs into memory."""
        sql = "SELECT DISTINCT normalized_url FROM extracted_links WHERE job_id = ?"
        async with self.conn.execute(sql, (job_id,)) as cursor:
            while True:
                rows = await cursor.fetchmany(chunk_size)
                if not rows:
                    break
                for row in rows:
                    yield row[0]

    async def update_source_classification(
        self, source_url: str, page_type: str, business: str, method: str,
        tag_1: Optional[str] = None, tag_2: Optional[str] = None, diagnostic_notes: Optional[str] = None
    ):
        sql = """
            INSERT INTO source_classifications (
                source_url, page_type, business, classification_method,
                tag_1, tag_2, diagnostic_notes, classified_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_url) DO UPDATE SET
                page_type = excluded.page_type,
                business = excluded.business,
                classification_method = excluded.classification_method,
                tag_1 = excluded.tag_1,
                tag_2 = excluded.tag_2,
                diagnostic_notes = excluded.diagnostic_notes,
                classified_at = CURRENT_TIMESTAMP
        """
        await self._execute_with_retry(sql, [[source_url, page_type, business, method, tag_1, tag_2, diagnostic_notes]])

    async def get_source_classification(self, source_url: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT page_type, business, classification_method, tag_1, tag_2, diagnostic_notes FROM source_classifications WHERE source_url = ?"
        async with self.conn.execute(sql, (source_url,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return {
                    "page_type": row[0],
                    "business": row[1],
                    "classification_method": row[2],
                    "tag_1": row[3],
                    "tag_2": row[4],
                    "diagnostic_notes": row[5]
                }
            return None

    async def batch_insert_page_audit_results(self, results: List[Dict[str, Any]]):
        if not results: return
        sql = """
            INSERT INTO page_audit_results (
                job_id, batch_id, page_url, final_url, page_status, http_status,
                response_time_ms, error_message, screenshot_path, api_count,
                failed_api_count, summary_reason, evaluated_at
            ) VALUES (
                :job_id, :batch_id, :page_url, :final_url, :page_status, :http_status,
                :response_time_ms, :error_message, :screenshot_path, :api_count,
                :failed_api_count, :summary_reason, :evaluated_at
            )
            ON CONFLICT(job_id, page_url) DO UPDATE SET
                batch_id=excluded.batch_id,
                final_url=excluded.final_url,
                page_status=excluded.page_status,
                http_status=excluded.http_status,
                response_time_ms=excluded.response_time_ms,
                error_message=excluded.error_message,
                screenshot_path=excluded.screenshot_path,
                api_count=excluded.api_count,
                failed_api_count=excluded.failed_api_count,
                summary_reason=excluded.summary_reason,
                evaluated_at=excluded.evaluated_at
        """
        await self._execute_with_retry(sql, results)

    async def batch_insert_api_responses(self, responses: List[Dict[str, Any]]):
        if not responses: return
        pages_to_clear = list({(r['job_id'], r['page_url']) for r in responses if 'job_id' in r and 'page_url' in r})
        for j_id, p_url in pages_to_clear:
            await self._execute_with_retry("DELETE FROM page_api_responses WHERE job_id = ? AND page_url = ?", [(j_id, p_url)])
        sql = """
            INSERT INTO page_api_responses (
                job_id, page_url, request_url, method, resource_type, http_status,
                api_status, response_time_ms, content_type, error_message,
                response_snippet, observed_at
            ) VALUES (
                :job_id, :page_url, :request_url, :method, :resource_type, :http_status,
                :api_status, :response_time_ms, :content_type, :error_message,
                :response_snippet, :observed_at
            )
        """
        await self._execute_with_retry(sql, responses)

    async def get_audited_page_urls(self, job_id: str) -> List[str]:
        sql = "SELECT page_url FROM page_audit_results WHERE job_id = ?"
        async with self.conn.execute(sql, (job_id,)) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def get_page_audit_results(self, job_id: str):
        sql = "SELECT * FROM page_audit_results WHERE job_id = ? ORDER BY id ASC"
        async with self.conn.execute(sql, (job_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def get_page_api_responses(self, job_id: str):
        sql = "SELECT * FROM page_api_responses WHERE job_id = ? ORDER BY id ASC"
        async with self.conn.execute(sql, (job_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def get_api_audit_stats(self, job_id: str):
        sql_pages = """
            SELECT 
                COUNT(*) as total_pages,
                SUM(CASE WHEN page_status = 'FOUND' THEN 1 ELSE 0 END) as found,
                SUM(CASE WHEN page_status = 'NOT_FOUND' THEN 1 ELSE 0 END) as not_found,
                SUM(CASE WHEN page_status = 'API_ERROR' THEN 1 ELSE 0 END) as api_error,
                SUM(CASE WHEN page_status = 'PAGE_ERROR' THEN 1 ELSE 0 END) as page_error,
                SUM(CASE WHEN page_status = 'TIMEOUT' THEN 1 ELSE 0 END) as timeout,
                SUM(CASE WHEN page_status = 'INCONCLUSIVE' THEN 1 ELSE 0 END) as inconclusive,
                SUM(CASE WHEN page_status = 'SKIPPED' THEN 1 ELSE 0 END) as skipped,
                SUM(CASE WHEN screenshot_path IS NOT NULL AND screenshot_path != '' THEN 1 ELSE 0 END) as screenshots_count
            FROM page_audit_results
            WHERE job_id = ?
        """
        sql_apis = """
            SELECT 
                COUNT(*) as total_apis,
                SUM(CASE WHEN api_status = 'FOUND' THEN 1 ELSE 0 END) as successful_apis,
                SUM(CASE WHEN api_status IN ('API_ERROR', 'TIMEOUT') THEN 1 ELSE 0 END) as failed_apis
            FROM page_api_responses
            WHERE job_id = ?
        """
        async with self.conn.execute(sql_pages, (job_id,)) as cur:
            cols_p = [desc[0] for desc in cur.description]
            page_stats = dict(zip(cols_p, await cur.fetchone()))
            
        async with self.conn.execute(sql_apis, (job_id,)) as cur:
            cols_a = [desc[0] for desc in cur.description]
            api_stats = dict(zip(cols_a, await cur.fetchone()))
            
        return {**page_stats, **api_stats}

    async def cleanup_stale_artifacts(self, days: Optional[int] = None) -> int:
        if days is None:
            days = settings.SCREENSHOT_RETENTION_DAYS
        import os
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        sql = "SELECT DISTINCT job_id, screenshot_path FROM page_audit_results WHERE evaluated_at < ? AND screenshot_path IS NOT NULL AND screenshot_path != ''"
        deleted_count = 0
        try:
            async with self.conn.execute(sql, (cutoff,)) as cur:
                rows = await cur.fetchall()
                for job_id, s_path in rows:
                    if s_path and os.path.exists(s_path):
                        try:
                            os.remove(s_path)
                            deleted_count += 1
                        except OSError:
                            pass
        except Exception:
            pass
        return deleted_count

    async def batch_upsert_job_destinations(self, results: List[Dict[str, Any]]):
        if not results: return
        sql = """
            INSERT INTO job_destinations (
                job_id, normalized_url, initial_status_code, final_status_code,
                status, final_url, redirect_count, redirect_chain,
                response_time_ms, content_type, content_length,
                error_type, error_message, check_method, browser_verified,
                checked_at, retries_count
            ) VALUES (
                :job_id, :normalized_url, :initial_status_code, :final_status_code,
                :status, :final_url, :redirect_count, :redirect_chain,
                :response_time_ms, :content_type, :content_length,
                :error_type, :error_message, :check_method, :browser_verified,
                :checked_at, :retries_count
            )
            ON CONFLICT(job_id, normalized_url) DO UPDATE SET
                initial_status_code = excluded.initial_status_code,
                final_status_code = excluded.final_status_code,
                status = excluded.status,
                final_url = excluded.final_url,
                redirect_count = excluded.redirect_count,
                redirect_chain = excluded.redirect_chain,
                response_time_ms = excluded.response_time_ms,
                content_type = excluded.content_type,
                content_length = excluded.content_length,
                error_type = excluded.error_type,
                error_message = excluded.error_message,
                check_method = excluded.check_method,
                browser_verified = excluded.browser_verified,
                checked_at = excluded.checked_at,
                retries_count = excluded.retries_count
        """
        clean_results = []
        for r in results:
            clean_results.append({
                "job_id": r.get("job_id", ""),
                "normalized_url": r.get("normalized_url", ""),
                "initial_status_code": r.get("initial_status_code"),
                "final_status_code": r.get("final_status_code"),
                "status": r.get("status", "Pending Validation"),
                "final_url": r.get("final_url", r.get("normalized_url", "")),
                "redirect_count": r.get("redirect_count", 0),
                "redirect_chain": r.get("redirect_chain", "[]"),
                "response_time_ms": r.get("response_time_ms", 0.0),
                "content_type": r.get("content_type", ""),
                "content_length": r.get("content_length", 0),
                "error_type": r.get("error_type", ""),
                "error_message": r.get("error_message", ""),
                "check_method": r.get("check_method", "HTTP"),
                "browser_verified": r.get("browser_verified", "NO"),
                "checked_at": r.get("checked_at") or (datetime.utcnow().isoformat() + "Z"),
                "retries_count": r.get("retries_count", 0)
            })
        await self._execute_with_retry(sql, clean_results)

    async def get_job_destinations(self, job_id: str) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM job_destinations WHERE job_id = ? ORDER BY normalized_url ASC"
        async with self.conn.execute(sql, (job_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def get_job_non_200_destinations(self, job_id: str) -> List[Dict[str, Any]]:
        sql = """
            SELECT 
                d.job_id,
                d.normalized_url,
                d.initial_status_code,
                d.final_status_code,
                d.status,
                d.final_url,
                d.redirect_count,
                d.redirect_chain,
                d.response_time_ms,
                d.error_type,
                d.error_message,
                d.checked_at,
                COUNT(e.id) as affected_relationships_count,
                COUNT(DISTINCT e.source_url) as affected_sources_count,
                MIN(e.original_url) as sample_original_url,
                GROUP_CONCAT(DISTINCT e.source_url) as source_urls_concat
            FROM job_destinations d
            JOIN extracted_links e ON d.job_id = e.job_id AND d.normalized_url = e.normalized_url
            WHERE d.job_id = ? 
              AND (d.final_status_code != 200 OR d.final_status_code IS NULL)
            GROUP BY d.normalized_url
            ORDER BY affected_relationships_count DESC, d.normalized_url ASC
        """
        async with self.conn.execute(sql, (job_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            rows = [dict(zip(cols, row)) for row in await cursor.fetchall()]

        needed_sources = set()
        row_src_map = []
        for row in rows:
            src_list = [s for s in (row.get("source_urls_concat") or "").split(",") if s]
            needed_sources.update(src_list[:10])
            row_src_map.append(src_list)

        # Batch-fetch source classifications in chunks to eliminate N+1 queries
        class_map = {}
        if needed_sources:
            source_list = list(needed_sources)
            chunk_size = 500
            for i in range(0, len(source_list), chunk_size):
                chunk = source_list[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                q = f"SELECT source_url, page_type, business FROM source_classifications WHERE source_url IN ({placeholders})"
                async with self.conn.execute(q, chunk) as cur:
                    for s_url, p_type, b_ness in await cur.fetchall():
                        class_map[s_url] = {"page_type": p_type, "business": b_ness}

        for row, src_list in zip(rows, row_src_map):
            pts = set()
            bizs = set()
            for s in src_list[:10]:
                sc = class_map.get(s)
                if sc:
                    if sc.get("page_type"): pts.add(sc["page_type"])
                    if sc.get("business"): bizs.add(sc["business"])
            row["source_page_type"] = ", ".join(sorted(pts)) if pts else "NEEDS_REVIEW"
            row["business"] = ", ".join(sorted(bizs)) if bizs else "NEEDS_REVIEW"
            row["source_pages_sample"] = ", ".join(src_list[:5])
        return rows

    async def get_link_audit_stats(self, job_id: str) -> Dict[str, Any]:
        now = time.monotonic()
        if not hasattr(self, "_link_stats_cache"):
            self._link_stats_cache = {}
        cached = self._link_stats_cache.get(job_id)
        if cached:
            cached_time, cached_val = cached
            if now - cached_time < 1.5:
                return dict(cached_val)

        sql_ext = """
            SELECT 
                COUNT(*) as total_extracted,
                COUNT(DISTINCT normalized_url) as total_unique
            FROM extracted_links
            WHERE job_id = ?
        """
        async with self.conn.execute(sql_ext, (job_id,)) as cur:
            row_ext = await cur.fetchone()
            total_extracted = row_ext[0] if row_ext else 0
            total_unique = row_ext[1] if row_ext else 0

        duplicate_avoided = max(0, total_extracted - total_unique)
        dedup_ratio = round((duplicate_avoided / total_extracted) * 100, 2) if total_extracted > 0 else 0.0

        sql_val = """
            SELECT 
                COUNT(*) as unique_validated,
                SUM(CASE WHEN final_status_code = 200 THEN 1 ELSE 0 END) as working_200,
                SUM(CASE WHEN final_status_code >= 300 AND final_status_code <= 399 THEN 1 ELSE 0 END) as redirects_3xx,
                SUM(CASE WHEN final_status_code >= 400 AND final_status_code <= 499 THEN 1 ELSE 0 END) as client_errors_4xx,
                SUM(CASE WHEN final_status_code >= 500 AND final_status_code <= 599 THEN 1 ELSE 0 END) as server_errors_5xx,
                SUM(CASE WHEN error_type = 'Timeout' THEN 1 ELSE 0 END) as timeouts,
                SUM(CASE WHEN error_type IN ('DNS Error', 'Connection Error', 'Network Error', 'SSL Error') THEN 1 ELSE 0 END) as network_errors,
                SUM(CASE WHEN error_type LIKE '%Blocked%' OR status LIKE '%Blocked%' THEN 1 ELSE 0 END) as blocked_skipped,
                SUM(CASE WHEN (final_status_code != 200 OR final_status_code IS NULL) THEN 1 ELSE 0 END) as validation_failures,
                SUM(retries_count) as total_retries
            FROM job_destinations
            WHERE job_id = ?
        """
        async with self.conn.execute(sql_val, (job_id,)) as cur:
            cols = [desc[0] for desc in cur.description]
            val_stats = dict(zip(cols, await cur.fetchone()))

        unique_validated = val_stats.get("unique_validated") or 0
        working = val_stats.get("working_200") or 0
        redirects = val_stats.get("redirects_3xx") or 0
        client_err = val_stats.get("client_errors_4xx") or 0
        server_err = val_stats.get("server_errors_5xx") or 0
        timeouts = val_stats.get("timeouts") or 0
        network_failures = val_stats.get("network_errors") or 0
        blocked = val_stats.get("blocked_skipped") or 0
        failures = val_stats.get("validation_failures") or 0
        retries = val_stats.get("total_retries") or 0

        pending = max(0, total_unique - unique_validated)

        async with self.conn.execute("SELECT created_at, updated_at FROM jobs WHERE id = ?", (job_id,)) as cur:
            j_row = await cur.fetchone()
            created_at = j_row[0] if j_row else None
            updated_at = j_row[1] if j_row else None

        crawl_duration_sec = 0.0
        if created_at and updated_at:
            try:
                t0 = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                crawl_duration_sec = round((t1 - t0).total_seconds(), 2)
            except Exception:
                pass

        result = {
            "total_extracted": total_extracted,
            "total_unique": total_unique,
            "duplicate_avoided": duplicate_avoided,
            "dedup_ratio": dedup_ratio,
            "total_checked": unique_validated,
            "unique_validated": unique_validated,
            "working": working,
            "working_200": working,
            "redirects": redirects,
            "client_errors_4xx": client_err,
            "server_errors": server_err,
            "timeouts": timeouts,
            "network_failures": network_failures,
            "blocked_skipped": blocked,
            "validation_failures": failures,
            "retries_count": retries,
            "pending": pending,
            "queued_destinations": pending,
            "in_flight_validations": 0,
            "completed_unique": unique_validated,
            "urls_per_minute": 0.0,
            "estimated_remaining_time": "0s",
            "current_concurrency": getattr(settings, 'LINK_AUDIT_CONCURRENCY', 10),
            "peak_concurrency_observed": getattr(settings, 'LINK_AUDIT_CONCURRENCY', 10),
            "crawl_duration_sec": crawl_duration_sec,
            "created_at": created_at,
            "updated_at": updated_at
        }
        self._link_stats_cache[job_id] = (now, result)
        return result

    async def get_overview_stats(self) -> Dict[str, Any]:
        sql = """
            SELECT 
                COUNT(*) as total_jobs,
                SUM(CASE WHEN j.status IN ('RUNNING', 'CLASSIFYING', 'VALIDATING', 'FINALIZING') THEN 1 ELSE 0 END) as running_jobs,
                SUM(CASE WHEN j.status = 'QUEUED' THEN 1 ELSE 0 END) as queued_jobs,
                SUM(CASE WHEN j.status = 'COMPLETED' THEN 1 ELSE 0 END) as completed_jobs,
                SUM(CASE WHEN j.status IN ('FAILED', 'PARTIAL') THEN 1 ELSE 0 END) as failed_jobs,
                SUM(CASE WHEN j.status = 'CANCELLED' THEN 1 ELSE 0 END) as cancelled_jobs,
                SUM(CASE WHEN j.status = 'PAUSED' THEN 1 ELSE 0 END) as paused_jobs,
                SUM(COALESCE(j.total_records, 0)) as total_submitted_urls,
                SUM(COALESCE(j.processed_records, 0)) as total_processed_records,
                SUM(CASE WHEN u.mode = 'link_audit' THEN 1 ELSE 0 END) as link_audit_jobs,
                SUM(CASE WHEN u.mode = 'api_response_audit' THEN 1 ELSE 0 END) as api_audit_jobs,
                SUM(CASE WHEN u.mode = 'image_audit' THEN 1 ELSE 0 END) as image_audit_jobs,
                SUM(CASE WHEN u.mode = 'url_status' THEN 1 ELSE 0 END) as url_status_jobs
            FROM jobs j
            LEFT JOIN uploads u ON j.upload_id = u.id
        """
        async with self.conn.execute(sql) as cur:
            row = await cur.fetchone()
            cols = [desc[0] for desc in cur.description]
            res = dict(zip(cols, row)) if row else {}

        sql_dest = """
            SELECT 
                COUNT(*) as total_destinations_validated,
                SUM(CASE WHEN final_status_code = 200 THEN 1 ELSE 0 END) as total_200_ok,
                SUM(CASE WHEN final_status_code != 200 OR final_status_code IS NULL THEN 1 ELSE 0 END) as total_non_200
            FROM job_destinations
        """
        try:
            async with self.conn.execute(sql_dest) as cur:
                dest_row = await cur.fetchone()
                if dest_row:
                    res["total_destinations_validated"] = dest_row[0] or 0
                    res["total_200_ok"] = dest_row[1] or 0
                    res["total_non_200"] = dest_row[2] or 0
        except Exception:
            res["total_destinations_validated"] = 0
            res["total_200_ok"] = 0
            res["total_non_200"] = 0

        return res

    async def get_job_records_paginated(
        self, job_id: str, page: int = 1, page_size: int = 50, query: str = "", status_filter: str = ""
    ) -> Dict[str, Any]:
        page = max(1, page)
        page_size = min(max(1, page_size), 500)
        offset = (page - 1) * page_size

        where_clauses = ["e.job_id = ?"]
        params: List[Any] = [job_id]

        if query and query.strip():
            where_clauses.append("(e.source_url LIKE ? OR e.original_url LIKE ? OR e.normalized_url LIKE ? OR e.anchor_text LIKE ?)")
            q_param = f"%{query.strip()}%"
            params.extend([q_param, q_param, q_param, q_param])

        if status_filter:
            s_up = status_filter.strip().upper()
            if s_up in ("200", "OK"):
                where_clauses.append("d.final_status_code = 200")
            elif s_up in ("NON_200", "FAIL"):
                where_clauses.append("(d.final_status_code != 200 OR d.final_status_code IS NULL)")
            elif s_up in ("3XX", "REDIRECT"):
                where_clauses.append("d.final_status_code >= 300 AND d.final_status_code <= 399")
            elif s_up in ("4XX", "CLIENT_ERROR"):
                where_clauses.append("d.final_status_code >= 400 AND d.final_status_code <= 499")
            elif s_up in ("5XX", "SERVER_ERROR"):
                where_clauses.append("d.final_status_code >= 500 AND d.final_status_code <= 599")
            elif s_up == "PENDING":
                where_clauses.append("d.final_status_code IS NULL")

        where_sql = " AND ".join(where_clauses)

        if status_filter:
            count_sql = f"""
                SELECT COUNT(*)
                FROM extracted_links e
                LEFT JOIN job_destinations d ON e.job_id = d.job_id AND e.normalized_url = d.normalized_url
                WHERE {where_sql}
            """
        else:
            count_sql = f"""
                SELECT COUNT(*)
                FROM extracted_links e
                WHERE {where_sql}
            """
        async with self.conn.execute(count_sql, tuple(params)) as cur:
            total_records = (await cur.fetchone())[0]

        if not status_filter and not (query and query.strip()):
            data_sql = """
                SELECT 
                    e.id,
                    e.source_url,
                    e.original_url,
                    e.normalized_url,
                    e.anchor_text,
                    e.component_name,
                    sc.page_type as source_page_type,
                    sc.business as source_business,
                    d.final_status_code,
                    d.status as destination_status,
                    d.final_url,
                    d.redirect_count,
                    d.response_time_ms,
                    d.error_type
                FROM (
                    SELECT id, source_url, original_url, normalized_url, anchor_text, component_name, job_id
                    FROM extracted_links
                    WHERE job_id = ?
                    ORDER BY id ASC
                    LIMIT ? OFFSET ?
                ) e
                LEFT JOIN job_destinations d ON e.job_id = d.job_id AND e.normalized_url = d.normalized_url
                LEFT JOIN source_classifications sc ON e.source_url = sc.source_url
            """
            fetch_params = [job_id, page_size, offset]
        else:
            data_sql = f"""
                SELECT 
                    e.id,
                    e.source_url,
                    e.original_url,
                    e.normalized_url,
                    e.anchor_text,
                    e.component_name,
                    sc.page_type as source_page_type,
                    sc.business as source_business,
                    d.final_status_code,
                    d.status as destination_status,
                    d.final_url,
                    d.redirect_count,
                    d.response_time_ms,
                    d.error_type
                FROM extracted_links e
                LEFT JOIN job_destinations d ON e.job_id = d.job_id AND e.normalized_url = d.normalized_url
                LEFT JOIN source_classifications sc ON e.source_url = sc.source_url
                WHERE {where_sql}
                ORDER BY e.id ASC
                LIMIT ? OFFSET ?
            """
            fetch_params = list(params) + [page_size, offset]
        async with self.conn.execute(data_sql, tuple(fetch_params)) as cur:
            cols = [desc[0] for desc in cur.description]
            items = [dict(zip(cols, row)) for row in await cur.fetchall()]

        total_pages = (total_records + page_size - 1) // page_size if total_records > 0 else 1

        return {
            "job_id": job_id,
            "page": page,
            "page_size": page_size,
            "total_records": total_records,
            "total_pages": total_pages,
            "records": items
        }

    async def upsert_remediations(self, remediations: List[Dict[str, Any]]) -> int:
        if not remediations:
            return 0
        now = datetime.utcnow().isoformat() + "Z"
        count = 0
        for rem in remediations:
            created_at = rem.get("created_at") or now
            updated_at = now
            source_urls_val = json.dumps(rem.get("source_urls", [])) if isinstance(rem.get("source_urls"), (list, dict)) else (rem.get("source_urls") or "[]")
            technical_details_val = json.dumps(rem.get("technical_details", {})) if isinstance(rem.get("technical_details"), (list, dict)) else (rem.get("technical_details") or "{}")
            
            await self.conn.execute("""
                INSERT INTO remediations (
                    job_id, remediation_id, category, severity, priority, priority_score,
                    confidence, status, review_notes, target_url, source_urls, source_count,
                    title, description, impact, recommended_action, priority_rationale,
                    technical_details, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, category, target_url) DO UPDATE SET
                    priority_score = excluded.priority_score,
                    priority = excluded.priority,
                    severity = excluded.severity,
                    confidence = excluded.confidence,
                    title = excluded.title,
                    description = excluded.description,
                    impact = excluded.impact,
                    recommended_action = excluded.recommended_action,
                    priority_rationale = excluded.priority_rationale,
                    technical_details = excluded.technical_details,
                    source_urls = excluded.source_urls,
                    source_count = excluded.source_count,
                    updated_at = excluded.updated_at
            """, (
                rem["job_id"],
                rem["remediation_id"],
                rem["category"],
                rem["severity"],
                rem["priority"],
                rem["priority_score"],
                rem["confidence"],
                rem.get("status", "OPEN"),
                rem.get("review_notes"),
                rem["target_url"],
                source_urls_val,
                rem.get("source_count", 1),
                rem["title"],
                rem["description"],
                rem["impact"],
                rem["recommended_action"],
                rem["priority_rationale"],
                technical_details_val,
                created_at,
                updated_at
            ))
            count += 1
        await self.conn.commit()
        return count

    async def get_remediations_paginated(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
        severity: Optional[str] = None,
        priority: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: str = "priority_score",
        sort_dir: str = "desc"
    ) -> Dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(page_size, 500))
        offset = (page - 1) * page_size
        
        where_clauses = ["job_id = ?"]
        params: List[Any] = [job_id]
        
        if severity:
            where_clauses.append("severity = ?")
            params.append(severity.upper())
        if priority:
            where_clauses.append("priority = ?")
            params.append(priority.upper())
        if category:
            where_clauses.append("category = ?")
            params.append(category.upper())
        if status:
            where_clauses.append("status = ?")
            params.append(status.upper())
        if search:
            where_clauses.append("(target_url LIKE ? OR title LIKE ? OR remediation_id LIKE ? OR description LIKE ?)")
            q = f"%{search}%"
            params.extend([q, q, q, q])
            
        where_sql = " AND ".join(where_clauses)
        
        allowed_sort = {
            "priority_score": "priority_score",
            "priority": "priority",
            "severity": "severity",
            "category": "category",
            "status": "status",
            "source_count": "source_count",
            "created_at": "created_at",
            "remediation_id": "remediation_id"
        }
        col = allowed_sort.get(sort_by, "priority_score")
        direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
        order_sql = f"{col} {direction}, id ASC"
        
        count_sql = f"SELECT COUNT(*) FROM remediations WHERE {where_sql}"
        async with self.conn.execute(count_sql, tuple(params)) as cur:
            total_records = (await cur.fetchone())[0]
            
        data_sql = f"""
            SELECT * FROM remediations
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        fetch_params = list(params) + [page_size, offset]
        async with self.conn.execute(data_sql, tuple(fetch_params)) as cur:
            cols = [desc[0] for desc in cur.description]
            rows = await cur.fetchall()
            items = []
            for row in rows:
                item = dict(zip(cols, row))
                if item.get("source_urls") and isinstance(item["source_urls"], str):
                    try: item["source_urls"] = json.loads(item["source_urls"])
                    except Exception: item["source_urls"] = [item["source_urls"]]
                if item.get("technical_details") and isinstance(item["technical_details"], str):
                    try: item["technical_details"] = json.loads(item["technical_details"])
                    except Exception: pass
                items.append(item)
                
        total_pages = (total_records + page_size - 1) // page_size if total_records > 0 else 1
        return {
            "job_id": job_id,
            "page": page,
            "page_size": page_size,
            "total_records": total_records,
            "total_pages": total_pages,
            "records": items
        }

    async def get_remediation_by_id(self, job_id: str, remediation_id: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM remediations WHERE job_id = ? AND (remediation_id = ? OR CAST(id AS TEXT) = ?)"
        async with self.conn.execute(sql, (job_id, remediation_id, remediation_id)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            item = dict(zip(cols, row))
            if item.get("source_urls") and isinstance(item["source_urls"], str):
                try: item["source_urls"] = json.loads(item["source_urls"])
                except Exception: item["source_urls"] = [item["source_urls"]]
            if item.get("technical_details") and isinstance(item["technical_details"], str):
                try: item["technical_details"] = json.loads(item["technical_details"])
                except Exception: pass
            return item

    async def update_remediation_status(
        self,
        job_id: str,
        remediation_id: str,
        status: str,
        review_notes: Optional[str] = None
    ) -> bool:
        now = datetime.utcnow().isoformat() + "Z"
        status = status.upper()
        if review_notes is not None:
            cur = await self.conn.execute("""
                UPDATE remediations
                SET status = ?, review_notes = ?, updated_at = ?
                WHERE job_id = ? AND (remediation_id = ? OR CAST(id AS TEXT) = ?)
            """, (status, review_notes, now, job_id, remediation_id, remediation_id))
        else:
            cur = await self.conn.execute("""
                UPDATE remediations
                SET status = ?, updated_at = ?
                WHERE job_id = ? AND (remediation_id = ? OR CAST(id AS TEXT) = ?)
            """, (status, now, job_id, remediation_id, remediation_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_remediation_summary(self, job_id: str) -> Dict[str, Any]:
        async with self.conn.execute("""
            SELECT 
                COUNT(*) as total_count,
                SUM(CASE WHEN priority = 'P1' THEN 1 ELSE 0 END) as p1_count,
                SUM(CASE WHEN priority = 'P2' THEN 1 ELSE 0 END) as p2_count,
                SUM(CASE WHEN priority = 'P3' THEN 1 ELSE 0 END) as p3_count,
                SUM(CASE WHEN priority = 'P4' THEN 1 ELSE 0 END) as p4_count,
                SUM(CASE WHEN severity = 'CRITICAL' THEN 1 ELSE 0 END) as critical_count,
                SUM(CASE WHEN severity = 'HIGH' THEN 1 ELSE 0 END) as high_count,
                SUM(CASE WHEN severity = 'MEDIUM' THEN 1 ELSE 0 END) as medium_count,
                SUM(CASE WHEN severity = 'LOW' THEN 1 ELSE 0 END) as low_count,
                SUM(CASE WHEN severity = 'INFO' THEN 1 ELSE 0 END) as info_count,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) as open_count,
                SUM(CASE WHEN status = 'IN_PROGRESS' THEN 1 ELSE 0 END) as in_progress_count,
                SUM(CASE WHEN status = 'RESOLVED' THEN 1 ELSE 0 END) as resolved_count,
                SUM(CASE WHEN status = 'DISMISSED' THEN 1 ELSE 0 END) as dismissed_count,
                SUM(CASE WHEN status = 'NEEDS_REVIEW' THEN 1 ELSE 0 END) as needs_review_count
            FROM remediations
            WHERE job_id = ?
        """, (job_id,)) as cur:
            row = await cur.fetchone()
            if not row or row[0] == 0:
                return {
                    "job_id": job_id,
                    "total_count": 0,
                    "by_priority": {"P1": 0, "P2": 0, "P3": 0, "P4": 0},
                    "by_severity": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0},
                    "by_status": {"OPEN": 0, "IN_PROGRESS": 0, "RESOLVED": 0, "DISMISSED": 0, "NEEDS_REVIEW": 0},
                    "by_category": {},
                    "resolved_percentage": 0.0
                }
            
            by_priority = {"P1": row[1] or 0, "P2": row[2] or 0, "P3": row[3] or 0, "P4": row[4] or 0}
            by_severity = {"CRITICAL": row[5] or 0, "HIGH": row[6] or 0, "MEDIUM": row[7] or 0, "LOW": row[8] or 0, "INFO": row[9] or 0}
            by_status = {"OPEN": row[10] or 0, "IN_PROGRESS": row[11] or 0, "RESOLVED": row[12] or 0, "DISMISSED": row[13] or 0, "NEEDS_REVIEW": row[14] or 0}
            total = row[0] or 0
            resolved = by_status["RESOLVED"]
            pct = round((resolved / total) * 100, 1) if total > 0 else 0.0

        async with self.conn.execute("""
            SELECT category, COUNT(*) as cnt
            FROM remediations
            WHERE job_id = ?
            GROUP BY category
            ORDER BY cnt DESC
        """, (job_id,)) as cur:
            by_category = {cat: cnt for cat, cnt in await cur.fetchall()}

        return {
            "job_id": job_id,
            "total_count": total,
            "by_priority": by_priority,
            "by_severity": by_severity,
            "by_status": by_status,
            "by_category": by_category,
            "resolved_percentage": pct
        }

    async def get_all_remediations_for_job(self, job_id: str) -> List[Dict[str, Any]]:
        sql = """
            SELECT * FROM remediations
            WHERE job_id = ?
            ORDER BY priority_score DESC, id ASC
        """
        async with self.conn.execute(sql, (job_id,)) as cur:
            cols = [desc[0] for desc in cur.description]
            rows = await cur.fetchall()
            items = []
            for row in rows:
                item = dict(zip(cols, row))
                if item.get("source_urls") and isinstance(item["source_urls"], str):
                    try: item["source_urls"] = json.loads(item["source_urls"])
                    except Exception: item["source_urls"] = [item["source_urls"]]
                if item.get("technical_details") and isinstance(item["technical_details"], str):
                    try: item["technical_details"] = json.loads(item["technical_details"])
                    except Exception: pass
                items.append(item)
            return items

    async def get_cached_explanation(self, cache_key: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT explanation_json, provider, model FROM remediation_explanations_cache WHERE cache_key = ?"
        async with self.conn.execute(sql, (cache_key,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            try:
                data = json.loads(row[0])
                data["_provider"] = row[1]
                data["_model"] = row[2]
                return data
            except Exception:
                return None

    async def set_cached_explanation(self, cache_key: str, explanation_json: str, provider: str, model: Optional[str] = None):
        now = datetime.utcnow().isoformat() + "Z"
        await self.conn.execute("""
            INSERT OR REPLACE INTO remediation_explanations_cache (cache_key, explanation_json, provider, model, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (cache_key, explanation_json, provider, model, now))
        await self.conn.commit()

    # =========================================================================
    # Phase 10.12 Sitemap Imports & 403 Diagnostics Database Methods
    # =========================================================================

    async def create_sitemap_import(self, import_id: str, title: str = "Sitemap Import") -> Dict[str, Any]:
        now = datetime.utcnow().isoformat() + "Z"
        await self.conn.execute("""
            INSERT INTO sitemap_imports (id, title, status, created_at, updated_at)
            VALUES (?, ?, 'INITIALIZING', ?, ?)
        """, (import_id, title, now, now))
        await self.conn.commit()
        return await self.get_sitemap_import(import_id)

    async def get_sitemap_import(self, import_id: str) -> Optional[Dict[str, Any]]:
        async with self.conn.execute("SELECT * FROM sitemap_imports WHERE id = ?", (import_id,)) as cur:
            cols = [desc[0] for desc in cur.description]
            row = await cur.fetchone()
            if not row:
                return None
            return dict(zip(cols, row))

    async def update_sitemap_import(self, import_id: str, **kwargs):
        if not kwargs:
            return
        kwargs["updated_at"] = datetime.utcnow().isoformat() + "Z"
        fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [import_id]
        sql = f"UPDATE sitemap_imports SET {fields} WHERE id = ?"
        await self.conn.execute(sql, values)
        await self.conn.commit()

    async def add_sitemap_source(
        self,
        import_id: str,
        sitemap_url: str,
        parent_sitemap_url: str = "",
        depth: int = 0,
        status: str = "PENDING",
        http_status: Optional[int] = None,
        urls_found: int = 0,
        classification: str = "VALID",
        error_message: str = ""
    ):
        now = datetime.utcnow().isoformat() + "Z"
        await self.conn.execute("""
            INSERT INTO sitemap_sources (
                import_id, sitemap_url, parent_sitemap_url, depth,
                status, http_status, urls_found, classification,
                error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            import_id, sitemap_url, parent_sitemap_url, depth,
            status, http_status, urls_found, classification,
            error_message, now
        ))
        await self.conn.commit()

    async def get_sitemap_sources(self, import_id: str) -> List[Dict[str, Any]]:
        async with self.conn.execute("SELECT * FROM sitemap_sources WHERE import_id = ? ORDER BY id ASC", (import_id,)) as cur:
            cols = [desc[0] for desc in cur.description]
            rows = await cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]

    async def add_extracted_urls_batch(self, import_id: str, urls: List[Dict[str, Any]]) -> int:
        if not urls:
            return 0
        now = datetime.utcnow().isoformat() + "Z"
        rows = [
            (
                import_id,
                u.get("source_sitemap_url", ""),
                u.get("raw_url", ""),
                u.get("normalized_url", ""),
                u.get("status", "VALID"),
                1 if u.get("robots_allowed", True) else 0,
                1 if u.get("selected_for_crawl", True) else 0,
                u.get("rejection_reason", ""),
                u.get("lastmod", ""),
                u.get("changefreq", ""),
                float(u.get("priority", 0.5)),
                now
            )
            for u in urls
        ]
        cur = await self.conn.executemany("""
            INSERT OR IGNORE INTO sitemap_extracted_urls (
                import_id, source_sitemap_url, raw_url, normalized_url,
                status, robots_allowed, selected_for_crawl, rejection_reason,
                lastmod, changefreq, priority, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        await self.conn.commit()
        return cur.rowcount

    async def get_extracted_urls(
        self,
        import_id: str,
        page: int = 1,
        page_size: int = 50,
        search: str = "",
        filter_status: str = "",
        filter_robots: Optional[bool] = None
    ) -> Dict[str, Any]:
        conditions = ["import_id = ?"]
        params = [import_id]

        if search:
            conditions.append("(normalized_url LIKE ? OR raw_url LIKE ? OR source_sitemap_url LIKE ?)")
            s_param = f"%{search}%"
            params.extend([s_param, s_param, s_param])

        if filter_status and filter_status != "ALL":
            conditions.append("status = ?")
            params.append(filter_status)

        if filter_robots is not None:
            conditions.append("robots_allowed = ?")
            params.append(1 if filter_robots else 0)

        where_clause = " AND ".join(conditions)

        # Total count
        count_sql = f"SELECT COUNT(*) FROM sitemap_extracted_urls WHERE {where_clause}"
        async with self.conn.execute(count_sql, params) as cur:
            row = await cur.fetchone()
            total_count = row[0] if row else 0

        # Paginated items
        offset = max(0, (page - 1) * page_size)
        query_sql = f"""
            SELECT * FROM sitemap_extracted_urls 
            WHERE {where_clause} 
            ORDER BY id ASC 
            LIMIT ? OFFSET ?
        """
        async with self.conn.execute(query_sql, params + [page_size, offset]) as cur:
            cols = [desc[0] for desc in cur.description]
            rows = await cur.fetchall()
            items = [dict(zip(cols, r)) for r in rows]

        return {
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "items": items
        }

    async def toggle_url_selection(self, import_id: str, url_ids: List[int], selected: bool):
        if not url_ids:
            return
        placeholders = ",".join("?" for _ in url_ids)
        sql = f"UPDATE sitemap_extracted_urls SET selected_for_crawl = ? WHERE import_id = ? AND id IN ({placeholders})"
        await self.conn.execute(sql, [1 if selected else 0, import_id] + url_ids)
        await self.conn.commit()

    async def select_all_urls(self, import_id: str, eligible_only: bool = True, selected: bool = True):
        if eligible_only:
            sql = "UPDATE sitemap_extracted_urls SET selected_for_crawl = ? WHERE import_id = ? AND robots_allowed = 1 AND status = 'VALID'"
            await self.conn.execute(sql, (1 if selected else 0, import_id))
        else:
            sql = "UPDATE sitemap_extracted_urls SET selected_for_crawl = ? WHERE import_id = ?"
            await self.conn.execute(sql, (1 if selected else 0, import_id))
        await self.conn.commit()

    async def get_selected_urls_for_crawl(self, import_id: str) -> List[str]:
        sql = """
            SELECT normalized_url FROM sitemap_extracted_urls
            WHERE import_id = ? AND selected_for_crawl = 1 AND status = 'VALID' AND robots_allowed = 1
            ORDER BY id ASC
        """
        async with self.conn.execute(sql, (import_id,)) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

    async def record_request_diagnostic(
        self,
        url: str,
        stage: str,
        classification: str,
        what_happened: str,
        where_it_happened: str,
        what_verified: str,
        recommended_action: str,
        http_status: Optional[int] = None,
        final_url: Optional[str] = None,
        import_id: Optional[str] = None,
        job_id: Optional[str] = None,
        retry_appropriate: bool = False,
        safe_headers: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
        evidence_type: str = "VERIFIED_EVIDENCE"
    ):
        now = datetime.utcnow().isoformat() + "Z"
        headers_str = json.dumps(safe_headers or {})
        details_str = json.dumps(details or {})
        await self.conn.execute("""
            INSERT INTO request_diagnostics (
                import_id, job_id, url, final_url, stage, http_status,
                classification, evidence_type, what_happened, where_it_happened, what_verified,
                recommended_action, retry_appropriate, safe_headers, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            import_id, job_id, url, final_url or url, stage, http_status,
            classification, evidence_type or "VERIFIED_EVIDENCE", what_happened, where_it_happened, what_verified,
            recommended_action, 1 if retry_appropriate else 0, headers_str, details_str, now
        ))
        await self.conn.commit()

    async def get_request_diagnostics(
        self,
        import_id: Optional[str] = None,
        job_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        conditions = []
        params = []
        if import_id:
            conditions.append("import_id = ?")
            params.append(import_id)
        if job_id:
            conditions.append("job_id = ?")
            params.append(job_id)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM request_diagnostics {where_clause} ORDER BY id DESC LIMIT ?"
        params.append(limit)

        async with self.conn.execute(sql, params) as cur:
            cols = [desc[0] for desc in cur.description]
            rows = await cur.fetchall()
            items = []
            for r in rows:
                item = dict(zip(cols, r))
                if item.get("safe_headers") and isinstance(item["safe_headers"], str):
                    try: item["safe_headers"] = json.loads(item["safe_headers"])
                    except Exception: pass
                if item.get("details") and isinstance(item["details"], str):
                    try: item["details"] = json.loads(item["details"])
                    except Exception: pass
                items.append(item)
            return items

