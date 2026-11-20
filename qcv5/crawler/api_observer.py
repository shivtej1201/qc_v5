import os
import re
import time
import json
import hashlib
import asyncio
import urllib.parse
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

from qcv5.core.config import settings
from qcv5.crawler.browser import BrowserPool
from qcv5.crawler.url_normalizer import URLNormalizer
from qcv5.core.models import PageAuditResult, PageApiResponse

class ApiResponseObserver:
    TELEMETRY_DOMAINS = [
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "hotjar.com", "clarity.ms", "facebook.net", "sentry.io",
        "segment.io", "datadoghq.com", "newrelic.com", "mixpanel.com"
    ]

    def __init__(self, browser_pool: BrowserPool):
        self.browser_pool = browser_pool
        self.redacted_patterns = [
            (re.compile(r'(?i)(bearer\s+)[a-zA-Z0-9_\-\.]+'), r'\1[REDACTED]'),
            (re.compile(r'(?i)(basic\s+)[a-zA-Z0-9+/=]+'), r'\1[REDACTED]'),
            (re.compile(r'(?i)(cookie:\s*.*?(?:session|token|auth)=)[^;\s]+'), r'\1[REDACTED]'),
            (re.compile(r'(?i)([?&]|\b)(token|key|secret|password|pwd|auth|api[_\-]?key|access[_\-]?token|refresh[_\-]?token|session[_\-]?id|jwt|private[_\-]?key|signature|cvv)=([^&\s]+)'), r'\1\2=[REDACTED]'),
            (re.compile(r'(?i)"([^"]*(?:token|secret|password|pwd|api[_\-]?key|auth|jwt|cred|cookie|session|cvv|card)[^"]*)"\s*:\s*("(?:[^"\\]|\\.)*"|[0-9]+|true|false|null)'), r'"\1":"[REDACTED]"'),
        ]

    def redact_text(self, text: str) -> str:
        if not text:
            return ""
        redacted = text
        for pattern, repl in self.redacted_patterns:
            redacted = pattern.sub(repl, redacted)
        return redacted

    def redact_url(self, url: str) -> str:
        if not url:
            return ""
        try:
            parsed = urllib.parse.urlsplit(url)
            query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            safe_pairs = []
            sensitive_fragments = [
                "token", "secret", "password", "pwd", "auth", "api_key", "apikey",
                "signature", "jwt", "privkey", "private_key", "cvv", "card", "session", "credential"
            ]
            for k, v in query_pairs:
                k_clean = k.lower().replace("-", "_")
                if any(s in k_clean for s in sensitive_fragments):
                    safe_pairs.append((k, "[REDACTED]"))
                else:
                    # Also check if value itself contains bearer or jwt signature
                    safe_pairs.append((k, self.redact_text(v)))
            new_query = urllib.parse.urlencode(safe_pairs)
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
        except Exception:
            return self.redact_text(url)

    def is_telemetry_api(self, url: str) -> bool:
        low = url.lower()
        return any(d in low for d in self.TELEMETRY_DOMAINS)

    def _inspect_json_structure(self, data: Any, depth: int = 0) -> Tuple[Optional[str], Optional[str]]:
        """
        Recursively inspects JSON data up to depth 3 for collection counts and items.
        Returns (status, reason) if a conclusive signal is found, else (None, None).
        """
        if depth > 3 or data is None:
            return None, None
            
        if isinstance(data, list):
            if len(data) == 0:
                return "NOT_FOUND", "API returned empty array []"
            return "FOUND", f"API returned array with {len(data)} items"
            
        if isinstance(data, dict):
            # 1. Explicit error signals
            if data.get("error") or data.get("errors"):
                err_msg = str(data.get("error") or data.get("errors"))
                return "API_ERROR", f"API payload error: {err_msg[:100]}"
            if str(data.get("status", "")).lower() in ["error", "fail", "failed"]:
                return "API_ERROR", "API status indicates failure"
            if str(data.get("status", "")).lower() in ["not_found", "empty", "zero_results"]:
                return "NOT_FOUND", "API payload status indicates not_found"

            # 2. Check total / count fields
            zero_count_keys = ["total", "count", "totalrecords", "totalitems", "total_records", "total_items", "totalresults", "recordstotal", "recordsfiltered", "size"]
            for k in zero_count_keys:
                if k in data and data[k] == 0:
                    return "NOT_FOUND", f"API indicated {k}: 0"
                if k in data and isinstance(data[k], (int, float)) and data[k] > 0:
                    return "FOUND", f"API indicated {k}: {int(data[k])}"

            # 3. Check list collections
            list_keys = ["items", "products", "results", "records", "data", "list", "content", "rows", "articles"]
            for k in list_keys:
                if k in data:
                    val = data[k]
                    if isinstance(val, list):
                        if len(val) == 0:
                            return "NOT_FOUND", f"API returned empty list in '{k}'"
                        return "FOUND", f"API returned {len(val)} items in '{k}'"
                    elif isinstance(val, dict):
                        res, reason = self._inspect_json_structure(val, depth + 1)
                        if res:
                            return res, reason

            # 4. Check nested response / data / result objects
            container_keys = ["response", "result", "payload", "body", "pagination"]
            for k in container_keys:
                if k in data and isinstance(data[k], dict):
                    res, reason = self._inspect_json_structure(data[k], depth + 1)
                    if res:
                        return res, reason

        return None, None

    def _evaluate_api_payload(self, status_code: int, snippet: str, content_type: str) -> Tuple[str, str]:
        """
        Evaluates single API response payload into (api_status, reason)
        api_status can be: FOUND, NOT_FOUND, API_ERROR, TIMEOUT, INCONCLUSIVE
        """
        if status_code >= 500:
            return "API_ERROR", f"HTTP {status_code} Server Error"
        if status_code == 404:
            return "NOT_FOUND", "HTTP 404 Not Found"
        if status_code >= 400:
            return "API_ERROR", f"HTTP {status_code} Client Error"

        if not snippet or not snippet.strip():
            return "INCONCLUSIVE", "Empty response payload"

        lower = snippet.lower()
        if "application/json" in content_type.lower() or lower.startswith("{") or lower.startswith("["):
            try:
                data = json.loads(snippet)
                status, reason = self._inspect_json_structure(data)
                if status:
                    return status, reason
                return "FOUND", "API returned valid JSON object"
            except Exception:
                pass

        # String-based patterns
        if any(term in lower for term in ["\"count\":0", "\"total\":0", "\"totalrecords\":0", "\"items\":[]", "\"products\":[]", "\"data\":[]"]):
            return "NOT_FOUND", "API response indicates zero records"

        if 200 <= status_code < 300:
            return "FOUND", f"HTTP {status_code} Success"

        return "INCONCLUSIVE", f"HTTP {status_code}"

    def _evaluate_page_content(self, body_text: str) -> Tuple[bool, str, str]:
        """
        Inspects page DOM text for empty-state phrases or unusable content.
        Returns (is_problem, problem_type, reason) where problem_type can be 'NOT_FOUND' or 'PAGE_ERROR'.
        """
        if not body_text or not body_text.strip():
            return True, "PAGE_ERROR", "Page rendered empty or blank body content"

        lower = body_text.lower()
        
        # 1. Unusable content signals (maintenance, access denied, server crash screen)
        unusable_indicators = [
            ("under maintenance", "Page indicates system is under maintenance"),
            ("service temporarily unavailable", "Page indicates service is temporarily unavailable"),
            ("access denied", "Page indicates access denied"),
            ("403 forbidden", "Page rendered 403 Forbidden"),
            ("checking your browser before accessing", "Page stalled on security / bot challenge screen"),
            ("we are having trouble loading this page", "Page displayed load failure screen"),
            ("something went wrong", "Page displayed 'Something went wrong' crash state")
        ]
        for phrase, reason in unusable_indicators:
            if phrase in lower:
                return True, "PAGE_ERROR", reason

        # 2. Empty-state signals
        empty_indicators = [
            "no products found",
            "no results found",
            "0 results found",
            "we couldn't find any results",
            "we could not find any results",
            "no items found",
            "no records found",
            "product not available",
            "page not found",
            "404 - page not found",
            "404 not found",
            "out of stock",
            "zero results"
        ]
        for phrase in empty_indicators:
            if phrase in lower:
                return True, "NOT_FOUND", f"Page displayed empty state: '{phrase}'"

        return False, "", ""

    async def audit_page(self, job_id: str, page_url: str, batch_id: str = "", allow_local: bool = False) -> Tuple[PageAuditResult, List[PageApiResponse]]:
        evaluated_at = datetime.utcnow().isoformat() + "Z"
        clean_page_url = self.redact_url(page_url)

        # 1. Initial SSRF & URL safety check
        if not URLNormalizer.is_safe_url(page_url, allow_local=allow_local):
            result = PageAuditResult(
                job_id=job_id,
                batch_id=batch_id,
                page_url=clean_page_url,
                page_status="SKIPPED",
                http_status=None,
                response_time_ms=0,
                error_message="URL blocked by SSRF / safety validation",
                screenshot_path="",
                api_count=0,
                failed_api_count=0,
                summary_reason="URL skipped due to security policy or invalid scheme",
                evaluated_at=evaluated_at
            )
            return result, []

        page = await self.browser_pool.get_page()
        if not page:
            result = PageAuditResult(
                job_id=job_id,
                batch_id=batch_id,
                page_url=clean_page_url,
                page_status="PAGE_ERROR",
                http_status=None,
                response_time_ms=0,
                error_message="Browser pool capacity unavailable",
                screenshot_path="",
                api_count=0,
                failed_api_count=0,
                summary_reason="Could not obtain browser page from pool",
                evaluated_at=evaluated_at
            )
            return result, []

        observed_apis: List[PageApiResponse] = []
        observed_api_urls = set()
        request_start_times: Dict[str, float] = {}
        active_requests = 0
        is_main_aborted_by_ssrf = False

        # 2. SSRF Route Interception for ALL subrequests & redirects
        async def handle_route(route, request):
            nonlocal is_main_aborted_by_ssrf
            req_url = request.url
            if not URLNormalizer.is_safe_url(req_url, allow_local=allow_local):
                if request.is_navigation_request():
                    is_main_aborted_by_ssrf = True
                try:
                    await route.abort("blockedbyclient")
                except Exception:
                    pass
                return
            try:
                await route.continue_()
            except Exception:
                pass

        # Request listener
        def handle_request(req):
            nonlocal active_requests
            try:
                res_type = req.resource_type
                if res_type in ["fetch", "xhr"]:
                    active_requests += 1
                    req_id = f"{req.method}_{req.url}"
                    request_start_times[req_id] = time.time()
            except Exception:
                pass

        # Request failed listener
        def handle_request_failed(req):
            nonlocal active_requests
            try:
                res_type = req.resource_type
                if res_type in ["fetch", "xhr"]:
                    if active_requests > 0:
                        active_requests -= 1
                    req_id = f"{req.method}_{req.url}"
                    start_t = request_start_times.get(req_id, time.time())
                    dur_ms = round((time.time() - start_t) * 1000, 2)
                    err_text = req.failure or "Request failed"
                    sanitized_url = self.redact_url(req.url)
                    
                    if req.url not in observed_api_urls:
                        observed_api_urls.add(req.url)
                        observed_apis.append(PageApiResponse(
                            job_id=job_id,
                            page_url=clean_page_url,
                            request_url=sanitized_url,
                            method=req.method,
                            resource_type=res_type,
                            http_status=None,
                            api_status="API_ERROR",
                            response_time_ms=dur_ms,
                            content_type="",
                            error_message=self.redact_text(err_text),
                            response_snippet="",
                            observed_at=datetime.utcnow().isoformat() + "Z"
                        ))
            except Exception:
                pass

        # Response listener
        async def handle_response(resp):
            nonlocal active_requests, is_main_aborted_by_ssrf
            try:
                # Intercept unsafe redirect targets immediately
                if resp.status in [301, 302, 303, 307, 308]:
                    loc = resp.headers.get("location")
                    if loc:
                        abs_loc = urllib.parse.urljoin(resp.url, loc)
                        if not URLNormalizer.is_safe_url(abs_loc, allow_local=allow_local):
                            is_main_aborted_by_ssrf = True
                            if not page.is_closed():
                                asyncio.create_task(page.close())
                            return

                req = resp.request
                res_type = req.resource_type
                if res_type in ["fetch", "xhr"]:
                    if active_requests > 0:
                        active_requests -= 1
                    req_id = f"{req.method}_{req.url}"
                    start_t = request_start_times.get(req_id, time.time())
                    dur_ms = round((time.time() - start_t) * 1000, 2)
                    status_code = resp.status
                    content_type = resp.headers.get("content-type", "")
                    
                    snippet = ""
                    try:
                        if any(ct in content_type.lower() for ct in ["json", "text", "javascript", "xml"]):
                            cl = resp.headers.get("content-length")
                            if cl and int(cl) > settings.MAX_PAYLOAD_SIZE_BYTES:
                                snippet = f"[Payload exceeded {settings.MAX_PAYLOAD_SIZE_BYTES} bytes limit]"
                            else:
                                body_bytes = await resp.body()
                                if len(body_bytes) > settings.MAX_PAYLOAD_SIZE_BYTES:
                                    snippet = f"[Payload exceeded {settings.MAX_PAYLOAD_SIZE_BYTES} bytes limit]"
                                else:
                                    text = body_bytes.decode('utf-8', errors='replace')[:800]
                                    snippet = self.redact_text(text)
                    except Exception:
                        snippet = ""

                    api_status, reason = self._evaluate_api_payload(status_code, snippet, content_type)
                    sanitized_url = self.redact_url(resp.url)
                    
                    if resp.url not in observed_api_urls:
                        observed_api_urls.add(resp.url)
                        observed_apis.append(PageApiResponse(
                            job_id=job_id,
                            page_url=clean_page_url,
                            request_url=sanitized_url,
                            method=req.method,
                            resource_type=res_type,
                            http_status=status_code,
                            api_status=api_status,
                            response_time_ms=dur_ms,
                            content_type=content_type[:100],
                            error_message=reason if api_status in ["API_ERROR", "NOT_FOUND"] else None,
                            response_snippet=snippet[:400] if snippet else "",
                            observed_at=datetime.utcnow().isoformat() + "Z"
                        ))
            except Exception:
                pass

        # Register route handler for subrequests & redirects
        popup_handler = lambda popup: asyncio.create_task(popup.close())
        page.on("popup", popup_handler)
        await page.route("**/*", handle_route)
        page.on("request", handle_request)
        page.on("requestfailed", handle_request_failed)
        page.on("response", handle_response)

        start_time = time.time()
        final_url = clean_page_url
        main_status_code = None
        error_msg = None
        page_status = "INCONCLUSIVE"
        summary_reason = ""
        screenshot_path = ""

        try:
            # Navigate to page
            timeout_ms = settings.API_AUDIT_TIMEOUT
            response = await page.goto(page_url, timeout=timeout_ms, wait_until="domcontentloaded")
            if response:
                main_status_code = response.status
                final_url = self.redact_url(page.url)

            # Dynamic settle loop: wait for pending Fetch/XHR requests to finish up to max settle threshold
            max_settle = settings.API_AUDIT_SETTLE_MS / 1000.0
            step = 0.2
            settle_elapsed = 0.0
            # Allow 1.2s for SPA framework mounting and delayed fetch calls to trigger
            await asyncio.sleep(1.2)
            settle_elapsed += 1.2
            while settle_elapsed < max_settle:
                if active_requests == 0:
                    await asyncio.sleep(0.3)
                    if active_requests == 0:
                        break
                await asyncio.sleep(step)
                settle_elapsed += step

            response_time_ms = round((time.time() - start_time) * 1000, 2)

            # Evaluate Page Level
            if main_status_code and main_status_code >= 500:
                page_status = "PAGE_ERROR"
                summary_reason = f"Main page returned HTTP {main_status_code} server error"
            elif main_status_code == 404:
                page_status = "NOT_FOUND"
                summary_reason = "Main page returned HTTP 404 not found"
            else:
                try:
                    body_text = await page.inner_text("body", timeout=3000)
                except Exception:
                    body_text = ""

                is_problem, problem_type, problem_reason = self._evaluate_page_content(body_text)

                # Separate primary APIs from optional telemetry APIs
                primary_apis = [a for a in observed_apis if not self.is_telemetry_api(a.request_url)]
                failed_primary = [a for a in primary_apis if a.api_status == "API_ERROR"]
                not_found_primary = [a for a in primary_apis if a.api_status == "NOT_FOUND"]
                found_primary = [a for a in primary_apis if a.api_status == "FOUND"]

                failed_all = [a for a in observed_apis if a.api_status == "API_ERROR"]

                if is_problem:
                    if problem_type == "PAGE_ERROR" and failed_primary and not found_primary:
                        page_status = "API_ERROR"
                        summary_reason = f"Critical API failure: {failed_primary[0].error_message}"
                    else:
                        page_status = problem_type
                        summary_reason = problem_reason
                elif not_found_primary and not found_primary:
                    page_status = "NOT_FOUND"
                    summary_reason = f"Primary API returned zero records: {not_found_primary[0].error_message or 'empty results'}"
                elif failed_primary and not found_primary and len(body_text.strip()) < 30:
                    page_status = "API_ERROR"
                    summary_reason = f"Critical API failure: {failed_primary[0].error_message}"
                elif found_primary or (len(body_text.strip()) >= 20 and not is_problem):
                    page_status = "FOUND"
                    if failed_all:
                        summary_reason = f"Page loaded successfully with content ({len(failed_all)} optional/telemetry APIs had non-fatal errors)"
                    else:
                        summary_reason = f"Page loaded successfully with active content ({len(observed_apis)} APIs observed)"
                else:
                    page_status = "INCONCLUSIVE"
                    summary_reason = "Page loaded but lacked definitive success or failure signals"

        except Exception as e:
            err_str = str(e)
            response_time_ms = round((time.time() - start_time) * 1000, 2)
            error_msg = self.redact_text(err_str)
            if is_main_aborted_by_ssrf or "ERR_BLOCKED_BY_CLIENT" in err_str or "ERR_ABORTED" in err_str or page.is_closed():
                page_status = "SKIPPED"
                summary_reason = "URL or redirect blocked by SSRF policy"
            elif "Timeout" in err_str or "timed out" in err_str.lower():
                page_status = "TIMEOUT"
                summary_reason = f"Page navigation/settling timed out after {settings.API_AUDIT_TIMEOUT}ms"
            else:
                page_status = "PAGE_ERROR"
                summary_reason = f"Page load exception: {error_msg[:120]}"

        # Screenshot capture for relevant states: NOT_FOUND, API_ERROR, PAGE_ERROR, TIMEOUT
        if not page.is_closed() and page_status in ["NOT_FOUND", "API_ERROR", "PAGE_ERROR", "TIMEOUT"]:
            try:
                # Mask sensitive input fields in DOM prior to taking screenshot
                try:
                    await page.evaluate("""() => {
                        const inputs = document.querySelectorAll('input[type="password"], input[name*="pass"], input[name*="cvv"], input[name*="card"], input[name*="secret"]');
                        inputs.forEach(el => { el.value = '••••'; });
                    }""")
                except Exception:
                    pass

                screenshots_dir = os.path.join(settings.ARTIFACTS_DIR, job_id, "screenshots")
                os.makedirs(screenshots_dir, exist_ok=True)
                
                url_hash = hashlib.sha256(page_url.encode('utf-8')).hexdigest()[:12]
                filename = f"{url_hash}_{int(time.time())}.png"
                target_file = os.path.join(screenshots_dir, filename)
                
                await page.screenshot(path=target_file, full_page=False, timeout=5000)
                screenshot_path = target_file.replace("\\", "/")
            except Exception:
                pass

        # Cleanup route & listeners and release page
        try:
            if not page.is_closed():
                await page.unroute("**/*")
                page.remove_listener("popup", popup_handler)
                page.remove_listener("request", handle_request)
                page.remove_listener("requestfailed", handle_request_failed)
                page.remove_listener("response", handle_response)
        except Exception:
            pass
        finally:
            self.browser_pool.release_page(page)

        failed_count = sum(1 for api in observed_apis if api.api_status in ["API_ERROR", "TIMEOUT"])
        
        result = PageAuditResult(
            job_id=job_id,
            batch_id=batch_id,
            page_url=clean_page_url,
            final_url=final_url,
            page_status=page_status,
            http_status=main_status_code,
            response_time_ms=response_time_ms,
            error_message=error_msg,
            screenshot_path=screenshot_path,
            api_count=len(observed_apis),
            failed_api_count=failed_count,
            summary_reason=summary_reason,
            evaluated_at=evaluated_at
        )

        return result, observed_apis
