"""
Remediation Engine for SEO and Site Quality Audits.
Transforms raw audit findings from database into prioritized, structured remediation actions.
"""

import hashlib
import json
import logging
import urllib.parse
from typing import List, Dict, Any, Optional
from ai_provider import RemediationAIProvider, sanitize_untrusted_input

logger = logging.getLogger(__name__)

SEVERITY_BASE_SCORES = {
    "CRITICAL": 70,
    "HIGH": 55,
    "MEDIUM": 35,
    "LOW": 20,
    "INFO": 10
}

def normalize_remediation_url(url: str) -> str:
    """
    Normalizes a remediation target URL:
    - Strips leading/trailing whitespace
    - Lowercases scheme and netloc (domain)
    - Strips standard default ports (:80 for http, :443 for https)
    - Strips URL fragments (#section)
    - Normalizes root path to '/'
    """
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme and not parsed.netloc:
            return url.split("#")[0].strip()
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        path = parsed.path
        if not path and netloc:
            path = "/"
        clean = urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ""))
        return clean
    except Exception:
        return url.split("#")[0].strip()


def calculate_priority_score(
    severity: str,
    source_count: int,
    confidence: float = 1.0,
    business_multiplier: float = 0.0
) -> (int, str):
    """
    Computes deterministic priority score (0-100) and priority level (P1-P4).
    """
    base = SEVERITY_BASE_SCORES.get(severity.upper(), 30)
    
    # Source impact multiplier
    if source_count > 20:
        impact_boost = 30
    elif source_count >= 6:
        impact_boost = 20
    elif source_count >= 2:
        impact_boost = 10
    else:
        impact_boost = 0
        
    raw_score = (base + impact_boost + int(business_multiplier * 10)) * confidence
    score = max(0, min(100, int(round(raw_score))))
    
    if score >= 80:
        prio = "P1"
    elif score >= 60:
        prio = "P2"
    elif score >= 40:
        prio = "P3"
    else:
        prio = "P4"
        
    return score, prio


def generate_stable_rem_id(category: str, target_url: str) -> str:
    """
    Generates a deterministic, collision-resistant, and human-friendly remediation identifier.
    e.g. REM-LNK-4A1B2C3D
    """
    clean_url = normalize_remediation_url(target_url)
    cat_prefix = {
        "BROKEN_LINK": "LNK",
        "REDIRECT_CHAIN": "RED",
        "REDIRECT_LOOP": "LOP",
        "BROKEN_IMAGE": "IMG",
        "MISSING_ALT_TEXT": "ALT",
        "FAILED_API": "API",
        "PAGE_ERROR": "PAG",
        "METADATA_ISSUE": "MET"
    }.get(category.upper(), "GEN")
    
    hash_digest = hashlib.sha256(f"{category.upper()}:{clean_url}".encode("utf-8")).hexdigest()[:8].upper()
    return f"REM-{cat_prefix}-{hash_digest}"


