import httpx
import asyncio
import time
import json
from datetime import datetime
from config import settings
from status_classifier import StatusClassifier
from models import UrlResult
from browser import BrowserPool
from urllib.parse import urlparse
from tuner import ConcurrencyTuner
from diagnostics_403 import Diagnostics403

class URLChecker:
    def __init__(self, browser_pool: BrowserPool):
        self.browser_pool = browser_pool
        limits = httpx.Limits(
            max_connections=settings.HTTP_CONCURRENCY * 2,
            max_keepalive_connections=settings.HTTP_CONCURRENCY,
            keepalive_expiry=30.0
        )
        timeout = httpx.Timeout(
            connect=settings.CONNECT_TIMEOUT,
            read=settings.READ_TIMEOUT,
            write=settings.WRITE_TIMEOUT,
            pool=settings.POOL_TIMEOUT
        )
        self.http_client = httpx.AsyncClient(
            verify=False,
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        self.domain_semaphores = {}
        self.tuner = ConcurrencyTuner(settings.HTTP_CONCURRENCY)

    async def stop(self):
        await self.http_client.aclose()

    def _get_domain_sem(self, url: str) -> asyncio.Semaphore:
        domain = urlparse(url).netloc
        if domain not in self.domain_semaphores:
            self.domain_semaphores[domain] = asyncio.Semaphore(settings.PER_DOMAIN_CONCURRENCY)
        return self.domain_semaphores[domain]

    async def check(self, url: str) -> UrlResult:
        result = UrlResult(normalized_url=url, original_url=url)
        domain_sem = self._get_domain_sem(url)
        
        await self.tuner.acquire()
        try:
            async with domain_sem:
                success = await self._http_check(result)
        finally:
            self.tuner.release()
            asyncio.create_task(self.tuner.record_result(result.final_status_code or 0, result.response_time_ms))
                
        # Playwright Fallback for suspicious HTTP responses
        if settings.BROWSER_FALLBACK:
            if not success or result.final_status_code in [401, 403, 429] or result.error_type:
                await self._browser_check(result)
                
        # Classification
        if result.browser_verified == "YES":
            result.status = "Browser Verified"
        else:
            cat = StatusClassifier.classify(result.final_status_code)
            if result.initial_status_code and 300 <= result.initial_status_code <= 399 and result.initial_status_code != result.final_status_code:
                result.status = f"Redirect -> {cat}"
            else:
                result.status = cat
                
        if result.error_type and result.browser_verified == "NO":
            result.status = result.error_type
            
        result.checked_at = datetime.utcnow().isoformat() + "Z"
        result.html = None # Immediately release raw HTML body to prevent memory retention at 500k scale
        return result

    async def _http_check(self, result: UrlResult) -> bool:
        start = time.time()
        retries = 0
        success = False
        
        while retries <= settings.MAX_RETRIES and not success:
            try:
                if settings.USE_HEAD_FIRST:
                    # In CLASSIFYING stage, we might need HTML.
                    # Since we are sharing fetch, we just use GET.
                    resp = await self.http_client.get(result.normalized_url)
                else:
                    resp = await self.http_client.get(result.normalized_url)
                    
                result.response_time_ms = round((time.time() - start) * 1000, 2)
                result.final_status_code = resp.status_code
                result.final_url = str(resp.url)
                
                try:
                    result.html = resp.text
                except Exception:
                    result.html = ""
                
                chain = [{"url": str(r.url), "status": r.status_code} for r in resp.history]
                result.redirect_chain = json.dumps(chain)
                result.redirect_count = len(chain)
                result.initial_status_code = chain[0]["status"] if chain else resp.status_code
                
                result.content_type = resp.headers.get("content-type", "")
                result.content_length = int(resp.headers.get("content-length", 0))
                
                result.check_method = "HTTP"
                result.error_type = ""
                result.error_message = ""
                
                if resp.status_code in [502, 503, 504]:
                    retries += 1
                    await asyncio.sleep(2 ** retries)
                elif resp.status_code == 429:
                    retry_after = int(resp.headers.get("retry-after", 2))
                    retries += 1
                    await asyncio.sleep(retry_after)
                else:
                    success = True
                    
            except Exception as e:
                result.response_time_ms = round((time.time() - start) * 1000, 2)
                err = str(e).lower()
                if "timeout" in err: result.error_type = "Timeout"
                elif "dns" in err or "name_not_resolved" in err or "resolve" in err: result.error_type = "DNS Error"
                elif "connect" in err: result.error_type = "Connection Error"
                elif "ssl" in err or "cert" in err: result.error_type = "SSL Error"
                else: result.error_type = "Network Error"
                result.error_message = str(e)
                
                retries += 1
                await asyncio.sleep(2 ** retries)
                
        return success

    async def _browser_check(self, result: UrlResult):
        start = time.time()
        page = await self.browser_pool.get_page()
        if not page:
            return
            
        try:
            chain = []
            def handle_response(response):
                if response.request.resource_type == "document" and response.status in [301, 302, 303, 307, 308]:
                    chain.append({"url": response.url, "status": response.status})
                    
            page.on("response", handle_response)
            
            try:
                response = await page.goto(
                    result.normalized_url,
                    wait_until="domcontentloaded",
                    timeout=30000
                )
                
                result.response_time_ms = round((time.time() - start) * 1000, 2)
                
                if response:
                    result.final_status_code = response.status
                    result.final_url = response.url
                    try:
                        result.html = await page.content()
                    except Exception:
                        result.html = ""
                    result.redirect_chain = json.dumps(chain)
                    result.redirect_count = len(chain)
                    result.initial_status_code = chain[0]["status"] if chain else response.status
                    
                    body_indicators = Diagnostics403.inspect_html_body(result.html)
                    if response.status < 400 and not body_indicators.get("challenge_detected"):
                        result.browser_verified = "YES"
                        result.error_type = ""
                        result.error_message = ""
                    else:
                        result.browser_verified = "NO"
                        if body_indicators.get("challenge_detected"):
                            result.error_type = "BROWSER_CHALLENGE_BLOCKED"
                            result.error_message = "Browser challenge detected (CAPTCHA / WAF verification); blocked."
                        else:
                            result.error_type = "BROWSER_HTTP_ERROR"
                            result.error_message = f"HTTP {response.status}"
                        
                result.check_method = "HTTP + PLAYWRIGHT"
            except Exception as e:
                result.response_time_ms = round((time.time() - start) * 1000, 2)
                err = str(e).lower()
                if "timeout" in err: result.error_type = "Timeout"
                elif "ssl" in err: result.error_type = "SSL Error"
                elif "name_not_resolved" in err: result.error_type = "DNS Error"
                else: result.error_type = "Playwright Error"
                result.error_message = str(e)
            finally:
                page.remove_listener("response", handle_response)
        finally:
            self.browser_pool.release_page(page)

    async def check_image(self, url: str) -> dict:
        source_platform = "Unknown"
        url_lower = url.lower()
        if "mc.bajajfinserv.in" in url_lower:
            source_platform = "Commerce"
        elif "www.bajajfinserv.in" in url_lower:
            source_platform = "AEM dam"
        elif "cms-assets.bajajfinserv.in" in url_lower:
            source_platform = "dynamic media"
            
        result = {
            "normalized_url": url,
            "initial_status_code": None,
            "final_status_code": None,
            "status": "Failed",
            "image_format": None,
            "image_size_kb": None,
            "is_dam_asset": any(p in url for p in settings.DAM_PATTERNS),
            "source_platform": source_platform,
            "error_type": "",
            "error_message": "",
            "checked_at": datetime.utcnow().isoformat() + "Z"
        }
        
        domain_sem = self._get_domain_sem(url)
        
        start_time = time.time()
        
        await self.tuner.acquire()
        try:
            async with domain_sem:
                try:
                    # Use streaming to avoid downloading large images fully
                    async with self.http_client.stream("GET", url) as resp:
                        result["final_status_code"] = resp.status_code
                        result["initial_status_code"] = resp.history[0].status_code if resp.history else resp.status_code
                        
                        cl = resp.headers.get("content-length")
                        if cl and cl.isdigit():
                            result["image_size_kb"] = round(int(cl) / 1024, 2)
                            
                        ct = resp.headers.get("content-type", "").lower()
                        if "image/" in ct:
                            result["image_format"] = ct.split("image/")[-1].split(";")[0].upper()
                        else:
                            # fallback to extension
                            ext = url.split(".")[-1].split("?")[0].upper()
                            result["image_format"] = ext if len(ext) <= 4 else "UNKNOWN"
                            
                        if resp.status_code < 400:
                            result["status"] = "Working"
                        else:
                            cat = StatusClassifier.classify(resp.status_code)
                            result["status"] = "Broken Image" if "Broken" in cat else cat
                except Exception as e:
                    err = str(e).lower()
                    if "timeout" in err: result["error_type"] = "Timeout"; result["status"] = "Timeout"
                    elif "connect" in err: result["error_type"] = "Connection Error"; result["status"] = "Connection Error"
                    elif "dns" in err or "resolve" in err: result["error_type"] = "DNS Error"; result["status"] = "DNS Error"
                    else: result["error_type"] = "Network Error"; result["status"] = "Failed"
                    result["error_message"] = str(e)
        finally:
            self.tuner.release()
            latency = (time.time() - start_time) * 1000
            asyncio.create_task(self.tuner.record_result(result["final_status_code"] or 0, latency))
            
        return result
