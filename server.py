import os
import re
import sys
import uuid
import asyncio
import io
import csv
import urllib.parse
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from datetime import datetime
from config import settings
from db import Database
from job_manager import JobManager
from daemon import BatchWorkerDaemon
from report_generator import ReportGenerator
from remediation_engine import RemediationEngine
from ai_provider import sanitize_cell_for_export
from sitemap_extractor import SitemapExtractor
from diagnostics_403 import Diagnostics403

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(title="Link Auditor V2 API")

class ReverseProxyAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Always allow health endpoints and CORS preflights without authentication
        if request.method == "OPTIONS" or request.url.path in ["/api/health", "/health"]:
            return await call_next(request)

        # Reverse proxy authentication check if AUTH_REQUIRED is enabled or PROXY_SECRET_VALUE is set
        if getattr(settings, 'AUTH_REQUIRED', False) or getattr(settings, 'PROXY_SECRET_VALUE', ''):
            # 1. Validate Proxy Secret if configured
            expected_secret = getattr(settings, 'PROXY_SECRET_VALUE', '')
            if expected_secret:
                secret_hdr = getattr(settings, 'PROXY_SECRET_HEADER', 'X-Proxy-Secret')
                provided_secret = request.headers.get(secret_hdr.lower())
                if not provided_secret or provided_secret != expected_secret:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Forbidden: Invalid or missing reverse proxy secret"}
                    )

            # 2. Validate User Identity header
            user_hdr = getattr(settings, 'USER_HEADER', 'X-Remote-User')
            user = request.headers.get(user_hdr.lower())
            if not user or not user.strip():
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: Missing authenticated user header"}
                )
            request.state.user = user.strip()

        return await call_next(request)