class RemediationEngine:
    """
    Scans audit database tables for a job and produces structured, prioritized remediations.
    """

    def __init__(self, db: Any, ai_provider: Optional[RemediationAIProvider] = None):
        self.db = db
        self.ai = ai_provider or RemediationAIProvider(db=db)

    async def analyze_job(self, job_id: str) -> List[Dict[str, Any]]:
        """
        Gathers findings across all audit types, computes priorities, and builds remediations.
        """
        raw_findings: List[Dict[str, Any]] = []

        # 1. Broken Links & Unhealthy Destinations
        link_findings = await self._analyze_link_destinations(job_id)
        raw_findings.extend(link_findings)

        # 2. Redirect Chains & Loops
        redirect_findings = await self._analyze_redirects(job_id)
        raw_findings.extend(redirect_findings)

        # 3. Broken Images
        image_findings = await self._analyze_images(job_id)
        raw_findings.extend(image_findings)

        # 4. Missing Alt Text
        alt_findings = await self._analyze_alt_text(job_id)
        raw_findings.extend(alt_findings)

        # 5. Failed APIs & Page Audit Results
        api_findings = await self._analyze_page_api_results(job_id)
        raw_findings.extend(api_findings)

        # 6. Metadata & Source Classification Issues
        meta_findings = await self._analyze_metadata(job_id)
        raw_findings.extend(meta_findings)

        # 7. On-page audit issues (persisted by the on-page report package)
        raw_findings.extend(await self._analyze_on_page_issues(job_id))

        # Deduplicate findings by (category, normalized target_url)
        unique_findings: Dict[str, Dict[str, Any]] = {}
        for f in raw_findings:
            norm_url = normalize_remediation_url(f['target_url'])
            f['target_url'] = norm_url
            key = f"{f['category']}::{norm_url}"
            if key not in unique_findings:
                unique_findings[key] = f
            else:
                # Merge sources if duplicate
                existing = unique_findings[key]
                existing_sources = set(existing.get("source_urls", []))
                for s in f.get("source_urls", []):
                    if s:
                        existing_sources.add(s)
                existing["source_urls"] = list(existing_sources)
                existing["source_count"] = max(1, len(existing["source_urls"]))
                # Keep higher severity if duplicate findings differ
                if SEVERITY_BASE_SCORES.get(f.get("severity", "").upper(), 0) > SEVERITY_BASE_SCORES.get(existing.get("severity", "").upper(), 0):
                    existing["severity"] = f["severity"]

        # Generate explanations and final priority scores
        remediations: List[Dict[str, Any]] = []
        for finding in unique_findings.values():
            category = finding["category"]
            target_url = finding["target_url"]
            source_count = finding["source_count"]
            severity = finding["severity"]
            confidence = finding.get("confidence", 1.0)
            
            score, prio = calculate_priority_score(
                severity=severity,
                source_count=source_count,
                confidence=confidence
            )
            finding["priority_score"] = score
            finding["priority"] = prio
            finding["remediation_id"] = generate_stable_rem_id(category, target_url)

            # Generate structured explanation (on-page findings carry their own deterministic, data-derived text)
            explanation = finding.pop("explanation", None) or await self.ai.generate_explanation(finding)
            
            remediations.append({
                "job_id": job_id,
                "remediation_id": finding["remediation_id"],
                "category": category,
                "severity": severity,
                "priority": prio,
                "priority_score": score,
                "confidence": confidence,
                "status": "OPEN",
                "review_notes": None,
                "target_url": target_url,
                "source_urls": finding.get("source_urls", []),
                "source_count": source_count,
                "title": explanation["title"],
                "description": explanation["description"],
                "impact": explanation["impact"],
                "recommended_action": explanation["recommended_action"],
                "priority_rationale": explanation["priority_rationale"],
                "technical_details": finding.get("technical_details", {})
            })

        # Sort by priority_score descending
        remediations.sort(key=lambda r: r["priority_score"], reverse=True)
        return remediations

    ON_PAGE_SITEWIDE_THRESHOLD = 25
    ON_PAGE_SEVERITY = {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}   # INFO findings are not remediations

    async def _analyze_on_page_issues(self, job_id: str) -> List[Dict[str, Any]]:
        """Group on_page_issues into remediation items with ONE aggregate query per grouping (no N+1):
        * destination issues (links, resources, images): one item per broken/redirecting URL, listing the pages;
        * page issues: one item per page, or one site-wide item when a rule affects > 25 pages."""
        from issue_store import ISSUE_SCHEMA_SQL
        from on_page_issue_rules import action_for
        await self.db.conn.executescript(ISSUE_SCHEMA_SQL)
        async with self.db.conn.execute("SELECT 1 FROM on_page_issues WHERE job_id = ? LIMIT 1", (job_id,)) as cur:
            if await cur.fetchone() is None:
                return []
        dest_cats = "('LINK','IFRAME','RESOURCE','METADATA_URL','FORM','IMAGE','CANONICAL','HREFLANG')"
        # worst severity by rank (string MAX would rank MEDIUM above HIGH)
        worst = ("CASE MIN(CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END) "
                 "WHEN 0 THEN 'HIGH' WHEN 1 THEN 'MEDIUM' ELSE 'LOW' END")
        findings: List[Dict[str, Any]] = []

        def item(rule, category, severity, target, pages, count, detail, kind):
            action = action_for(rule, category)
            human = rule.replace("_", " ").capitalize()
            return {
                "category": f"ONPAGE_{rule}", "severity": self.ON_PAGE_SEVERITY[severity], "target_url": target,
                "source_urls": [p for p in (pages or "").split("\x1f") if p][:50], "source_count": max(1, count),
                "confidence": 1.0,
                "technical_details": {"rule_code": rule, "issue_category": category, "detail": detail, "scope": kind},
                "explanation": {
                    "title": f"{human}: {target}"[:300],
                    "description": f"{human} detected by the on-page audit on {count} page(s). Example: {detail}"[:1000],
                    "impact": {"HIGH": "Broken for users and search engines; fix first.",
                               "MEDIUM": "Wrong or risky; affects indexing, UX or crawl efficiency.",
                               "LOW": "Hygiene issue; wastes crawl budget or weakens signals."}[severity],
                    "recommended_action": action,
                    "priority_rationale": f"{severity} severity across {count} page(s) (deterministic rule {rule}).",
                },
            }

        async with self.db.conn.execute(
                f"""SELECT rule_code, category, {worst}, subject_url, COUNT(*), GROUP_CONCAT(page_url, char(31)), MIN(detail)
                      FROM (SELECT rule_code, category, subject_url, page_url, MIN(severity) AS severity, MIN(detail) AS detail
                              FROM on_page_issues
                             WHERE job_id = ? AND category IN {dest_cats} AND severity IN ('HIGH','MEDIUM','LOW')
                               AND subject_url != ''
                             GROUP BY rule_code, category, subject_url, page_url)
                     GROUP BY rule_code, category, subject_url""", (job_id,)) as cur:
            for rule, cat, sev, subject, n, pages, detail in await cur.fetchall():
                findings.append(item(rule, cat, sev, subject, pages, n, detail, "destination"))
        async with self.db.conn.execute(
                f"""SELECT rule_code, category, {worst}, COUNT(DISTINCT page_url), MIN(page_url), MIN(detail)
                      FROM on_page_issues
                     WHERE job_id = ? AND (category NOT IN {dest_cats} OR subject_url = '')
                       AND severity IN ('HIGH','MEDIUM','LOW')
                     GROUP BY rule_code, category""", (job_id,)) as cur:
            rule_rows = await cur.fetchall()
        for rule, cat, sev, n, first_page, detail in rule_rows:
            if n > self.ON_PAGE_SITEWIDE_THRESHOLD:
                root = urllib.parse.urlunsplit(urllib.parse.urlsplit(first_page)[:2] + ("/", "", ""))
                async with self.db.conn.execute(
                        "SELECT DISTINCT page_url FROM on_page_issues WHERE job_id = ? AND rule_code = ? AND category = ? LIMIT 50",
                        (job_id, rule, cat)) as cur:
                    sample = "\x1f".join(r[0] for r in await cur.fetchall())
                findings.append(item(rule, cat, sev, root, sample, n, detail, "site-wide"))
            else:
                async with self.db.conn.execute(
                        f"SELECT page_url, {worst}, MIN(detail) FROM on_page_issues WHERE job_id = ? AND rule_code = ? "
                        "AND category = ? GROUP BY page_url", (job_id, rule, cat)) as cur:
                    for page, psev, pdetail in await cur.fetchall():
                        findings.append(item(rule, cat, psev if psev in self.ON_PAGE_SEVERITY else sev, page, page, 1,
                                             pdetail, "page"))
        return findings

    async def _analyze_link_destinations(self, job_id: str) -> List[Dict[str, Any]]:
        findings = []
        # Find all job_destinations where final_status_code is non-200 or status is not Working
        sql = """
            SELECT 
                d.normalized_url,
                d.final_status_code,
                d.status,
                d.error_type,
                d.error_message,
                d.response_time_ms,
                d.redirect_count
            FROM job_destinations d
            WHERE d.job_id = ? 
              AND (d.final_status_code != 200 OR d.status != 'Working')
        """
        async with self.db.conn.execute(sql, (job_id,)) as cur:
            dest_rows = await cur.fetchall()

        for row in dest_rows:
            target_url, status_code, status, err_type, err_msg, resp_time, redir_count = row
            
            # Fetch source URLs referencing this destination
            src_sql = "SELECT DISTINCT source_url FROM extracted_links WHERE job_id = ? AND normalized_url = ?"
            async with self.db.conn.execute(src_sql, (job_id, target_url)) as scur:
                src_rows = await scur.fetchall()
            source_urls = [r[0] for r in src_rows if r[0]]
            source_count = max(1, len(source_urls))

            # Determine severity
            if status_code and 500 <= status_code <= 599:
                severity = "CRITICAL" if source_count > 5 else "HIGH"
            elif status_code == 404:
                severity = "HIGH" if source_count > 1 else "MEDIUM"
            elif status_code in (401, 403, 410):
                severity = "HIGH"
            elif err_type and ("SSRF" in err_type.upper() or "BLOCKED" in err_type.upper()):
                severity = "CRITICAL"
            elif err_type and "TIMEOUT" in err_type.upper():
                severity = "HIGH" if source_count > 3 else "MEDIUM"
            else:
                severity = "MEDIUM"

            findings.append({
                "category": "BROKEN_LINK",
                "severity": severity,
                "target_url": target_url,
                "source_urls": source_urls,
                "source_count": source_count,
                "confidence": 1.0,
                "technical_details": {
                    "final_status_code": status_code,
                    "status": status,
                    "error_type": err_type,
                    "error_message": err_msg,
                    "response_time_ms": resp_time,
                    "redirect_count": redir_count
                }
            })
        return findings

    async def _analyze_redirects(self, job_id: str) -> List[Dict[str, Any]]:
        findings = []
        sql = """
            SELECT 
                d.normalized_url,
                d.final_url,
                d.redirect_count,
                d.redirect_chain,
                d.final_status_code,
                d.error_type
            FROM job_destinations d
            WHERE d.job_id = ? AND d.redirect_count >= 2
        """
        async with self.db.conn.execute(sql, (job_id,)) as cur:
            rows = await cur.fetchall()

        for row in rows:
            target_url, final_url, redir_count, chain_str, status_code, err_type = row
            
            src_sql = "SELECT DISTINCT source_url FROM extracted_links WHERE job_id = ? AND normalized_url = ?"
            async with self.db.conn.execute(src_sql, (job_id, target_url)) as scur:
                src_rows = await scur.fetchall()
            source_urls = [r[0] for r in src_rows if r[0]]
            source_count = max(1, len(source_urls))

            is_loop = (err_type and "LOOP" in err_type.upper()) or (target_url == final_url and redir_count >= 2)
            
            if is_loop:
                category = "REDIRECT_LOOP"
                severity = "CRITICAL"
            else:
                category = "REDIRECT_CHAIN"
                severity = "HIGH" if redir_count >= 4 or source_count > 10 else "MEDIUM"

            chain_data = []
            if chain_str:
                try: chain_data = json.loads(chain_str)
                except Exception: chain_data = [chain_str]

            findings.append({
                "category": category,
                "severity": severity,
                "target_url": target_url,
                "source_urls": source_urls,
                "source_count": source_count,
                "confidence": 0.95,
                "technical_details": {
                    "final_url": final_url,
                    "redirect_count": redir_count,
                    "redirect_chain": chain_data,
                    "final_status_code": status_code,
                    "error_type": err_type
                }
            })
        return findings

    async def _analyze_images(self, job_id: str) -> List[Dict[str, Any]]:
        findings = []
        # Check image_results table via extracted_links for this job
        try:
            sql = """
                SELECT 
                    e.source_url,
                    ir.normalized_url,
                    ir.final_status_code,
                    ir.status,
                    ir.error_message,
                    ir.image_format
                FROM extracted_links e
                JOIN image_results ir ON e.normalized_url = ir.normalized_url
                WHERE e.job_id = ? AND (ir.final_status_code != 200 OR ir.status != 'Working')
            """
            async with self.db.conn.execute(sql, (job_id,)) as cur:
                rows = await cur.fetchall()
            
            for row in rows:
                src_url, img_url, status_code, status, err_msg, img_fmt = row
                severity = "HIGH" if status_code == 404 else "MEDIUM"
                findings.append({
                    "category": "BROKEN_IMAGE",
                    "severity": severity,
                    "target_url": img_url,
                    "source_urls": [src_url] if src_url else [],
                    "source_count": 1,
                    "confidence": 0.95,
                    "technical_details": {
                        "final_status_code": status_code,
                        "status": status,
                        "error_message": err_msg,
                        "image_format": img_fmt
                    }
                })
        except Exception:
            pass
        return findings

    async def _analyze_alt_text(self, job_id: str) -> List[Dict[str, Any]]:
        findings = []
        try:
            sql = """
                SELECT 
                    source_url,
                    normalized_url,
                    image_name,
                    component_name
                FROM extracted_links
                WHERE job_id = ? 
                  AND (component_name LIKE '%IMG%' OR image_name IS NOT NULL)
                  AND (image_name IS NULL OR TRIM(image_name) = '' OR LOWER(TRIM(image_name)) IN ('image', 'img', 'untitled', 'null', 'undefined', 'icon'))
                LIMIT 100
            """
            async with self.db.conn.execute(sql, (job_id,)) as cur:
                rows = await cur.fetchall()

            for row in rows:
                src_url, norm_url, img_name, comp_name = row
                findings.append({
                    "category": "MISSING_ALT_TEXT",
                    "severity": "LOW",
                    "target_url": norm_url or src_url,
                    "source_urls": [src_url] if src_url else [],
                    "source_count": 1,
                    "confidence": 0.90,
                    "technical_details": {
                        "component_name": comp_name,
                        "image_name": img_name
                    }
                })
        except Exception:
            pass
        return findings

    async def _analyze_page_api_results(self, job_id: str) -> List[Dict[str, Any]]:
        findings = []
        # Page audit failures
        try:
            sql = """
                SELECT 
                    page_url,
                    final_url,
                    page_status,
                    http_status,
                    failed_api_count,
                    summary_reason,
                    error_message
                FROM page_audit_results
                WHERE job_id = ? AND (page_status != 'Working' OR failed_api_count > 0)
            """
            async with self.db.conn.execute(sql, (job_id,)) as cur:
                rows = await cur.fetchall()

            for row in rows:
                page_url, final_url, page_status, http_status, failed_api_cnt, summary_reason, err_msg = row
                if page_status != 'Working' or (http_status and http_status >= 400):
                    category = "PAGE_ERROR"
                    severity = "CRITICAL" if http_status and http_status >= 500 else ("HIGH" if http_status == 404 else "MEDIUM")
                else:
                    category = "FAILED_API"
                    severity = "HIGH" if failed_api_cnt > 0 else "MEDIUM"
                findings.append({
                    "category": category,
                    "severity": severity,
                    "target_url": page_url,
                    "source_urls": [page_url],
                    "source_count": 1,
                    "confidence": 0.95,
                    "technical_details": {
                        "final_url": final_url,
                        "page_status": page_status,
                        "http_status": http_status,
                        "failed_api_count": failed_api_cnt,
                        "summary_reason": summary_reason,
                        "error_message": err_msg
                    }
                })
        except Exception:
            pass

        # Specific page API subrequest failures
        try:
            api_sql = """
                SELECT 
                    page_url,
                    request_url,
                    http_status,
                    api_status,
                    error_message
                FROM page_api_responses
                WHERE job_id = ? AND (api_status IN ('API_ERROR', 'TIMEOUT') OR http_status >= 400)
            """
            async with self.db.conn.execute(api_sql, (job_id,)) as cur:
                api_rows = await cur.fetchall()

            for arow in api_rows:
                page_url, req_url, http_status, api_status, err_msg = arow
                findings.append({
                    "category": "FAILED_API",
                    "severity": "HIGH",
                    "target_url": req_url,
                    "source_urls": [page_url] if page_url else [],
                    "source_count": 1,
                    "confidence": 0.95,
                    "technical_details": {
                        "api_status": api_status,
                        "final_status_code": http_status,
                        "error_message": err_msg
                    }
                })
        except Exception:
            pass

        return findings

    async def _analyze_metadata(self, job_id: str) -> List[Dict[str, Any]]:
        findings = []
        try:
            sql = """
                SELECT DISTINCT 
                    e.source_url,
                    sc.page_type,
                    sc.business,
                    sc.classification_method,
                    sc.diagnostic_notes
                FROM extracted_links e
                JOIN source_classifications sc ON e.source_url = sc.source_url
                WHERE e.job_id = ? AND sc.page_type = 'NEEDS_REVIEW'
            """
            async with self.db.conn.execute(sql, (job_id,)) as cur:
                rows = await cur.fetchall()

            for row in rows:
                src_url, page_type, business, method, notes = row
                findings.append({
                    "category": "METADATA_ISSUE",
                    "severity": "INFO",
                    "target_url": src_url,
                    "source_urls": [src_url],
                    "source_count": 1,
                    "confidence": 0.85,
                    "technical_details": {
                        "classification": page_type,
                        "business": business,
                        "classification_method": method,
                        "reason": notes or "Needs Review"
                    }
                })
        except Exception:
            pass
        return findings
