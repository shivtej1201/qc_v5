import os
import uuid
import pandas as pd
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from db import Database
from url_normalizer import URLNormalizer
from ai_provider import sanitize_cell_for_export

# Corporate Excel Style Constants
HEADER_FONT = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
CELL_BORDER = Border(
    left=Side(style='thin', color="D9D9D9"),
    right=Side(style='thin', color="D9D9D9"),
    top=Side(style='thin', color="D9D9D9"),
    bottom=Side(style='thin', color="D9D9D9")
)
FONT_DEFAULT = Font(name="Calibri", size=10, color="000000")
FONT_LINK = Font(name="Calibri", size=10, color="0563C1", underline="single")

# Soft Conditional Formatting Palette
FILL_2XX = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
FONT_2XX = Font(name="Calibri", size=10, color="276A3C", bold=True)

FILL_3XX = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
FONT_3XX = Font(name="Calibri", size=10, color="8A6D3B", bold=True)

FILL_4XX = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
FONT_4XX = Font(name="Calibri", size=10, color="A61C1C", bold=True)

FILL_5XX = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")
FONT_5XX = Font(name="Calibri", size=10, color="900C3F", bold=True)

FILL_BLOCKED = PatternFill(start_color="EAECEE", end_color="EAECEE", fill_type="solid")
FONT_BLOCKED = Font(name="Calibri", size=10, color="5D6D7E", bold=True)