app.add_middleware(ReverseProxyAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("uploads", exist_ok=True)
os.makedirs("reports", exist_ok=True)

# Global instances
db = Database()
job_manager = JobManager(db)
daemon = BatchWorkerDaemon()
report_gen = ReportGenerator(db)
remediation_engine = RemediationEngine(db)

@app.on_event("startup")
async def startup_event():
    await db.connect()
    daemon.db = db
    asyncio.create_task(daemon.start())

@app.on_event("shutdown")
async def shutdown_event():
    daemon.active = False

@app.get("/api/health")
@app.get("/health")
async def health_check():
    db_status = "connected"
    try:
        await db.conn.execute("SELECT 1")
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    daemon_status = {
        "active": bool(daemon and daemon.active),
        "worker_id": getattr(daemon, "worker_id", "none"),
        "active_jobs": getattr(daemon, "active_jobs", 0),
        "configured_workers": getattr(daemon, "worker_count", getattr(settings, "WORKER_COUNT", 4))
    }

    status = "healthy" if db_status == "connected" and daemon_status["active"] else "degraded"
    
    return {
        "status": status,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "database": db_status,
        "daemon": daemon_status,
        "version": "2.0.0"
    }

@app.post("/api/admin/cleanup-artifacts")
async def cleanup_artifacts(days: int = None):
    deleted = await db.cleanup_stale_artifacts(days)
    return {"message": "Cleanup completed", "deleted_screenshots": deleted}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), mode: str = Form("url_status")):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".xlsx", ".csv"]:
        raise HTTPException(status_code=400, detail="Only .xlsx and .csv files allowed")
        
    upload_id = str(uuid.uuid4())
    filepath = f"uploads/{upload_id}_{file.filename}"
    
    with open(filepath, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
        
    sheets = await job_manager.parse_sheets(filepath)
    total_urls = sum(s.get("count", 0) for s in sheets)
    max_records = getattr(settings, "LINK_AUDIT_MAX_URL_RECORDS", 20000) if mode == "link_audit" else settings.MAX_URLS_PER_JOB
    if total_urls > max_records:
        try: os.remove(filepath)
        except OSError: pass
        raise HTTPException(
            status_code=400, 
            detail=f"File contains {total_urls} URLs, which exceeds the limit of {max_records} URLs per job."
        )

    await job_manager.create_upload(upload_id, file.filename, mode)
    return {"upload_id": upload_id, "filename": file.filename, "mode": mode, "sheets": sheets}

from pydantic import BaseModel
class StartJobsRequest(BaseModel):
    filepath: str
    mode: str
    sheets: list
    force_recheck: bool = False

@app.post("/api/upload/{upload_id}/start")
async def start_jobs(upload_id: str, req: StartJobsRequest):
    filepath = f"uploads/{upload_id}_{req.filepath}"
    sheets = await job_manager.parse_sheets(filepath)
    selected_counts = sum(s.get("count", 0) for s in sheets if not req.sheets or s.get("name") in req.sheets)
    max_records = getattr(settings, "LINK_AUDIT_MAX_URL_RECORDS", 20000) if req.mode == "link_audit" else settings.MAX_URLS_PER_JOB
    if selected_counts > max_records:
        raise HTTPException(
            status_code=400, 
            detail=f"Selected sheets contain {selected_counts} URLs, which exceeds the limit of {max_records} URLs per job."
        )

    await job_manager.create_jobs_for_sheets(upload_id, filepath, req.mode, req.sheets, req.force_recheck)
    return {"message": "Jobs created and queued successfully"}

@app.get("/api/uploads/{upload_id}/jobs")
async def get_upload_jobs(upload_id: str):
    jobs = await job_manager.get_jobs_for_upload(upload_id)
    return {"jobs": jobs}

@app.get("/api/jobs")
async def get_all_jobs():
    jobs = await job_manager.get_all_jobs()
    return {"jobs": jobs}

@app.get("/api/jobs/{job_id}/batches")
async def get_job_batches(job_id: str):
    batches = await job_manager.get_batches_for_job(job_id)
    return {"batches": batches}

@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    await job_manager.update_job_status(job_id, "PAUSED")
    return {"message": "Job paused"}

@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    await job_manager.update_job_status(job_id, "QUEUED")
    return {"message": "Job resumed"}

@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    await job_manager.update_job_status(job_id, "CANCELLED")
    return {"message": "Job cancelled"}

@app.get("/api/jobs/{job_id}/download")
async def download_report(job_id: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    # Get job
    jobs = await db.conn.execute("SELECT u.mode, j.status FROM jobs j JOIN uploads u ON j.upload_id = u.id WHERE j.id = ?", (job_id,))
    row = await jobs.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    
    mode, status = row
    
    if status in ["QUEUED", "RUNNING"]:
        raise HTTPException(status_code=400, detail="Cannot download report while job is still running. Please pause or cancel if you want a partial report.")
        
    is_partial = status in ["PARTIAL", "FAILED", "CANCELLED", "PAUSED"]
    prefix = "PARTIAL_" if is_partial else "FINAL_"
    
    os.makedirs("reports", exist_ok=True)
    output_file = f"reports/{prefix}report_{job_id}.xlsx"
    safe_reports_dir = os.path.realpath("reports")
    target_report = os.path.realpath(output_file)
    if not target_report.startswith(safe_reports_dir):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Generate on the fly if not exists
    if not os.path.exists(output_file):
        await report_gen.generate_report(job_id, mode, output_file)
        
    # Get the time the job was completed (or now)
    async with db.conn.execute("SELECT updated_at FROM jobs WHERE id = ?", (job_id,)) as cur:
        dt_row = await cur.fetchone()
        updated_at = dt_row[0] if dt_row and dt_row[0] else datetime.utcnow().isoformat() + "Z"
        
    prefix_name = "Page-API-Audit-Report" if mode == "api_response_audit" else "URLs-Audit-Report"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
        friendly_name = dt.strftime(f"{prefix_name}_%Y_%b_%d-%I_%M_%p") + ".xlsx"
    except:
        friendly_name = f"{prefix_name}_{job_id[:8]}.xlsx"
        
    return FileResponse(output_file, filename=friendly_name)

@app.get("/api/artifacts/api_response_audit/{job_id}/screenshots/{filename}")
async def get_screenshot(job_id: str, filename: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    # Verify job exists in database
    job_row = await db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,))
    if not await job_row.fetchone():
        raise HTTPException(status_code=404, detail="Job not found")

    # Strict filename validation: alphanumeric/underscore/hyphen with .png extension
    if not re.match(r'^[a-zA-Z0-9_\-]+\.png$', filename):
        raise HTTPException(status_code=400, detail="Invalid screenshot filename")

    # Verify screenshot is registered for this job to prevent cross-job asset harvesting
    res_row = await db.conn.execute("SELECT 1 FROM page_audit_results WHERE job_id = ? AND screenshot_path LIKE ?", (job_id, f"%{filename}"))
    if not await res_row.fetchone():
        raise HTTPException(status_code=404, detail="Screenshot not found for this audit")

    safe_dir = os.path.realpath(os.path.join(settings.ARTIFACTS_DIR, job_id, "screenshots"))
    target_file = os.path.realpath(os.path.join(safe_dir, filename))
    
    # Path traversal protection: target must be inside safe_dir and artifacts dir
    canonical_artifacts = os.path.realpath(settings.ARTIFACTS_DIR)
    if not target_file.startswith(safe_dir) or not target_file.startswith(canonical_artifacts):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(target_file):
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return FileResponse(target_file, media_type="image/png")

@app.get("/api/jobs/{job_id}/api-audit-results")
async def get_api_audit_results(job_id: str):
    pages = await db.get_page_audit_results(job_id)
    for p in pages:
        if p.get("screenshot_path"):
            fn = os.path.basename(p["screenshot_path"])
            p["screenshot_url"] = f"/api/artifacts/api_response_audit/{job_id}/screenshots/{fn}"
        else:
            p["screenshot_url"] = None
    return {"pages": pages}

@app.get("/api/jobs/{job_id}/stats")
async def get_job_stats(job_id: str):
    # Fetch mode
    async with db.conn.execute("SELECT u.mode, j.created_at, j.updated_at, j.status FROM jobs j JOIN uploads u ON j.upload_id = u.id WHERE j.id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
        if not row: raise HTTPException(404, "Job not found")
        mode, created_at, updated_at, status = row
        
    if mode == "api_response_audit":
        stats = await db.get_api_audit_stats(job_id)
        stats['mode'] = mode
        stats['created_at'] = created_at
        stats['updated_at'] = updated_at
        stats['status'] = status
        return stats

    if mode == "link_audit":
        stats = await db.get_link_audit_stats(job_id)
        if daemon and hasattr(daemon, "link_validator") and daemon.link_validator:
            live = daemon.link_validator.get_live_stats(job_id)
            if live:
                stats.update(live)
        stats['mode'] = mode
        stats['created_at'] = created_at
        stats['updated_at'] = updated_at
        stats['status'] = status
        return stats

    table = "image_results" if mode == "image_audit" else "url_results"
    
    # Calculate stats
    sql = f"""
        SELECT 
            COUNT(*) as total_extracted,
            COUNT(DISTINCT e.normalized_url) as total_unique,
            SUM(CASE WHEN r.final_status_code IS NOT NULL THEN 1 ELSE 0 END) as total_checked,
            SUM(CASE WHEN r.status = 'Working' THEN 1 ELSE 0 END) as working,
            SUM(CASE WHEN r.status LIKE '%Broken%' THEN 1 ELSE 0 END) as broken,
            SUM(CASE WHEN r.status LIKE '%Redirect%' THEN 1 ELSE 0 END) as redirects,
            SUM(CASE WHEN r.status = 'Forbidden' THEN 1 ELSE 0 END) as forbidden,
            SUM(CASE WHEN r.status = 'Server Error' THEN 1 ELSE 0 END) as server_errors,
            SUM(CASE WHEN r.status IN ('Timeout', 'Connection Error', 'DNS Error', 'Network Error') THEN 1 ELSE 0 END) as network_failures,
            SUM(CASE WHEN r.final_status_code IS NULL THEN 1 ELSE 0 END) as pending
        FROM extracted_links e
        LEFT JOIN {table} r ON e.normalized_url = r.normalized_url
        WHERE e.job_id = ?
    """
    
    async with db.conn.execute(sql, (job_id,)) as cur:
        cols = [desc[0] for desc in cur.description]
        stats = dict(zip(cols, await cur.fetchone()))
        
    stats['mode'] = mode
    stats['created_at'] = created_at
    stats['updated_at'] = updated_at
    stats['status'] = status
    return stats

@app.get("/api/jobs/{job_id}/non-200-results")
async def get_job_non_200_results(job_id: str):
    results = await db.get_job_non_200_destinations(job_id)
    return {"non_200_results": results}

@app.get("/api/debug")
async def get_debug():
    if not daemon: return {"error": "no daemon"}
    return {
        "active_jobs": daemon.active_jobs,
        "http_queue_size": daemon.http_queue.qsize(),
        "db_queue_size": daemon.db_queue.qsize(),
        "tuner_active": daemon.checker.tuner.active_requests,
        "tuner_concurrency": daemon.checker.tuner.current_concurrency
    }

@app.get("/api/db-metrics")
async def get_db_metrics():
    from db_metrics import db_metrics
    return db_metrics.get_stats()

@app.get("/api/overview")
async def get_overview():
    stats = await db.get_overview_stats()
    stats["timestamp"] = datetime.utcnow().isoformat() + "Z"
    return stats

@app.get("/api/jobs/{job_id}/records")
async def get_job_records(
    job_id: str,
    page: int = 1,
    page_size: int = 50,
    q: str = "",
    status: str = ""
):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    
    async with db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")

    return await db.get_job_records_paginated(
        job_id=job_id,
        page=page,
        page_size=page_size,
        query=q,
        status_filter=status
    )

class RemediationStatusUpdate(BaseModel):
    status: str
    review_notes: Optional[str] = None

@app.post("/api/jobs/{job_id}/remediations/generate")
async def generate_job_remediations(job_id: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    async with db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")
            
    remediations = await remediation_engine.analyze_job(job_id)
    count = await db.upsert_remediations(remediations)
    return {
        "status": "ok",
        "job_id": job_id,
        "generated_count": count
    }

@app.get("/api/jobs/{job_id}/remediations")
async def get_job_remediations(
    job_id: str,
    page: int = 1,
    page_size: int = 50,
    severity: Optional[str] = None,
    priority: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    sort_by: str = "priority_score",
    sort_dir: str = "desc"
):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    if page < 1:
        raise HTTPException(status_code=400, detail="page must be greater than or equal to 1")
    if page_size < 1 or page_size > 500:
        raise HTTPException(status_code=400, detail="page_size must be between 1 and 500")
    if q and len(q) > 500:
        raise HTTPException(status_code=400, detail="Search query exceeds maximum limit of 500 characters")

    async with db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")

    return await db.get_remediations_paginated(
        job_id=job_id,
        page=page,
        page_size=page_size,
        severity=severity,
        priority=priority,
        category=category,
        status=status,
        search=q,
        sort_by=sort_by,
        sort_dir=sort_dir
    )

@app.get("/api/jobs/{job_id}/remediations/summary")
async def get_job_remediation_summary(job_id: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    async with db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")

    return await db.get_remediation_summary(job_id)

@app.get("/api/jobs/{job_id}/remediations/export")
async def export_job_remediations(job_id: str, format: str = "csv"):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    fmt = format.lower().strip()
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats: csv, xlsx")

    async with db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")

    rems = await db.get_all_remediations_for_job(job_id)
    if not rems:
        rems = await remediation_engine.analyze_job(job_id)
        if rems:
            await db.upsert_remediations(rems)

    if fmt == "xlsx":
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Remediations"
        report_gen._write_remediations_sheet(ws, rems)
        
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="remediations_{job_id}.xlsx"'}
        )
    else:
        output = io.StringIO()
        writer = csv.writer(output)
        headers = [
            "Remediation ID", "Priority", "Score", "Severity", "Category",
            "Status", "Target URL", "Sources Count", "Title",
            "Recommended Action", "Impact", "Priority Rationale", "Review Notes"
        ]
        writer.writerow(headers)
        for r in rems:
            writer.writerow([
                sanitize_cell_for_export(r.get("remediation_id")),
                sanitize_cell_for_export(r.get("priority")),
                r.get("priority_score", 0),
                sanitize_cell_for_export(r.get("severity")),
                sanitize_cell_for_export(r.get("category")),
                sanitize_cell_for_export(r.get("status")),
                sanitize_cell_for_export(r.get("target_url")),
                r.get("source_count", 1),
                sanitize_cell_for_export(r.get("title")),
                sanitize_cell_for_export(r.get("recommended_action")),
                sanitize_cell_for_export(r.get("impact")),
                sanitize_cell_for_export(r.get("priority_rationale")),
                sanitize_cell_for_export(r.get("review_notes") or "")
            ])
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="remediations_{job_id}.csv"'}
        )

@app.get("/api/jobs/{job_id}/remediations/{remediation_id}")
async def get_job_remediation_detail(job_id: str, remediation_id: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    if not re.match(r'^[a-zA-Z0-9_\-]+$', remediation_id):
        raise HTTPException(status_code=400, detail="Invalid remediation ID format")
        
    async with db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")

    item = await db.get_remediation_by_id(job_id, remediation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Remediation not found")
    return item

@app.patch("/api/jobs/{job_id}/remediations/{remediation_id}")
async def update_job_remediation(job_id: str, remediation_id: str, body: RemediationStatusUpdate):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    if not re.match(r'^[a-zA-Z0-9_\-]+$', remediation_id):
        raise HTTPException(status_code=400, detail="Invalid remediation ID format")
        
    allowed_statuses = {"OPEN", "IN_PROGRESS", "RESOLVED", "DISMISSED", "NEEDS_REVIEW"}
    status_upper = body.status.strip().upper()
    if status_upper not in allowed_statuses:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status '{body.status}'. Must be one of {sorted(list(allowed_statuses))}"
        )
    if body.review_notes and len(body.review_notes) > 5000:
        raise HTTPException(status_code=400, detail="Review notes exceed maximum limit of 5000 characters")

    async with db.conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")

    updated = await db.update_remediation_status(
        job_id=job_id,
        remediation_id=remediation_id,
        status=status_upper,
        review_notes=body.review_notes
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Remediation not found")

    item = await db.get_remediation_by_id(job_id, remediation_id)
    return {"status": "ok", "remediation": item}

# =========================================================================
# Phase 10.12 Sitemap to Crawl & 403 Diagnostics Endpoints
# =========================================================================

class SitemapImportJsonRequest(BaseModel):
    urls: Optional[List[str]] = None
    sitemap_url: Optional[str] = None
    title: Optional[str] = "Sitemap Import"

class SitemapUrlSelectRequest(BaseModel):
    url_ids: List[int]
    selected: bool = True

class SitemapSelectAllRequest(BaseModel):
    eligible_only: bool = True
    selected: bool = True

class SitemapCrawlRequest(BaseModel):
    mode: str = "link_audit"
    force_recheck: bool = False
    selected_only: bool = True

async def _execute_sitemap_extraction(import_id: str, urls: List[str], file_bytes: Optional[bytes] = None, filename: str = ""):
    allow_local = getattr(settings, "ALLOW_LOCAL_TESTING", False)
    extractor = SitemapExtractor(
        max_depth=getattr(settings, "SITEMAP_MAX_DEPTH", 5),
        max_response_size=getattr(settings, "SITEMAP_MAX_RESPONSE_SIZE", 10 * 1024 * 1024),
        max_urls=getattr(settings, "SITEMAP_MAX_URLS", 50000),
        timeout=getattr(settings, "SITEMAP_TIMEOUT", 15.0),
        concurrency=getattr(settings, "SITEMAP_CONCURRENCY", 5),
        allow_local=allow_local
    )
    try:
        await db.update_sitemap_import(import_id, status="EXTRACTING")

        async def on_progress(sources_count, urls_count, dups, rejs, errs):
            await db.update_sitemap_import(
                import_id,
                total_sitemaps=sources_count,
                processed_sitemaps=sources_count,
                total_urls=urls_count + dups + rejs,
                unique_urls=urls_count,
                duplicate_urls=dups,
                rejected_urls=rejs,
                error_count=errs
            )

        if file_bytes:
            res = await extractor.extract_from_uploaded_bytes(file_bytes, filename=filename or "sitemap.xml")
        else:
            res = await extractor.extract_all(urls, progress_callback=on_progress)

        # 1. Insert sitemap sources
        for src in res.get("sitemap_sources", []):
            await db.add_sitemap_source(
                import_id=import_id,
                sitemap_url=src.get("sitemap_url", ""),
                parent_sitemap_url=src.get("parent_sitemap_url", ""),
                depth=src.get("depth", 0),
                status=src.get("status", "PARSED"),
                http_status=src.get("http_status"),
                urls_found=src.get("urls_found", 0),
                classification=src.get("classification", "VALID"),
                error_message=src.get("error_message", "")
            )

        # 2. Insert extracted URLs in batches
        all_urls = res.get("unique_urls", [])
        batch_size = 500
        for i in range(0, len(all_urls), batch_size):
            chunk = all_urls[i:i + batch_size]
            await db.add_extracted_urls_batch(import_id, chunk)

        # 3. Record diagnostics
        for d in res.get("diagnostics", []):
            diag = d.get("diagnostic", {})
            await db.record_request_diagnostic(
                import_id=import_id,
                url=d.get("url", ""),
                stage=d.get("stage", "sitemap_download"),
                classification=diag.get("classification", "HTTP_403_UNCLASSIFIED"),
                evidence_type=diag.get("evidence_type", "VERIFIED_EVIDENCE"),
                what_happened=diag.get("what_happened", ""),
                where_it_happened=diag.get("where_it_happened", ""),
                what_verified=diag.get("what_verified", ""),
                recommended_action=diag.get("recommended_action", ""),
                retry_appropriate=diag.get("retry_appropriate", False),
                safe_headers=diag.get("safe_headers", {}),
                details=diag.get("body_indicators", {})
            )

        # 4. Final status update
        total_unique = len(all_urls)
        errors = res.get("errors", [])
        final_status = "COMPLETED" if len(errors) == 0 else ("PARTIAL" if total_unique > 0 else "FAILED")
        err_msg = "; ".join(errors[:5]) if errors else ""

        await db.update_sitemap_import(
            import_id,
            status=final_status,
            total_sitemaps=len(res.get("sitemap_sources", [])),
            processed_sitemaps=len(res.get("sitemap_sources", [])),
            total_urls=total_unique + res.get("duplicate_count", 0) + res.get("rejected_count", 0),
            unique_urls=total_unique,
            duplicate_urls=res.get("duplicate_count", 0),
            rejected_urls=res.get("rejected_count", 0),
            error_count=len(errors),
            error_message=err_msg
        )
    except Exception as e:
        await db.update_sitemap_import(
            import_id,
            status="FAILED",
            error_message=str(e),
            error_count=1
        )
    finally:
        await extractor.close()

@app.post("/api/sitemaps/import")
async def import_sitemaps(
    request: Request,
    file: Optional[UploadFile] = File(None),
    urls: Optional[str] = Form(None),
    title: Optional[str] = Form("Sitemap Import")
):
    import_id = str(uuid.uuid4())
    sitemap_urls = []
    file_bytes = None
    filename = ""

    # Check content-type to see if application/json was sent
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                title = body.get("title") or title
                if body.get("urls") and isinstance(body["urls"], list):
                    sitemap_urls = [str(u).strip() for u in body["urls"] if str(u).strip()]
                elif body.get("sitemap_url"):
                    sitemap_urls = [str(body["sitemap_url"]).strip()]
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    else:
        if file:
            filename = file.filename or "uploaded_sitemap.xml"
            ext = os.path.splitext(filename)[1].lower()
            if ext not in [".xml", ".gz"]:
                if not filename.lower().endswith(".xml.gz"):
                    raise HTTPException(status_code=400, detail="Only .xml and .xml.gz sitemap files are allowed")
            file_bytes = await file.read()
            if len(file_bytes) > getattr(settings, "SITEMAP_MAX_RESPONSE_SIZE", 10 * 1024 * 1024):
                raise HTTPException(status_code=400, detail="Uploaded file exceeds 10 MB limit")
        elif urls:
            sitemap_urls = [line.strip() for line in urls.splitlines() if line.strip()]

    if not file_bytes and not sitemap_urls:
        raise HTTPException(status_code=400, detail="Please provide at least one sitemap URL or upload an XML sitemap file")

    # Validate URLs scheme and basic formatting
    if sitemap_urls:
        for u in sitemap_urls:
            parsed = urllib.parse.urlparse(u)
            if parsed.scheme.lower() not in ["http", "https"]:
                raise HTTPException(status_code=400, detail=f"Invalid URL scheme in '{u}'. Only http and https are supported.")

    await db.create_sitemap_import(import_id, title=title or "Sitemap Import")

    # Launch asynchronous background extraction
    asyncio.create_task(_execute_sitemap_extraction(import_id, sitemap_urls, file_bytes=file_bytes, filename=filename))

    return {
        "status": "ok",
        "import_id": import_id,
        "title": title,
        "message": "Sitemap import started"
    }

@app.get("/api/sitemaps/{import_id}/status")
async def get_sitemap_status(import_id: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', import_id):
        raise HTTPException(status_code=400, detail="Invalid import ID format")
    item = await db.get_sitemap_import(import_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sitemap import not found")
    return item

@app.get("/api/sitemaps/{import_id}/sources")
async def get_sitemap_sources(import_id: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', import_id):
        raise HTTPException(status_code=400, detail="Invalid import ID format")
    item = await db.get_sitemap_import(import_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sitemap import not found")
    sources = await db.get_sitemap_sources(import_id)
    return {"import_id": import_id, "sources": sources}

@app.get("/api/sitemaps/{import_id}/urls")
async def get_sitemap_urls(
    import_id: str,
    page: int = 1,
    page_size: int = 50,
    search: str = "",
    status: str = "",
    robots_allowed: Optional[bool] = None
):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', import_id):
        raise HTTPException(status_code=400, detail="Invalid import ID format")
    item = await db.get_sitemap_import(import_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sitemap import not found")
    res = await db.get_extracted_urls(
        import_id=import_id,
        page=page,
        page_size=page_size,
        search=search,
        filter_status=status,
        filter_robots=robots_allowed
    )
    return res

@app.post("/api/sitemaps/{import_id}/urls/select")
async def select_sitemap_urls(import_id: str, body: SitemapUrlSelectRequest):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', import_id):
        raise HTTPException(status_code=400, detail="Invalid import ID format")
    item = await db.get_sitemap_import(import_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sitemap import not found")
    await db.toggle_url_selection(import_id, body.url_ids, body.selected)
    return {"status": "ok"}

@app.post("/api/sitemaps/{import_id}/urls/select-all")
async def select_all_sitemap_urls(import_id: str, body: SitemapSelectAllRequest):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', import_id):
        raise HTTPException(status_code=400, detail="Invalid import ID format")
    item = await db.get_sitemap_import(import_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sitemap import not found")
    await db.select_all_urls(import_id, eligible_only=body.eligible_only, selected=body.selected)
    return {"status": "ok"}

@app.post("/api/sitemaps/{import_id}/crawl")
async def start_sitemap_crawl(import_id: str, body: SitemapCrawlRequest):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', import_id):
        raise HTTPException(status_code=400, detail="Invalid import ID format")
    item = await db.get_sitemap_import(import_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sitemap import not found")
        
    try:
        job_id = await job_manager.create_job_from_sitemap(
            sitemap_import_id=import_id,
            mode=body.mode,
            force_recheck=body.force_recheck,
            selected_only=body.selected_only
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "ok",
        "job_id": job_id,
        "import_id": import_id,
        "mode": body.mode,
        "message": "Crawl job successfully created and queued in daemon pipeline"
    }

@app.get("/api/sitemaps/{import_id}/diagnostics")
async def get_sitemap_diagnostics(import_id: str):
    if not re.match(r'^[a-zA-Z0-9_\-]+$', import_id):
        raise HTTPException(status_code=400, detail="Invalid import ID format")
    item = await db.get_sitemap_import(import_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sitemap import not found")
    diags = await db.get_request_diagnostics(import_id=import_id)
    return {"import_id": import_id, "diagnostics": diags}

@app.get("/api/diagnostics/403")
async def get_all_403_diagnostics(import_id: Optional[str] = None, job_id: Optional[str] = None, limit: int = 100):
    diags = await db.get_request_diagnostics(import_id=import_id, job_id=job_id, limit=limit)
    return {"diagnostics": diags}

frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
os.makedirs(frontend_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