PRIORITY_COLORS = {
    "P1": (PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid"), Font(name="Calibri", size=10, color="900C3F", bold=True)),
    "P2": (PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid"), Font(name="Calibri", size=10, color="A61C1C", bold=True)),
    "P3": (PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"), Font(name="Calibri", size=10, color="8A6D3B", bold=True)),
    "P4": (PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid"), Font(name="Calibri", size=10, color="1B365D", bold=True)),
}

SEVERITY_COLORS = {
    "CRITICAL": (PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid"), Font(name="Calibri", size=10, color="900C3F", bold=True)),
    "HIGH": (PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid"), Font(name="Calibri", size=10, color="A61C1C", bold=True)),
    "MEDIUM": (PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"), Font(name="Calibri", size=10, color="8A6D3B", bold=True)),
    "LOW": (PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid"), Font(name="Calibri", size=10, color="1B365D", bold=True)),
}

STATUS_COLORS = {
    "FOUND": (PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid"), Font(name="Calibri", size=10, color="276A3C", bold=True)),
    "RESOLVED": (PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid"), Font(name="Calibri", size=10, color="276A3C", bold=True)),
    "ACCEPTED": (PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid"), Font(name="Calibri", size=10, color="276A3C", bold=True)),
    "NOT_FOUND": (PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid"), Font(name="Calibri", size=10, color="A61C1C", bold=True)),
    "API_ERROR": (PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid"), Font(name="Calibri", size=10, color="900C3F", bold=True)),
    "PAGE_ERROR": (PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid"), Font(name="Calibri", size=10, color="900C3F", bold=True)),
    "TIMEOUT": (PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid"), Font(name="Calibri", size=10, color="900C3F", bold=True)),
    "SKIPPED": (PatternFill(start_color="EAECEE", end_color="EAECEE", fill_type="solid"), Font(name="Calibri", size=10, color="5D6D7E", bold=True)),
    "INCONCLUSIVE": (PatternFill(start_color="EAECEE", end_color="EAECEE", fill_type="solid"), Font(name="Calibri", size=10, color="5D6D7E", bold=True)),
    "PENDING": (PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"), Font(name="Calibri", size=10, color="8A6D3B", bold=True)),
    "IN_PROGRESS": (PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid"), Font(name="Calibri", size=10, color="1B365D", bold=True)),
    "DISMISSED": (PatternFill(start_color="EAECEE", end_color="EAECEE", fill_type="solid"), Font(name="Calibri", size=10, color="5D6D7E", bold=True)),
}

def _configure_sheet_print_setup(worksheet, has_header: bool = True):
    worksheet.page_setup.orientation = worksheet.ORIENTATION_LANDSCAPE
    worksheet.page_setup.paperSize = worksheet.PAPERSIZE_A4
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    if has_header:
        worksheet.print_title_rows = '1:1'
    if worksheet.views.sheetView:
        worksheet.views.sheetView[0].showGridLines = True

class ReportGenerator:
    def __init__(self, db: Database):
        self.db = db

    async def generate_report(self, job_id: str, mode: str, output_file: str):
        if mode == "api_response_audit":
            await self.generate_api_audit_report(job_id, output_file)
            return

        if mode == "link_audit":
            await self.generate_link_audit_report(job_id, output_file)
            return

        temp_file = f"{output_file}.{os.getpid()}_{uuid.uuid4().hex[:6]}.tmp.xlsx"
        try:
            await self._do_generate_report(job_id, mode, temp_file)
            os.replace(temp_file, output_file)
        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    async def _do_generate_report(self, job_id: str, mode: str, output_file: str):
        if mode == "api_response_audit":
            await self.generate_api_audit_report(job_id, output_file)
            return

        if mode == "link_audit":
            await self._do_generate_link_audit_report(job_id, output_file)
            return

        if mode == "url_status":
            await self._write_url_status_report(job_id, output_file)
            return

        # For link_audit and image_audit
        sql = "SELECT * FROM extracted_links WHERE job_id = ?"
        async with self.db.conn.execute(sql, (job_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            extracted = [dict(zip(cols, row)) for row in await cursor.fetchall()]

        if not extracted:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Image Audit" if mode == "image_audit" else "Link Audit"
            self._write_table_sheet(ws, pd.DataFrame())
            wb.save(output_file)
            return

        report_data = []
        if mode == "image_audit":
            # Fetch image results in bulk
            res_map = {}
            unique_norms = list({l['normalized_url'] for l in extracted if l.get('normalized_url')})
            if unique_norms:
                chunk_size = 500
                for i in range(0, len(unique_norms), chunk_size):
                    chunk = unique_norms[i:i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    q = f"SELECT * FROM image_results WHERE normalized_url IN ({placeholders})"
                    async with self.db.conn.execute(q, chunk) as cur:
                        cols = [desc[0] for desc in cur.description]
                        for row in await cur.fetchall():
                            r_dict = dict(zip(cols, row))
                            res_map[r_dict["normalized_url"]] = r_dict
                    
            for link in extracted:
                norm = link['normalized_url']
                cached = res_map.get(norm, {})
                source_url = link.get("source_url", "N/A")
                lang = URLNormalizer.detect_language(source_url) if source_url != "N/A" else "en"
                
                report_data.append({
                    "source_page_url": source_url,
                    "language": lang,
                    "image_name": link.get("image_name", ""),
                    "image_url": link.get("original_url", ""),
                    "image_format": cached.get("image_format", "UNKNOWN"),
                    "image_size_kb": cached.get("image_size_kb", ""),
                    "loading_type": link.get("loading_type", "unknown"),
                    "is_dam_asset": cached.get("is_dam_asset", False),
                    "source_platform": cached.get("source_platform", "Unknown"),
                    "http_status_code": cached.get("final_status_code", ""),
                    "remarks": cached.get("status", "Pending Validation"),
                    "extracted_timestamp": link.get("extracted_timestamp", ""),
                    "checked_timestamp": cached.get("checked_at", "")
                })
        else:
            # link_audit bulk lookups
            res_map = {}
            unique_norms = list({l['normalized_url'] for l in extracted if l.get('normalized_url')})
            if unique_norms:
                chunk_size = 500
                for i in range(0, len(unique_norms), chunk_size):
                    chunk = unique_norms[i:i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    q = f"SELECT * FROM url_results WHERE normalized_url IN ({placeholders})"
                    async with self.db.conn.execute(q, chunk) as cur:
                        cols = [desc[0] for desc in cur.description]
                        for row in await cur.fetchall():
                            r_dict = dict(zip(cols, row))
                            res_map[r_dict["normalized_url"]] = r_dict

            source_class_map = {}
            unique_sources = list({l.get("source_url") for l in extracted if l.get("source_url") and l.get("source_url") != "N/A"})
            if unique_sources:
                chunk_size = 500
                for i in range(0, len(unique_sources), chunk_size):
                    chunk = unique_sources[i:i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    q = f"SELECT * FROM source_classifications WHERE source_url IN ({placeholders})"
                    async with self.db.conn.execute(q, chunk) as cur:
                        cols = [desc[0] for desc in cur.description]
                        for row in await cur.fetchall():
                            sc_dict = dict(zip(cols, row))
                            source_class_map[sc_dict["source_url"]] = sc_dict
                    
            for i, link in enumerate(extracted, 1):
                norm = link['normalized_url']
                cached = res_map.get(norm, {})
                source_url = link.get("source_url", "N/A")
                source_class = source_class_map.get(source_url, {})
                lang = URLNormalizer.detect_language(source_url) if source_url != "N/A" else "en"
                
                report_data.append({
                    "Sr.no": i,
                    "source_page_url": source_url,
                    "language": lang,
                    "component_name": link.get("component_name", "Unknown"),
                    "anchor_text": link.get("anchor_text", "N/A"),
                    "broken_destination_url": link.get("original_url", ""),
                    "http_status_code": cached.get("final_status_code", ""),
                    "remarks": cached.get("status", "Pending Validation"),
                    "page_type": source_class.get("page_type") or "NEEDS_REVIEW",
                    "business": source_class.get("business") or "NEEDS_REVIEW",
                    "extracted_timestamp": link.get("extracted_timestamp", ""),
                    "checked_timestamp": cached.get("checked_at", "")
                })

        df = pd.DataFrame(report_data)
        sheet_name = "Image Audit" if mode == "image_audit" else "Link Audit"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name
        self._write_table_sheet(ws, df)
        wb.save(output_file)

    URL_STATUS_EXPORT_COLUMNS = [
        "row_no", "original_url", "normalized_url", "item_state", "outcome", "validated", "status",
        "initial_status_code", "final_status_code", "final_url", "redirect_count", "error_type",
        "error_message", "validation_source", "duplicate_of_row", "attempts", "response_time_ms",
        "content_type", "check_method", "checked_at", "severity", "recommended_action",
    ]

    @staticmethod
    def url_status_row(item: dict, cols) -> list:
        """One export row; severity/recommended_action are derived from the recorded outcome."""
        from status_classifier import outcome_action
        sev, act = ("", "") if item.get("item_state") != "DONE" else outcome_action(
            item.get("outcome") or "", item.get("final_status_code"), item.get("initial_status_code"),
            item.get("redirect_count") or 0)
        if item.get("item_state") != "DONE":
            sev, act = "HIGH", "No result yet (PENDING). Resume the job."
        d = dict(item, severity=sev, recommended_action=act)
        return [sanitize_cell_for_export(d.get(c)) for c in cols]

    async def _write_url_status_report(self, job_id: str, output_file: str):
        if await self.db.job_has_url_items(job_id):
            return await self._write_url_status_ledger_report(job_id, output_file)
        return await self._write_url_status_legacy_report(job_id, output_file)

    async def _write_url_status_ledger_report(self, job_id: str, output_file: str):
        """One row per SUBMITTED URL, in submission order (duplicates and invalid entries included,
        each with its own result or explicit reason). Streams rows: bounded memory at 125K+."""
        cols = self.URL_STATUS_EXPORT_COLUMNS
        counts = await self.db.get_url_status_counts(job_id) or {}
        workbook = openpyxl.Workbook(write_only=True)
        ws = workbook.create_sheet(title="URL Status")
        ws.append(cols)
        n = 0
        sev_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        async for item in self.db.iter_job_url_items(job_id):
            row = self.url_status_row(item, cols)
            ws.append(row)
            sev = row[cols.index("severity")]
            if sev in sev_counts:
                sev_counts[sev] += 1
            n += 1
        summary = workbook.create_sheet(title="Summary")
        summary.append(["Metric", "Value"])
        for key in ("total_urls", "processed_urls", "pending_urls", "successfully_validated_urls",
                    "failed_validations", "duplicate_urls", "invalid_urls", "unique_urls",
                    "new_http_checks", "reused_from_cache"):
            summary.append([key, counts.get(key, 0)])
        summary.append(["rows_in_url_status_sheet", n])
        summary.append(["reconciled (rows == total_urls)", "PASS" if n == counts.get("total_urls") else "FAIL"])
        summary.append([])
        summary.append(["Issues by severity", "URLs"])
        for k in ("HIGH", "MEDIUM", "LOW"):
            summary.append([k, sev_counts[k]])
        summary.append([])
        summary.append(["Outcome", "URLs"])
        for oc, cnt in sorted((counts.get("by_outcome") or {}).items()):
            summary.append([oc, cnt])
        workbook.save(output_file)

    async def _write_url_status_legacy_report(self, job_id: str, output_file: str):
        import json

        url_columns = [
            row[1] for row in await (await self.db.conn.execute("PRAGMA table_info(url_results)")).fetchall()
        ]
        temp_table = f"report_urls_{uuid.uuid4().hex}"
        await self.db.conn.execute(f"CREATE TEMP TABLE {temp_table} (normalized_url TEXT PRIMARY KEY)")
        workbook = openpyxl.Workbook(write_only=True)
        worksheet = workbook.create_sheet(title="URL Status")
        worksheet.append(url_columns or ["normalized_url", "final_status_code", "status", "error_type", "checked_at"])
        try:
            async with self.db.conn.execute("SELECT input_file FROM batches WHERE job_id = ?", (job_id,)) as cursor:
                async for (input_file,) in cursor:
                    try:
                        with open(input_file, "r", encoding="utf-8") as source:
                            urls = json.load(source)
                    except (OSError, ValueError):
                        continue

                    normalized = {
                        URLNormalizer.normalize(url)
                        for url in urls
                        if URLNormalizer.is_safe_url(url)
                    }
                    if normalized:
                        await self.db.conn.executemany(
                            f"INSERT OR IGNORE INTO {temp_table} (normalized_url) VALUES (?)",
                            [(url,) for url in normalized],
                        )

            query = f"SELECT r.* FROM url_results r JOIN {temp_table} j ON j.normalized_url = r.normalized_url"
            async with self.db.conn.execute(query) as cursor:
                async for row in cursor:
                    worksheet.append([sanitize_cell_for_export(value) for value in row])
            workbook.save(output_file)
        finally:
            await self.db.conn.execute(f"DROP TABLE IF EXISTS {temp_table}")

    async def generate_link_audit_report(self, job_id: str, output_file: str):
        temp_file = f"{output_file}.{os.getpid()}_{uuid.uuid4().hex[:6]}.tmp.xlsx"
        try:
            await self._do_generate_link_audit_report(job_id, temp_file)
            os.replace(temp_file, output_file)
        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    async def _do_generate_link_audit_report(self, job_id: str, output_file: str):
        # 1. Fetch extracted links
        sql = "SELECT * FROM extracted_links WHERE job_id = ? ORDER BY id ASC"
        async with self.db.conn.execute(sql, (job_id,)) as cursor:
            cols = [desc[0] for desc in cursor.description]
            extracted = [dict(zip(cols, row)) for row in await cursor.fetchall()]

        # 2. Fetch destination validation results
        dest_list = await self.db.get_job_destinations(job_id)
        dest_map = {d["normalized_url"]: d for d in dest_list}

        # Fallback to cached url_results in bulk if not present in job_destinations
        missing_norms = list({l['normalized_url'] for l in extracted if l.get('normalized_url') and l['normalized_url'] not in dest_map})
        if missing_norms:
            chunk_size = 500
            for i in range(0, len(missing_norms), chunk_size):
                chunk = missing_norms[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                q = f"SELECT * FROM url_results WHERE normalized_url IN ({placeholders})"
                async with self.db.conn.execute(q, chunk) as cur:
                    cols = [desc[0] for desc in cur.description]
                    for row in await cur.fetchall():
                        r_dict = dict(zip(cols, row))
                        dest_map[r_dict["normalized_url"]] = r_dict

        # 3. Fetch source classifications in bulk
        source_urls = list({l.get("source_url") for l in extracted if l.get("source_url") and l.get("source_url") != "N/A"})
        source_class_map = {}
        if source_urls:
            chunk_size = 500
            for i in range(0, len(source_urls), chunk_size):
                chunk = source_urls[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                q = f"SELECT * FROM source_classifications WHERE source_url IN ({placeholders})"
                async with self.db.conn.execute(q, chunk) as cur:
                    cols = [desc[0] for desc in cur.description]
                    for row in await cur.fetchall():
                        sc_dict = dict(zip(cols, row))
                        source_class_map[sc_dict["source_url"]] = sc_dict

        if not extracted:
            wb = openpyxl.Workbook()
            ws_pivot = wb.active
            ws_pivot.title = "Pivot Summary"
            ws_pivot.cell(row=1, column=1, value="No link data extracted for this job.")
            ws_non200 = wb.create_sheet(title="Non-200 Responses")
            ws_non200.append([
                "Sr.no", "destination_url", "http_status_code", "status_category",
                "affected_relationships_count", "affected_sources_count", "sample_source_url",
                "source_page_type", "business", "redirect_target", "response_time_ms", "error_reason"
            ])
            ws_links = wb.create_sheet(title="Link Audit")
            ws_links.append([
                "Sr.no", "source_page_url", "language", "component_name", "anchor_text",
                "broken_destination_url", "http_status_code", "remarks", "page_type",
                "business", "extracted_timestamp", "checked_timestamp"
            ])
            wb.save(output_file)
            return

        def get_dest_category(dest_record):
            if not dest_record:
                return "Other / Inconclusive"
            err = str(dest_record.get("error_type") or "").strip()
            stat = str(dest_record.get("status") or "").strip()
            code = dest_record.get("final_status_code")
            
            if "Blocked" in err or "Blocked" in stat or "SSRF" in err or "SSRF" in stat:
                return "Blocked / Skipped"
            if "Timeout" in err or "Timeout" in stat or code == "TIMEOUT":
                return "Timeout"
            if any(k in err for k in ["DNS Error", "Connection Error", "Network Error", "SSL Error"]) or any(k in stat for k in ["DNS", "Connection"]):
                return "DNS / Connection Error"
                
            num_code = None
            if isinstance(code, int):
                num_code = code
            elif isinstance(code, str) and str(code).isdigit():
                num_code = int(code)
                
            if num_code is not None:
                if 200 <= num_code <= 299:
                    return "HTTP 200 (Success)"
                elif 300 <= num_code <= 399:
                    return "HTTP 3xx (Redirect)"
                elif 400 <= num_code <= 499:
                    return "HTTP 4xx (Client Error)"
                elif 500 <= num_code <= 599:
                    return "HTTP 5xx (Server Error)"
                    
            return "Other / Inconclusive"

        STATUS_DESCRIPTIONS = {
            200: "OK", 201: "Created", 204: "No Content",
            301: "Moved Permanently", 302: "Found", 303: "See Other", 304: "Not Modified",
            307: "Temporary Redirect", 308: "Permanent Redirect",
            400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 408: "Request Timeout", 410: "Gone", 429: "Too Many Requests",
            500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"
        }

        # 4. Build Detailed Relationships (Sheet 3: Link Audit)
        link_audit_rows = []
        for i, link in enumerate(extracted, 1):
            norm = link['normalized_url']
            cached = dest_map.get(norm, {})
            source_url = link.get("source_url", "N/A")
            source_class = source_class_map.get(source_url, {})
            lang = URLNormalizer.detect_language(source_url) if source_url != "N/A" else "en"
            
            link_audit_rows.append({
                "Sr.no": i,
                "source_page_url": source_url,
                "language": lang,
                "component_name": link.get("component_name", "Unknown"),
                "anchor_text": link.get("anchor_text", "N/A"),
                "broken_destination_url": link.get("original_url", ""),
                "http_status_code": cached.get("final_status_code", ""),
                "remarks": cached.get("status", "Pending Validation"),
                "page_type": source_class.get("page_type") or "NEEDS_REVIEW",
                "business": source_class.get("business") or "NEEDS_REVIEW",
                "extracted_timestamp": link.get("extracted_timestamp", ""),
                "checked_timestamp": cached.get("checked_at", "")
            })
        df_link_audit = pd.DataFrame(link_audit_rows)

        # 5. Build Unique Non-200 Responses (Sheet 2: Non-200 Responses)
        links_by_dest = {}
        for link in extracted:
            norm = link['normalized_url']
            links_by_dest.setdefault(norm, []).append(link)

        unique_dests = sorted(list(links_by_dest.keys()))
        non_200_dest_rows = []
        sr_non200 = 1

        for norm in unique_dests:
            d_info = dest_map.get(norm, {})
            code = d_info.get("final_status_code")
            cat = get_dest_category(d_info)
            if code != 200 or cat != "HTTP 200 (Success)":
                d_links = links_by_dest[norm]
                aff_rels = len(d_links)
                srcs = list(dict.fromkeys([l.get("source_url") for l in d_links if l.get("source_url") and l.get("source_url") != "N/A"]))
                aff_srcs = len(srcs)
                sample_src = srcs[0] if srcs else "N/A"
                
                pts = list(dict.fromkeys([source_class_map.get(s, {}).get("page_type") for s in srcs if source_class_map.get(s, {}).get("page_type")]))
                bizs = list(dict.fromkeys([source_class_map.get(s, {}).get("business") for s in srcs if source_class_map.get(s, {}).get("business")]))
                
                pt_str = ", ".join(pts) if pts else "NEEDS_REVIEW"
                biz_str = ", ".join(bizs) if bizs else "NEEDS_REVIEW"
                
                final_url = d_info.get("final_url", "")
                redir_target = final_url if (final_url and final_url != norm) else ""
                err_msg = d_info.get("error_message") or d_info.get("error_type") or d_info.get("status") or ""
                status_code_disp = code if code is not None and code != "" else (d_info.get("error_type") or d_info.get("status") or "FAILED")
                
                non_200_dest_rows.append({
                    "Sr.no": sr_non200,
                    "destination_url": norm,
                    "http_status_code": status_code_disp,
                    "status_category": cat,
                    "affected_relationships_count": aff_rels,
                    "affected_sources_count": aff_srcs,
                    "sample_source_url": sample_src,
                    "source_page_type": pt_str,
                    "business": biz_str,
                    "redirect_target": redir_target,
                    "response_time_ms": d_info.get("response_time_ms", ""),
                    "error_reason": err_msg
                })
                sr_non200 += 1

        non_200_dest_rows.sort(key=lambda x: x["affected_relationships_count"], reverse=True)
        for idx, row in enumerate(non_200_dest_rows, 1):
            row["Sr.no"] = idx

        df_non_200 = pd.DataFrame(non_200_dest_rows) if non_200_dest_rows else pd.DataFrame(columns=[
            "Sr.no", "destination_url", "http_status_code", "status_category",
            "affected_relationships_count", "affected_sources_count", "sample_source_url",
            "source_page_type", "business", "redirect_target", "response_time_ms", "error_reason"
        ])

        # 6. Aggregations for Sheet 1: Pivot Summary
        CATEGORIES_A = [
            "HTTP 200 (Success)",
            "HTTP 3xx (Redirect)",
            "HTTP 4xx (Client Error)",
            "HTTP 5xx (Server Error)",
            "Timeout",
            "DNS / Connection Error",
            "Blocked / Skipped",
            "Other / Inconclusive",
        ]
        u_cat_counts = {c: 0 for c in CATEGORIES_A}
        r_cat_counts = {c: 0 for c in CATEGORIES_A}

        for norm in unique_dests:
            cat = get_dest_category(dest_map.get(norm, {}))
            u_cat_counts[cat] = u_cat_counts.get(cat, 0) + 1

        for link in extracted:
            norm = link['normalized_url']
            cat = get_dest_category(dest_map.get(norm, {}))
            r_cat_counts[cat] = r_cat_counts.get(cat, 0) + 1

        rows_sec_a = []
        for cat in CATEGORIES_A:
            u_cnt = u_cat_counts[cat]
            r_cnt = r_cat_counts[cat]
            u_pct = (u_cnt / len(unique_dests) * 100) if len(unique_dests) > 0 else 0.0
            r_pct = (r_cnt / len(extracted) * 100) if len(extracted) > 0 else 0.0
            rows_sec_a.append([cat, u_cnt, f"{u_pct:.1f}%", r_cnt, f"{r_pct:.1f}%"])

        total_sec_a = ["Total", len(unique_dests), "100.0%", len(extracted), "100.0%"]

        # Section B: Detailed HTTP Status Breakdown
        status_breakdown = {}
        for norm in unique_dests:
            d_info = dest_map.get(norm, {})
            cat = get_dest_category(d_info)
            code = d_info.get("final_status_code")
            err = d_info.get("error_type")
            stat = d_info.get("status")
            
            if code is not None and str(code).isdigit():
                disp_code = str(code)
                desc = STATUS_DESCRIPTIONS.get(int(code), f"HTTP Status {code}")
            elif cat == "Blocked / Skipped":
                disp_code = "BLOCKED"
                desc = "Blocked by Security Policy / SSRF"
            elif cat == "Timeout":
                disp_code = "TIMEOUT"
                desc = "Request Timed Out"
            elif cat == "DNS / Connection Error":
                disp_code = "DNS_ERROR" if (err and "DNS" in err) else "CONN_ERROR"
                desc = err or "Connection Failure"
            else:
                disp_code = str(err or stat or "UNKNOWN")
                desc = stat or "Other Error / Inconclusive"
                
            key = (disp_code, desc, cat)
            if key not in status_breakdown:
                status_breakdown[key] = {"unique": 0, "rels": 0}
            status_breakdown[key]["unique"] += 1
            status_breakdown[key]["rels"] += len(links_by_dest[norm])

        cat_order = {c: i for i, c in enumerate(CATEGORIES_A)}
        sorted_keys = sorted(status_breakdown.keys(), key=lambda k: (cat_order.get(k[2], 99), str(k[0])))
        rows_sec_b = []
        for k in sorted_keys:
            rows_sec_b.append([
                k[0], k[1], k[2],
                status_breakdown[k]["unique"],
                status_breakdown[k]["rels"]
            ])

        # Section C: Source Page Type Breakdown
        pt_rels = {}
        pt_unique_dests = {}
        pt_non200_dests = {}

        for link in extracted:
            src = link.get("source_url", "N/A")
            sc = source_class_map.get(src, {})
            pt = sc.get("page_type") or "NEEDS_REVIEW"
            norm = link['normalized_url']
            
            pt_rels.setdefault(pt, 0)
            pt_rels[pt] += 1
            
            pt_unique_dests.setdefault(pt, set()).add(norm)
            
            d_info = dest_map.get(norm, {})
            if d_info.get("final_status_code") != 200 or get_dest_category(d_info) != "HTTP 200 (Success)":
                pt_non200_dests.setdefault(pt, set()).add(norm)

        rows_sec_c = []
        for pt in sorted(pt_rels.keys()):
            u_cnt = len(pt_unique_dests.get(pt, set()))
            n_cnt = len(pt_non200_dests.get(pt, set()))
            r_cnt = pt_rels[pt]
            pct = f"{(n_cnt / u_cnt * 100):.1f}%" if u_cnt > 0 else "0.0%"
            rows_sec_c.append([pt, u_cnt, n_cnt, r_cnt, pct])

        total_non200_unique = len(non_200_dest_rows)
        total_sec_c = [
            "Total",
            len(unique_dests),
            total_non200_unique,
            len(extracted),
            f"{(total_non200_unique / len(unique_dests) * 100):.1f}%" if len(unique_dests) > 0 else "0.0%"
        ]

        # Section D: Business Breakdown
        biz_rels = {}
        biz_unique_dests = {}
        biz_non200_dests = {}

        for link in extracted:
            src = link.get("source_url", "N/A")
            sc = source_class_map.get(src, {})
            biz = sc.get("business") or "NEEDS_REVIEW"
            norm = link['normalized_url']
            
            biz_rels.setdefault(biz, 0)
            biz_rels[biz] += 1
            
            biz_unique_dests.setdefault(biz, set()).add(norm)
            
            d_info = dest_map.get(norm, {})
            if d_info.get("final_status_code") != 200 or get_dest_category(d_info) != "HTTP 200 (Success)":
                biz_non200_dests.setdefault(biz, set()).add(norm)

        rows_sec_d = []
        for b in sorted(biz_rels.keys()):
            u_cnt = len(biz_unique_dests.get(b, set()))
            n_cnt = len(biz_non200_dests.get(b, set()))
            r_cnt = biz_rels[b]
            pct = f"{(n_cnt / u_cnt * 100):.1f}%" if u_cnt > 0 else "0.0%"
            rows_sec_d.append([b, u_cnt, n_cnt, r_cnt, pct])

        total_sec_d = [
            "Total",
            len(unique_dests),
            total_non200_unique,
            len(extracted),
            f"{(total_non200_unique / len(unique_dests) * 100):.1f}%" if len(unique_dests) > 0 else "0.0%"
        ]

        # Section E: Source Page Type x Status Matrix
        MATRIX_COLS = ["HTTP 200", "3xx Redirect", "4xx Client Error", "5xx Server Error", "Timeout / Error", "Blocked"]
        def map_matrix_col(cat):
            if cat == "HTTP 200 (Success)": return "HTTP 200"
            if cat == "HTTP 3xx (Redirect)": return "3xx Redirect"
            if cat == "HTTP 4xx (Client Error)": return "4xx Client Error"
            if cat == "HTTP 5xx (Server Error)": return "5xx Server Error"
            if cat == "Blocked / Skipped": return "Blocked"
            return "Timeout / Error"

        pt_matrix = {pt: {c: 0 for c in MATRIX_COLS} for pt in pt_rels.keys()}
        for link in extracted:
            src = link.get("source_url", "N/A")
            sc = source_class_map.get(src, {})
            pt = sc.get("page_type") or "NEEDS_REVIEW"
            norm = link['normalized_url']
            cat = get_dest_category(dest_map.get(norm, {}))
            m_col = map_matrix_col(cat)
            if pt not in pt_matrix:
                pt_matrix[pt] = {c: 0 for c in MATRIX_COLS}
            pt_matrix[pt][m_col] += 1

        rows_sec_e = []
        col_sums_e = {c: 0 for c in MATRIX_COLS}
        for pt in sorted(pt_matrix.keys()):
            row_sum = sum(pt_matrix[pt][c] for c in MATRIX_COLS)
            row_vals = [pt]
            for c in MATRIX_COLS:
                val = pt_matrix[pt][c]
                col_sums_e[c] += val
                row_vals.append(val)
            row_vals.append(row_sum)
            rows_sec_e.append(row_vals)

        total_sec_e = ["Total"] + [col_sums_e[c] for c in MATRIX_COLS] + [len(extracted)]

        # Section F: Highest-Impact Failed Destinations
        rows_sec_f = []
        for idx, row in enumerate(non_200_dest_rows[:20], 1):
            rows_sec_f.append([
                idx,
                row["destination_url"],
                row["http_status_code"],
                row["status_category"],
                row["affected_relationships_count"],
                row["affected_sources_count"],
                row["sample_source_url"],
                row["source_page_type"]
            ])

        # 7. Write Workbook
        wb = openpyxl.Workbook()
        ws_pivot = wb.active
        ws_pivot.title = "Pivot Summary"
        self._write_pivot_summary_sheet(
            ws_pivot, job_id, len(extracted), len(unique_dests),
            rows_sec_a, total_sec_a,
            rows_sec_b,
            rows_sec_c, total_sec_c,
            rows_sec_d, total_sec_d,
            rows_sec_e, total_sec_e,
            rows_sec_f
        )

        ws_non200 = wb.create_sheet(title="Non-200 Responses")
        self._write_table_sheet(ws_non200, df_non_200)

        ws_links = wb.create_sheet(title="Link Audit")
        self._write_table_sheet(ws_links, df_link_audit)

        # Remediations sheet
        try:
            rems = await self.db.get_all_remediations_for_job(job_id)
            if not rems:
                from remediation_engine import RemediationEngine
                engine = RemediationEngine(self.db)
                rems = await engine.analyze_job(job_id)
                if rems:
                    await self.db.upsert_remediations(rems)
            if rems:
                ws_rem = wb.create_sheet(title="Remediations")
                self._write_remediations_sheet(ws_rem, rems)
        except Exception:
            pass

        wb.save(output_file)

    def _write_table_sheet(self, worksheet, df: pd.DataFrame):
        header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
        border = Border(
            left=Side(style='thin', color="D9D9D9"),
            right=Side(style='thin', color="D9D9D9"),
            top=Side(style='thin', color="D9D9D9"),
            bottom=Side(style='thin', color="D9D9D9")
        )
        
        # Write headers
        for col_idx, col in enumerate(df.columns, 1):
            cell = worksheet.cell(row=1, column=col_idx, value=str(col))
            cell.font = header_font
            cell.fill = header_fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")
            worksheet.row_dimensions[1].height = 24

        # Pre-identify column characteristics
        url_cols = {
            col_idx for col_idx, col in enumerate(df.columns, 1)
            if any(k in str(col).lower() for k in ("_url", "url", "link", "target")) and str(col).lower() not in ("url_status", "status_url")
        }
        status_cols = {
            col_idx for col_idx, col in enumerate(df.columns, 1)
            if str(col).lower() in ("http_status_code", "http_status", "status_code", "issue_type")
        }
        status_text_cols = {
            col_idx for col_idx, col in enumerate(df.columns, 1)
            if str(col).lower() in ("status", "page_status", "api_status", "outcome_status", "priority", "severity")
        }
        integer_cols = {
            col_idx for col_idx, col in enumerate(df.columns, 1)
            if str(col).lower() in ("sr.no", "affected_relationships_count", "affected_sources_count", "api_count", "failed_api_count", "sources count", "score")
        }
        ms_cols = {
            col_idx for col_idx, col in enumerate(df.columns, 1)
            if "response_time" in str(col).lower()
        }
        kb_cols = {
            col_idx for col_idx, col in enumerate(df.columns, 1)
            if "size_kb" in str(col).lower()
        }

        # Write data rows
        for row_idx, row_data in enumerate(df.itertuples(index=False), 2):
            worksheet.row_dimensions[row_idx].height = 20
            for col_idx, raw_val in enumerate(row_data, 1):
                clean_val = sanitize_cell_for_export(raw_val)
                cell = worksheet.cell(row=row_idx, column=col_idx, value=clean_val)
                cell.font = Font(name="Calibri", size=10)
                cell.border = border
                cell.alignment = Alignment(wrap_text=True, vertical="top")

                # 1. Hyperlinks for URLs
                if col_idx in url_cols and isinstance(clean_val, str) and clean_val.startswith(("http://", "https://")):
                    cell.hyperlink = clean_val
                    cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")

                # 2. Status code badges / soft fills
                elif col_idx in status_cols:
                    code_val = None
                    if isinstance(raw_val, int):
                        code_val = raw_val
                    elif isinstance(clean_val, str) and clean_val.isdigit():
                        code_val = int(clean_val)
                    elif isinstance(clean_val, str) and clean_val.startswith("HTTP ") and clean_val[5:].isdigit():
                        code_val = int(clean_val[5:])

                    cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
                    if code_val is not None:
                        cell.number_format = "0"
                        if 200 <= code_val <= 299:
                            cell.fill = FILL_2XX
                            cell.font = FONT_2XX
                        elif 300 <= code_val <= 399:
                            cell.fill = FILL_3XX
                            cell.font = FONT_3XX
                        elif 400 <= code_val <= 499:
                            cell.fill = FILL_4XX
                            cell.font = FONT_4XX
                        elif 500 <= code_val <= 599:
                            cell.fill = FILL_5XX
                            cell.font = FONT_5XX
                    else:
                        c_upper = str(clean_val).upper()
                        if any(err_term in c_upper for err_term in ("TIMEOUT", "FAILED", "DNS", "CONN", "ERROR", "500", "502", "503", "504")):
                            cell.fill = FILL_5XX
                            cell.font = FONT_5XX
                        elif "BLOCKED" in c_upper or "SSRF" in c_upper or "SKIP" in c_upper:
                            cell.fill = FILL_BLOCKED
                            cell.font = FONT_BLOCKED

                # 3. Status text badges / soft fills
                elif col_idx in status_text_cols:
                    s_upper = str(clean_val).upper()
                    if s_upper in STATUS_COLORS:
                        fill, font = STATUS_COLORS[s_upper]
                        cell.fill = fill
                        cell.font = font
                        cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
                    elif s_upper in PRIORITY_COLORS:
                        fill, font = PRIORITY_COLORS[s_upper]
                        cell.fill = fill
                        cell.font = font
                        cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
                    elif s_upper in SEVERITY_COLORS:
                        fill, font = SEVERITY_COLORS[s_upper]
                        cell.fill = fill
                        cell.font = font
                        cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)

                # 4. Integer formatting
                elif col_idx in integer_cols:
                    if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
                        cell.number_format = "#,##0"
                        cell.alignment = Alignment(horizontal="right", vertical="top", wrap_text=True)
                    elif str(df.columns[col_idx - 1]).lower() == "sr.no":
                        cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)

                # 5. Response time (ms)
                elif col_idx in ms_cols:
                    if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
                        cell.number_format = '#,##0 "ms"'
                        cell.alignment = Alignment(horizontal="right", vertical="top", wrap_text=True)

                # 6. File size (KB)
                elif col_idx in kb_cols:
                    if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
                        cell.number_format = '#,##0.0 "KB"'
                        cell.alignment = Alignment(horizontal="right", vertical="top", wrap_text=True)

        # Auto-fit width with max limit
        for col_idx, col in enumerate(df.columns, 1):
            col_letter = get_column_letter(col_idx)
            max_content_len = 0
            if len(df) > 0:
                s_max = df[col].astype(str).str.len().max()
                if pd.notna(s_max):
                    max_content_len = int(s_max)
            max_len = max(max_content_len, len(str(col))) + 3
            worksheet.column_dimensions[col_letter].width = min(max_len, 60)

        worksheet.freeze_panes = "A2"
        if len(df.columns) > 0:
            max_col_letter = get_column_letter(len(df.columns))
            worksheet.auto_filter.ref = f"A1:{max_col_letter}{max(len(df) + 1, 1)}"

        _configure_sheet_print_setup(worksheet, has_header=True)

    def _write_pivot_summary_sheet(
        self, ws, job_id: str, total_extracted: int, total_unique: int,
        rows_sec_a, total_sec_a,
        rows_sec_b,
        rows_sec_c, total_sec_c,
        rows_sec_d, total_sec_d,
        rows_sec_e, total_sec_e,
        rows_sec_f
    ):
        title_font = Font(name="Calibri", size=14, bold=True, color="1F497D")
        sub_font = Font(name="Calibri", size=9, italic=True, color="595959")
        sec_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        hdr_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        tot_font = Font(name="Calibri", size=10, bold=True, color="000000")
        dat_font = Font(name="Calibri", size=10, color="000000")

        sec_fill = PatternFill(start_color="2C4D75", end_color="2C4D75", fill_type="solid")
        hdr_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
        tot_fill = PatternFill(start_color="DCE6F1", end_color="DCE6F1", fill_type="solid")
        alt_fill = PatternFill(start_color="F2F5F9", end_color="F2F5F9", fill_type="solid")

        thin_border = Border(
            left=Side(style='thin', color="D9D9D9"),
            right=Side(style='thin', color="D9D9D9"),
            top=Side(style='thin', color="D9D9D9"),
            bottom=Side(style='thin', color="D9D9D9")
        )
        tot_border = Border(
            left=Side(style='thin', color="D9D9D9"),
            right=Side(style='thin', color="D9D9D9"),
            top=Side(style='thin', color="1F497D"),
            bottom=Side(style='double', color="1F497D")
        )

        ws.cell(row=1, column=1, value="LINK AUDITOR EXECUTIVE SUMMARY & PIVOT REPORT").font = title_font
        ws.row_dimensions[1].height = 24
        
        dedup_avoided = max(0, total_extracted - total_unique)
        dedup_pct = f"{(dedup_avoided / total_extracted * 100):.1f}%" if total_extracted > 0 else "0.0%"
        sub_text = (
            f"Audit Job ID: {job_id}  |  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}  |  "
            f"Total Extracted Links: {total_extracted:,}  |  Unique Destinations Validated: {total_unique:,}  |  "
            f"Deduplication Savings: {dedup_pct} ({dedup_avoided:,} duplicate checks avoided)"
        )
        ws.cell(row=2, column=1, value=sub_text).font = sub_font
        ws.row_dimensions[2].height = 18

        current_row = 4

        def render_section(sec_title, headers, rows, total_row=None, col_alignments=None, is_legend: bool = False):
            nonlocal current_row
            num_cols = len(headers)
            
            ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=num_cols)
            banner = ws.cell(row=current_row, column=1, value=sec_title)
            banner.font = sec_font
            banner.fill = sec_fill
            banner.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            ws.row_dimensions[current_row].height = 22
            current_row += 1

            ws.row_dimensions[current_row].height = 20
            for c_idx, h in enumerate(headers, 1):
                c = ws.cell(row=current_row, column=c_idx, value=h)
                c.font = hdr_font
                c.fill = hdr_fill
                c.border = thin_border
                c.alignment = Alignment(horizontal="center", vertical="center")
            current_row += 1

            for r_idx, r_data in enumerate(rows):
                ws.row_dimensions[current_row].height = 18
                rf = alt_fill if r_idx % 2 == 1 else None
                for c_idx, val in enumerate(r_data, 1):
                    clean_val = sanitize_cell_for_export(val)
                    c = ws.cell(row=current_row, column=c_idx, value=clean_val)
                    c.font = dat_font
                    if rf: c.fill = rf
                    c.border = thin_border
                    align = col_alignments[c_idx - 1] if col_alignments and c_idx - 1 < len(col_alignments) else "left"
                    c.alignment = Alignment(horizontal=align, vertical="center")

                    # Soft badges for Color Legend
                    if is_legend and c_idx == 2:
                        if r_idx == 0:
                            c.fill = FILL_2XX
                            c.font = FONT_2XX
                        elif r_idx == 1:
                            c.fill = FILL_3XX
                            c.font = FONT_3XX
                        elif r_idx == 2:
                            c.fill = FILL_4XX
                            c.font = FONT_4XX
                        elif r_idx in (3, 4):
                            c.fill = FILL_5XX
                            c.font = FONT_5XX
                        elif r_idx == 5:
                            c.fill = FILL_BLOCKED
                            c.font = FONT_BLOCKED

                    # Section F hyperlinks and status fills
                    elif "SECTION F" in sec_title:
                        if c_idx in (2, 7) and isinstance(clean_val, str) and clean_val.startswith(("http://", "https://")):
                            c.hyperlink = clean_val
                            c.font = FONT_LINK
                        elif c_idx == 3:
                            c_str = str(clean_val).strip()
                            if c_str.isdigit():
                                c_num = int(c_str)
                                if 200 <= c_num <= 299:
                                    c.fill = FILL_2XX
                                    c.font = FONT_2XX
                                elif 300 <= c_num <= 399:
                                    c.fill = FILL_3XX
                                    c.font = FONT_3XX
                                elif 400 <= c_num <= 499:
                                    c.fill = FILL_4XX
                                    c.font = FONT_4XX
                                elif 500 <= c_num <= 599:
                                    c.fill = FILL_5XX
                                    c.font = FONT_5XX
                            elif any(err_term in c_str.upper() for err_term in ("TIMEOUT", "FAILED", "DNS", "CONN", "ERROR")):
                                c.fill = FILL_5XX
                                c.font = FONT_5XX
                            elif "BLOCKED" in c_str.upper():
                                c.fill = FILL_BLOCKED
                                c.font = FONT_BLOCKED
                current_row += 1

            if total_row:
                ws.row_dimensions[current_row].height = 20
                for c_idx, val in enumerate(total_row, 1):
                    c = ws.cell(row=current_row, column=c_idx, value=val)
                    c.font = tot_font
                    c.fill = tot_fill
                    c.border = tot_border
                    align = col_alignments[c_idx - 1] if col_alignments and c_idx - 1 < len(col_alignments) else "left"
                    c.alignment = Alignment(horizontal=align, vertical="center")
                current_row += 1

            current_row += 1

        # 1. Section A
        render_section(
            "SECTION A: OVERALL STATUS SUMMARY (RECONCILIATION)",
            ["Status Category", "Unique URLs Count", "Unique URLs %", "Relationship Occurrences Count", "Relationship Occurrences %"],
            rows_sec_a, total_sec_a,
            ["left", "right", "right", "right", "right"]
        )

        # 2. Section B
        render_section(
            "SECTION B: DETAILED HTTP STATUS BREAKDOWN",
            ["HTTP Status Code", "Status Description", "Category", "Unique URLs Count", "Relationship Occurrences Count"],
            rows_sec_b, None,
            ["center", "left", "left", "right", "right"]
        )

        # 3. Section C
        render_section(
            "SECTION C: SOURCE PAGE TYPE BREAKDOWN",
            ["Source Page Type", "Unique Destinations Count", "Non-200 Destinations Count", "Affected Relationships Count", "Non-200 %"],
            rows_sec_c, total_sec_c,
            ["left", "right", "right", "right", "right"]
        )

        # 4. Section D
        render_section(
            "SECTION D: BUSINESS UNIT BREAKDOWN",
            ["Business", "Unique Destinations Count", "Non-200 Destinations Count", "Affected Relationships Count", "Non-200 %"],
            rows_sec_d, total_sec_d,
            ["left", "right", "right", "right", "right"]
        )

        # 5. Section E
        render_section(
            "SECTION E: SOURCE PAGE TYPE × STATUS CATEGORY MATRIX",
            ["Source Page Type", "HTTP 200", "3xx Redirect", "4xx Client Error", "5xx Server Error", "Timeout / Error", "Blocked", "Total Relationships"],
            rows_sec_e, total_sec_e,
            ["left", "right", "right", "right", "right", "right", "right", "right"]
        )

        # 6. Section F
        render_section(
            "SECTION F: HIGHEST-IMPACT FAILED DESTINATIONS (TOP 20)",
            ["Rank", "Destination URL", "Status Code", "Category", "Affected Relationships Count", "Affected Sources Count", "Sample Source URL", "Page Type"],
            rows_sec_f, None,
            ["center", "left", "center", "left", "right", "right", "left", "left"]
        )

        # 7. Section G: Color Coding Legend
        legend_headers = ["Category / HTTP Range", "Visual Indicator", "Meaning & Recommended Action"]
        legend_rows = [
            ["HTTP 200 (Success)", "Success (Green #E2EFDA)", "Destination reachable and returned standard HTTP 200 response."],
            ["HTTP 3xx (Redirect)", "Redirect (Amber #FFF2CC)", "Redirect detected. Recommended: update link to point directly to canonical target."],
            ["HTTP 4xx (Client Error)", "Client Error (Orange #FCE4D6)", "Broken link or client error (e.g., 404 Not Found, 403 Forbidden). High remediation priority."],
            ["HTTP 5xx (Server Error)", "Server Error (Red #FADBD8)", "Server failure, gateway timeout, or backend outage. Verify origin server availability."],
            ["Timeout / Connection Error", "Failure (Red #FADBD8)", "Connection attempt timed out or failed DNS resolution."],
            ["Blocked / Skipped", "Policy / Security (Gray #EAECEE)", "URL was skipped due to SSRF safety rules or policy restrictions."]
        ]
        render_section(
            "SECTION G: STATUS & CATEGORY COLOR CODING LEGEND",
            legend_headers,
            legend_rows,
            None,
            ["left", "center", "left"],
            is_legend=True
        )

        col_max_lens = {}
        for row in ws.iter_rows(min_row=4, max_row=current_row, min_col=1, max_col=8):
            for cell in row:
                if cell.value is not None:
                    if ws.cell(row=cell.row, column=1).coordinate != cell.coordinate or len(str(cell.value)) < 40:
                        c_idx = cell.column
                        col_max_lens[c_idx] = max(col_max_lens.get(c_idx, 0), len(str(cell.value)))

        for c_idx in range(1, 9):
            col_letter = get_column_letter(c_idx)
            w = max(col_max_lens.get(c_idx, 10) + 3, 14)
            ws.column_dimensions[col_letter].width = min(w, 65)

        ws.views.sheetView[0].showGridLines = True
        ws.freeze_panes = "A4"
        _configure_sheet_print_setup(ws, has_header=False)

    async def generate_api_audit_report(self, job_id: str, output_file: str):
        temp_file = f"{output_file}.{os.getpid()}_{uuid.uuid4().hex[:6]}.tmp.xlsx"
        try:
            await self._do_generate_api_audit_report(job_id, temp_file)
            os.replace(temp_file, output_file)
        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    async def _do_generate_api_audit_report(self, job_id: str, output_file: str):
        pages = await self.db.get_page_audit_results(job_id)
        apis = await self.db.get_page_api_responses(job_id)

        # 1. Build Summary Data
        total_pages = len(pages)
        found_cnt = sum(1 for p in pages if p.get("page_status") == "FOUND")
        not_found_cnt = sum(1 for p in pages if p.get("page_status") == "NOT_FOUND")
        api_err_cnt = sum(1 for p in pages if p.get("page_status") == "API_ERROR")
        page_err_cnt = sum(1 for p in pages if p.get("page_status") == "PAGE_ERROR")
        timeout_cnt = sum(1 for p in pages if p.get("page_status") == "TIMEOUT")
        inconclusive_cnt = sum(1 for p in pages if p.get("page_status") == "INCONCLUSIVE")
        skipped_cnt = sum(1 for p in pages if p.get("page_status") == "SKIPPED")
        screenshots_cnt = sum(1 for p in pages if p.get("screenshot_path"))

        total_apis = len(apis)
        successful_apis = sum(1 for a in apis if a.get("api_status") == "FOUND")
        failed_apis = sum(1 for a in apis if a.get("api_status") in ["API_ERROR", "TIMEOUT"])

        summary_rows = [
            {"Metric": "Audit Job ID", "Value": job_id},
            {"Metric": "Audit Mode", "Value": "Page & API Response Audit"},
            {"Metric": "Generated Timestamp", "Value": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")},
            {"Metric": "Total Pages Audited", "Value": total_pages},
            {"Metric": "Pages FOUND (Valid Content)", "Value": found_cnt},
            {"Metric": "Pages NOT_FOUND (Empty / 404)", "Value": not_found_cnt},
            {"Metric": "Pages with Critical API_ERROR", "Value": api_err_cnt},
            {"Metric": "Pages with PAGE_ERROR", "Value": page_err_cnt},
            {"Metric": "Pages with TIMEOUT", "Value": timeout_cnt},
            {"Metric": "Pages INCONCLUSIVE", "Value": inconclusive_cnt},
            {"Metric": "Pages SKIPPED (Security / SSRF)", "Value": skipped_cnt},
            {"Metric": "Screenshots Captured", "Value": screenshots_cnt},
            {"Metric": "Total Fetch/XHR APIs Observed", "Value": total_apis},
            {"Metric": "Successful APIs", "Value": successful_apis},
            {"Metric": "Failed APIs", "Value": failed_apis},
        ]
        df_summary = pd.DataFrame(summary_rows)

        import urllib.parse
        def _safe_url(u: str) -> str:
            if not u: return ""
            try:
                p = urllib.parse.urlsplit(u)
                q_pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
                s_keys = {"token", "key", "secret", "password", "auth", "api_key", "apikey", "signature", "access_token", "refresh_token", "session", "jwt", "cvv"}
                safe = [(k, "[REDACTED]") if k.lower() in s_keys else (k, v) for k, v in q_pairs]
                return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(safe), p.fragment))
            except Exception:
                return u

        # 2. Build Page Results Data
        page_rows = []
        for i, p in enumerate(pages, 1):
            ss_ref = os.path.basename(p.get("screenshot_path")) if p.get("screenshot_path") else ""
            page_rows.append({
                "Sr.no": i,
                "page_url": _safe_url(p.get("page_url", "")),
                "final_url": _safe_url(p.get("final_url", "")),
                "page_status": p.get("page_status", "INCONCLUSIVE"),
                "http_status": p.get("http_status", ""),
                "response_time_ms": p.get("response_time_ms", ""),
                "api_count": p.get("api_count", 0),
                "failed_api_count": p.get("failed_api_count", 0),
                "screenshot_available": "YES" if ss_ref else "NO",
                "screenshot_filename": ss_ref,
                "remarks": p.get("summary_reason") or p.get("error_message") or "",
                "evaluated_timestamp": p.get("evaluated_at", "")
            })
        df_pages = pd.DataFrame(page_rows) if page_rows else pd.DataFrame(columns=[
            "Sr.no", "page_url", "final_url", "page_status", "http_status", "response_time_ms",
            "api_count", "failed_api_count", "screenshot_available", "screenshot_filename", "remarks", "evaluated_timestamp"
        ])

        # 3. Build API Responses Data
        api_rows = []
        for i, a in enumerate(apis, 1):
            api_rows.append({
                "Sr.no": i,
                "parent_page_url": _safe_url(a.get("page_url", "")),
                "api_url": _safe_url(a.get("request_url", "")),
                "method": a.get("method", "GET"),
                "resource_type": a.get("resource_type", "fetch"),
                "http_status": a.get("http_status", ""),
                "api_status": a.get("api_status", "INCONCLUSIVE"),
                "response_time_ms": a.get("response_time_ms", ""),
                "error_message": a.get("error_message", ""),
                "response_snippet": a.get("response_snippet", ""),
                "observed_timestamp": a.get("observed_at", "")
            })
        df_apis = pd.DataFrame(api_rows) if api_rows else pd.DataFrame(columns=[
            "Sr.no", "parent_page_url", "api_url", "method", "resource_type", "http_status",
            "api_status", "response_time_ms", "error_message", "response_snippet", "observed_timestamp"
        ])

        # 4. Build Diagnostics Data
        diag_rows = []
        sr_diag = 1
        for p in pages:
            if p.get("page_status") in ["NOT_FOUND", "API_ERROR", "PAGE_ERROR", "TIMEOUT", "SKIPPED"] or p.get("error_message"):
                ss_ref = os.path.basename(p.get("screenshot_path")) if p.get("screenshot_path") else ""
                diag_rows.append({
                    "Sr.no": sr_diag,
                    "target_type": "PAGE",
                    "page_url": _safe_url(p.get("page_url", "")),
                    "associated_url": _safe_url(p.get("final_url", "")),
                    "issue_type": f"HTTP {p.get('http_status')}" if p.get("http_status") else p.get("page_status"),
                    "outcome_status": p.get("page_status"),
                    "diagnostic_detail": p.get("summary_reason") or p.get("error_message") or "",
                    "screenshot_filename": ss_ref,
                    "timestamp": p.get("evaluated_at", "")
                })
                sr_diag += 1

        for a in apis:
            if a.get("api_status") in ["API_ERROR", "TIMEOUT", "NOT_FOUND"] or a.get("error_message"):
                diag_rows.append({
                    "Sr.no": sr_diag,
                    "target_type": "API",
                    "page_url": _safe_url(a.get("page_url", "")),
                    "associated_url": _safe_url(a.get("request_url", "")),
                    "issue_type": f"HTTP {a.get('http_status')}" if a.get("http_status") else a.get("api_status"),
                    "outcome_status": a.get("api_status"),
                    "diagnostic_detail": a.get("error_message") or a.get("response_snippet") or "",
                    "screenshot_filename": "",
                    "timestamp": a.get("observed_at", "")
                })
                sr_diag += 1

        df_diag = pd.DataFrame(diag_rows) if diag_rows else pd.DataFrame(columns=[
            "Sr.no", "target_type", "page_url", "associated_url", "issue_type", "outcome_status",
            "diagnostic_detail", "screenshot_path", "timestamp"
        ])

        # Write to multi-sheet Excel workbook with corporate openpyxl formatting
        wb = openpyxl.Workbook()
        sheets_data = [
            ("Summary", df_summary),
            ("Page Results", df_pages),
            ("API Responses", df_apis),
            ("Diagnostics", df_diag),
        ]
        for idx, (sheet_name, df_sheet) in enumerate(sheets_data):
            if idx == 0:
                ws = wb.active
                ws.title = sheet_name
            else:
                ws = wb.create_sheet(title=sheet_name)
            self._write_table_sheet(ws, df_sheet)
        wb.save(output_file)

    def _write_remediations_sheet(self, ws, rems: list):
        headers = [
            "Remediation ID", "Priority", "Score", "Severity", "Category",
            "Status", "Target URL", "Sources Count", "Title",
            "Recommended Action", "Impact", "Priority Rationale", "Review Notes"
        ]
        header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
        border = Border(
            left=Side(style='thin', color="D9D9D9"),
            right=Side(style='thin', color="D9D9D9"),
            top=Side(style='thin', color="D9D9D9"),
            bottom=Side(style='thin', color="D9D9D9")
        )

        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 24

        for row_idx, r in enumerate(rems, 2):
            ws.row_dimensions[row_idx].height = 22
            prio = sanitize_cell_for_export(r.get("priority"))
            sev = sanitize_cell_for_export(r.get("severity"))
            stat = sanitize_cell_for_export(r.get("status"))
            target_url = sanitize_cell_for_export(r.get("target_url"))

            row_vals = [
                sanitize_cell_for_export(r.get("remediation_id")),
                prio,
                r.get("priority_score", 0),
                sev,
                sanitize_cell_for_export(r.get("category")),
                stat,
                target_url,
                r.get("source_count", 1),
                sanitize_cell_for_export(r.get("title")),
                sanitize_cell_for_export(r.get("recommended_action")),
                sanitize_cell_for_export(r.get("impact")),
                sanitize_cell_for_export(r.get("priority_rationale")),
                sanitize_cell_for_export(r.get("review_notes") or "")
            ]
            for col_idx, val in enumerate(row_vals, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.font = Font(name="Calibri", size=10)
                cell.border = border
                cell.alignment = Alignment(wrap_text=True, vertical="top")

                # Priority formatting (Col 2)
                if col_idx == 2 and str(prio).upper() in PRIORITY_COLORS:
                    fill, font = PRIORITY_COLORS[str(prio).upper()]
                    cell.fill = fill
                    cell.font = font
                    cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)

                # Score formatting (Col 3)
                elif col_idx == 3:
                    cell.number_format = "#,##0"
                    cell.alignment = Alignment(horizontal="right", vertical="top", wrap_text=True)

                # Severity formatting (Col 4)
                elif col_idx == 4 and str(sev).upper() in SEVERITY_COLORS:
                    fill, font = SEVERITY_COLORS[str(sev).upper()]
                    cell.fill = fill
                    cell.font = font
                    cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)

                # Status formatting (Col 6)
                elif col_idx == 6 and str(stat).upper() in STATUS_COLORS:
                    fill, font = STATUS_COLORS[str(stat).upper()]
                    cell.fill = fill
                    cell.font = font
                    cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)

                # Target URL clickable hyperlink (Col 7)
                elif col_idx == 7 and isinstance(target_url, str) and target_url.startswith(("http://", "https://")):
                    cell.hyperlink = target_url
                    cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")

                # Sources Count (Col 8)
                elif col_idx == 8:
                    cell.number_format = "#,##0"
                    cell.alignment = Alignment(horizontal="right", vertical="top", wrap_text=True)

        # Auto-fit column widths
        ws.column_dimensions["A"].width = 22  # Remediation ID
        ws.column_dimensions["B"].width = 12  # Priority
        ws.column_dimensions["C"].width = 10  # Score
        ws.column_dimensions["D"].width = 14  # Severity
        ws.column_dimensions["E"].width = 18  # Category
        ws.column_dimensions["F"].width = 14  # Status
        ws.column_dimensions["G"].width = 40  # Target URL
        ws.column_dimensions["H"].width = 14  # Sources Count
        ws.column_dimensions["I"].width = 35  # Title
        ws.column_dimensions["J"].width = 45  # Recommended Action
        ws.column_dimensions["K"].width = 35  # Impact
        ws.column_dimensions["L"].width = 35  # Rationale
        ws.column_dimensions["M"].width = 25  # Review Notes

        ws.freeze_panes = "A2"
        max_col_letter = get_column_letter(len(headers))
        ws.auto_filter.ref = f"A1:{max_col_letter}{max(len(rems) + 1, 1)}"
        _configure_sheet_print_setup(ws, has_header=True)
