import asyncio
import gzip
import io
import re
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from typing import Dict, List, Set, Any, Optional, Tuple
import httpx

from qcv5.core.config import settings
from qcv5.crawler.url_normalizer import URLNormalizer
from qcv5.crawler.diagnostics_403 import Diagnostics403

def _strip_tag_ns(tag: str) -> str:
    """Strip XML namespace prefix from tag name."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag

class RobotsManager:
    """Caches and evaluates robots.txt policies per domain with SSRF validation."""
    def __init__(self, http_client: httpx.AsyncClient, user_agent: str = "Mozilla/5.0",
                 allow_local: Optional[bool] = None):
        self.http_client = http_client
        self.allow_local = getattr(settings, "ALLOW_LOCAL_TESTING", False) if allow_local is None else allow_local
        self.user_agent = user_agent
        self.parsers: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self.lock = asyncio.Lock()

    async def get_parser(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        
        async with self.lock:
            if origin in self.parsers:
                return self.parsers[origin]

            robots_url = f"{origin}/robots.txt"
            if not URLNormalizer.is_safe_url(robots_url, allow_local=self.allow_local):
                self.parsers[origin] = None
                return None

            parser = urllib.robotparser.RobotFileParser()
            try:
                browser_headers = {  # the same identity as every other request (the client's headers + this UA)
                    "User-Agent": self.user_agent,
                    "Accept": "text/plain,text/html,*/*"
                }
                resp = await self.http_client.get(robots_url, headers=browser_headers, timeout=5.0)
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                    self.parsers[origin] = parser
                    return parser
                elif resp.status_code in [401, 403]:
                    # Site explicitly forbids robots.txt or access
                    parser.parse(["User-agent: *", "Disallow: /"])
                    self.parsers[origin] = parser
                    return parser
                else:
                    # 404 or other non-200 generally implies allow all
                    parser.parse(["User-agent: *", "Allow: /"])
                    self.parsers[origin] = parser
                    return parser
            except Exception:
                # Network error or timeout on robots.txt: allow by default or empty
                parser.parse(["User-agent: *", "Allow: /"])
                self.parsers[origin] = parser
                return parser

    async def is_allowed(self, url: str) -> bool:
        """Check if URL is permitted by target domain's robots.txt."""
        if not getattr(settings, "RESPECT_ROBOTS", True):
            return True
        try:
            parser = await self.get_parser(url)
            if not parser:
                return False
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True


class SitemapExtractor:
    """
    Robust, safe XML sitemap parser supporting:
    - Normal <urlset> sitemaps
    - <sitemapindex> recursive child sitemap discovery
    - Gzip-compressed sitemaps (.xml.gz)
    - Full SSRF protection across all requests and redirects
    - Robots.txt checking
    - URL deduplication and normalization
    - Configurable size, depth, URL count, and timeout limits
    - Root cause 403 and access diagnostics
    """

    def __init__(
        self,
        max_depth: int = 5,
        max_response_size: int = 50 * 1024 * 1024, # 50 MB: the sitemaps.org per-file limit (uncompressed)
        max_urls: int = 300000, # total across an index: enough for a 300K-page audit
        timeout: float = 15.0,
        concurrency: int = 5,
        allow_local: bool = False,
        browser_pool: Optional[Any] = None,
        use_browser_fallback: bool = True
    ):
        self.max_depth = max_depth
        self.max_response_size = max_response_size
        self.max_urls = max_urls
        self.timeout = timeout
        self.concurrency = concurrency
        self.allow_local = allow_local
        self.browser_pool = browser_pool
        self.use_browser_fallback = use_browser_fallback
        self._owned_browser_pool = False
        
        self.client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(connect=5.0, read=self.timeout, write=5.0, pool=5.0),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LinkAuditorBot/2.0"},
            follow_redirects=False # Manual redirect tracking for SSRF check
        )
        self.robots_manager = RobotsManager(self.client, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) LinkAuditorBot/2.0",
                                            allow_local=allow_local or None)
        self.semaphore = asyncio.Semaphore(self.concurrency)

    async def close(self):
        await self.client.aclose()
        if self._owned_browser_pool and self.browser_pool:
            try:
                await self.browser_pool.stop()
            except Exception:
                pass
            self.browser_pool = None

    @staticmethod
    def _safe_parse_xml(content_bytes: bytes, max_nesting_depth: int = 50) -> ET.Element:
        """
        Safely parse XML bytes, preventing entity expansion, XXE vulnerabilities,
        and excessive element nesting.
        """
        if not content_bytes or not content_bytes.strip():
            raise ValueError("Empty XML content")

        header_check = content_bytes[:2048].lower()
        if b"<!doctype" in header_check and b"<!entity" in header_check:
            if b"system" in header_check or b"public" in header_check:
                raise ValueError("External entity declarations in XML DTD are disallowed for security.")
                
        # Parse XML root
        try:
            parser = ET.XMLParser()
            root = ET.fromstring(content_bytes, parser=parser)
        except ET.ParseError as e:
            raise ValueError(f"Malformed XML: {e}")

        # Enforce maximum nesting depth to prevent stack overflow/DOS
        def _check_nesting(element: ET.Element, current_depth: int):
            if current_depth > max_nesting_depth:
                raise ValueError(f"Excessive XML nesting depth exceeded limit of {max_nesting_depth} levels.")
            for child in element:
                _check_nesting(child, current_depth + 1)

        _check_nesting(root, 1)
        return root

    @classmethod
    def _decompress_if_gzip(cls, data: bytes, url_or_filename: str = "", max_size: int = 10 * 1024 * 1024) -> bytes:
        """
        Safely decompress gzip content with strict streaming size checks against
        compression bombs, truncated streams, and malformed headers.
        """
        is_magic = len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B
        is_ext = url_or_filename.lower().endswith(".gz")
        
        if not is_magic and not is_ext:
            return data

        if len(data) < 10:
            raise ValueError("Corrupted or truncated gzip stream (header too short).")

        import zlib
        try:
            # 16 + zlib.MAX_WBITS specifies gzip format with standard header and CRC32
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decompressed_chunks = []
            total_decompressed = 0
            chunk_size = 64 * 1024

            for i in range(0, len(data), chunk_size):
                input_chunk = data[i:i + chunk_size]
                out = decompressor.decompress(input_chunk, max_size - total_decompressed + 1)
                total_decompressed += len(out)
                if total_decompressed > max_size:
                    raise ValueError(f"Decompressed XML size exceeded maximum limit of {max_size} bytes (compression bomb detected).")
                decompressed_chunks.append(out)

            tail = decompressor.flush(max_size - total_decompressed + 1)
            total_decompressed += len(tail)
            if total_decompressed > max_size:
                raise ValueError(f"Decompressed XML size exceeded maximum limit of {max_size} bytes (compression bomb detected).")
            decompressed_chunks.append(tail)

            if not decompressor.eof:
                # Incomplete gzip stream or trailing garbage
                raise ValueError("Corrupted or truncated gzip stream (unexpected end of compressed data).")

            return b"".join(decompressed_chunks)

        except ValueError:
            raise
        except Exception as e:
            # Also try standard gzip.decompress as fallback if zlib header variant was used,
            # but still strictly enforce output length
            try:
                out = gzip.decompress(data)
                if len(out) > max_size:
                    raise ValueError(f"Decompressed XML size exceeded maximum limit of {max_size} bytes (compression bomb detected).")
                return out
            except ValueError:
                raise
            except Exception:
                raise ValueError(f"Corrupted or truncated gzip stream: {e}")

    async def _fetch_with_browser(self, url: str) -> Tuple[bytes, int, Dict[str, str], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Human-like browser fallback using Playwright Chromium when HTTP client
        is blocked by edge WAF (e.g. Akamai bot protection).
        Preserves SSRF protection, redirect tracking, and max size limits.
        """
        if not URLNormalizer.is_safe_url(url, allow_local=self.allow_local):
            diag = Diagnostics403.classify(
                status_code=403,
                headers={},
                stage="sitemap_browser_download",
                robots_allowed=True,
                error_type="SSRF_PROHIBITED"
            )
            return b"", 403, {}, [], diag

        redirect_chain = []
        page = None
        try:
            if not self.browser_pool or not getattr(self.browser_pool, "context", None):
                from qcv5.crawler.browser import BrowserPool
                self.browser_pool = BrowserPool()
                await self.browser_pool.start()
                self._owned_browser_pool = True

            page = await self.browser_pool.get_page()
            if not page:
                diag = Diagnostics403.classify(
                    status_code=503,
                    headers={},
                    stage="sitemap_browser_download",
                    error_type="Browser page unavailable"
                )
                return b"", 503, {}, [], diag

            def handle_response(response):
                if response.request.resource_type == "document" and response.status in [301, 302, 303, 307, 308]:
                    redirect_chain.append({"url": response.url, "status": response.status})

            page.on("response", handle_response)

            try:
                timeout_ms = int(max(self.timeout, 30.0) * 1000)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if not response:
                    diag = Diagnostics403.classify(
                        status_code=0,
                        headers={},
                        stage="sitemap_browser_download",
                        error_type="No response from browser"
                    )
                    return b"", 0, {}, redirect_chain, diag

                status_code = response.status
                headers = dict(response.headers)
                body = await response.body()

                # SSRF validation on destination URL after browser redirects
                if response.url and response.url != url:
                    if not URLNormalizer.is_safe_url(response.url, allow_local=self.allow_local):
                        diag = Diagnostics403.classify(
                            status_code=403,
                            headers=headers,
                            stage="sitemap_browser_download",
                            error_type="SSRF_PROHIBITED"
                        )
                        return b"", 403, {}, redirect_chain, diag

                # Check max response size
                if len(body) > self.max_response_size:
                    raise ValueError(f"Response size exceeded limit of {self.max_response_size} bytes")

                # If status is 200, check if it's an interactive challenge or clean sitemap
                if status_code == 200:
                    body_snippet = body[:4096].decode("utf-8", errors="ignore")
                    body_indicators = Diagnostics403.inspect_html_body(body_snippet)
                    if body_indicators.get("challenge_detected"):
                        diag = Diagnostics403.classify(
                            status_code=403,
                            headers=headers,
                            body_snippet=body_snippet,
                            stage="sitemap_browser_download",
                            robots_allowed=True
                        )
                        return body, 403, headers, redirect_chain, diag

                    return body, 200, headers, redirect_chain, None

                # Non-200 from browser
                diag = Diagnostics403.classify(
                    status_code=status_code,
                    headers=headers,
                    body_snippet=body[:2048].decode("utf-8", errors="ignore"),
                    stage="sitemap_browser_download",
                    robots_allowed=True,
                    redirect_chain=redirect_chain
                )
                return body, status_code, headers, redirect_chain, diag

            finally:
                page.remove_listener("response", handle_response)

        except Exception as e:
            err_str = str(e)
            diag = Diagnostics403.classify(
                status_code=0,
                headers={},
                stage="sitemap_browser_download",
                robots_allowed=True,
                redirect_chain=redirect_chain,
                error_type="Timeout" if "timeout" in err_str.lower() else "Playwright Error"
            )
            diag["what_happened"] = f"Browser fetch failed: {err_str}"
            return b"", 0, {}, redirect_chain, diag
        finally:
            if page and self.browser_pool:
                self.browser_pool.release_page(page)

    async def _safe_fetch(self, url: str) -> Tuple[bytes, int, Dict[str, str], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Fetch remote URL following redirects with SSRF check on every hop.
        Returns: (body_bytes, status_code, headers, redirect_chain, diagnostic_record)
        """
        current_url = url
        redirect_chain = []
        max_hops = 10
        hops = 0

        while hops < max_hops:
            hops += 1
            # SSRF validation on every hop
            if not URLNormalizer.is_safe_url(current_url, allow_local=self.allow_local):
                diag = Diagnostics403.classify(
                    status_code=403,
                    headers={},
                    stage="sitemap_download",
                    robots_allowed=True,
                    redirect_chain=redirect_chain,
                    error_type="SSRF_PROHIBITED"
                )
                diag["classification"] = "REDIRECT_REJECTED" if redirect_chain else "SSRF_PROHIBITED"
                diag["what_happened"] = f"Destination URL '{current_url}' targets a private, loopback, or prohibited address."
                return b"", 403, {}, redirect_chain, diag

            try:
                # Stream to enforce max response size limit
                async with self.client.stream("GET", current_url) as resp:
                    status = resp.status_code
                    headers = dict(resp.headers)

                    if status in [301, 302, 303, 307, 308]:
                        loc = resp.headers.get("Location")
                        if not loc:
                            break
                        next_url = urllib.parse.urljoin(current_url, loc)
                        redirect_chain.append({"url": current_url, "status": status, "to": next_url})
                        current_url = next_url
                        continue

                    # Read up to max_response_size
                    body_chunks = []
                    total_bytes = 0
                    async for chunk in resp.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > self.max_response_size:
                            raise ValueError(f"Response size exceeded limit of {self.max_response_size} bytes")
                        body_chunks.append(chunk)

                    body = b"".join(body_chunks)
                    
                    if status == 403:
                        diag = Diagnostics403.classify(
                            status_code=403,
                            headers=headers,
                            body_snippet=body[:2048].decode("utf-8", errors="ignore"),
                            stage="sitemap_download",
                            robots_allowed=True,
                            redirect_chain=redirect_chain
                        )
                        # Playwright browser fallback when blocked by edge WAF / bot policy
                        if self.use_browser_fallback and diag.get("classification") in ["CDN_WAF_BLOCKED_OR_CHALLENGED", "HTTP_403_UNCLASSIFIED"]:
                            b_body, b_status, b_headers, b_chain, b_diag = await self._fetch_with_browser(current_url)
                            if b_status == 200 and b_body:
                                return b_body, 200, b_headers, redirect_chain + b_chain, None
                            elif b_diag and b_status == 403:
                                return b_body, b_status, b_headers, redirect_chain + b_chain, b_diag

                        return body, status, headers, redirect_chain, diag
                    elif status >= 400:
                        diag = Diagnostics403.classify(
                            status_code=status,
                            headers=headers,
                            body_snippet=body[:2048].decode("utf-8", errors="ignore"),
                            stage="sitemap_download",
                            robots_allowed=True,
                            redirect_chain=redirect_chain
                        )
                        if self.use_browser_fallback and status in [503] and diag.get("classification") in ["CDN_WAF_BLOCKED_OR_CHALLENGED", "HTTP_403_UNCLASSIFIED"]:
                            b_body, b_status, b_headers, b_chain, b_diag = await self._fetch_with_browser(current_url)
                            if b_status == 200 and b_body:
                                return b_body, 200, b_headers, redirect_chain + b_chain, None
                            elif b_diag:
                                return b_body, b_status, b_headers, redirect_chain + b_chain, b_diag

                        return body, status, headers, redirect_chain, diag

                    return body, status, headers, redirect_chain, None

            except Exception as e:
                err_str = str(e)
                diag = Diagnostics403.classify(
                    status_code=0,
                    headers={},
                    stage="sitemap_download",
                    robots_allowed=True,
                    redirect_chain=redirect_chain,
                    error_type="Timeout" if "timeout" in err_str.lower() else "Network Error"
                )
                diag["what_happened"] = f"Failed to download sitemap: {err_str}"
                return b"", 0, {}, redirect_chain, diag

        return b"", 0, {}, redirect_chain, {
            "classification": "REDIRECT_REJECTED",
            "what_happened": "Too many redirects encountered (exceeded 10 hops).",
            "where_it_happened": "During sitemap download redirect resolution.",
            "what_verified": "Redirect loop or chain exceeded maximum allowed hops.",
            "recommended_action": "Verify sitemap URL redirect configuration.",
            "retry_appropriate": False,
            "safe_headers": {},
            "body_indicators": {}
        }

    def parse_sitemap_xml(self, xml_bytes: bytes, sitemap_url_or_name: str = "") -> Dict[str, Any]:
        """
        Parse XML content bytes into child sitemaps and page URLs.
        Returns:
        {
            "is_index": bool,
            "child_sitemaps": List[Dict[str, str]], # [{"loc": ..., "lastmod": ...}]
            "urls": List[Dict[str, Any]],           # [{"loc": ..., "lastmod": ..., "changefreq": ..., "priority": ...}]
        }
        """
        decompressed = self._decompress_if_gzip(xml_bytes, sitemap_url_or_name, max_size=self.max_response_size)
        try:
            root = self._safe_parse_xml(decompressed)
        except Exception:
            # Recover <loc> values from malformed / truncated XML rather than dropping the whole sitemap
            text_snippet = decompressed.decode("utf-8", errors="ignore")
            locs = re.findall(r"<loc>\s*([^<>\s]+)\s*</loc>", text_snippet, flags=re.IGNORECASE)
            if not locs:
                raise
            join = (lambda l: urllib.parse.urljoin(sitemap_url_or_name, l)) if sitemap_url_or_name else (lambda l: l)
            if "<sitemapindex" in text_snippet.lower() or "<sitemap>" in text_snippet.lower():
                return {"is_index": True, "child_sitemaps": [{"loc": join(l), "lastmod": ""} for l in locs], "urls": [],
                        "recovered_from_malformed_xml": True}
            return {"is_index": False, "child_sitemaps": [], "recovered_from_malformed_xml": True,
                    "urls": [{"loc": join(l), "lastmod": "", "changefreq": "", "priority": 0.5} for l in locs]}
        
        root_tag = _strip_tag_ns(root.tag).lower()
        
        child_sitemaps = []
        urls = []
        
        if root_tag == "sitemapindex":
            for child in root:
                if _strip_tag_ns(child.tag).lower() == "sitemap":
                    loc = ""
                    lastmod = ""
                    for sub in child:
                        sub_tag = _strip_tag_ns(sub.tag).lower()
                        if sub_tag == "loc" and sub.text:
                            loc = sub.text.strip()
                        elif sub_tag == "lastmod" and sub.text:
                            lastmod = sub.text.strip()
                    if loc:
                        if sitemap_url_or_name and not loc.startswith(("http://", "https://")):
                            loc = urllib.parse.urljoin(sitemap_url_or_name, loc)
                        child_sitemaps.append({"loc": loc, "lastmod": lastmod})
            return {"is_index": True, "child_sitemaps": child_sitemaps, "urls": []}
            
        elif root_tag == "urlset":
            for child in root:
                if _strip_tag_ns(child.tag).lower() == "url":
                    loc = ""
                    lastmod = ""
                    changefreq = ""
                    priority = 0.5
                    for sub in child:
                        sub_tag = _strip_tag_ns(sub.tag).lower()
                        if sub_tag == "loc" and sub.text:
                            loc = sub.text.strip()
                        elif sub_tag == "lastmod" and sub.text:
                            lastmod = sub.text.strip()
                        elif sub_tag == "changefreq" and sub.text:
                            changefreq = sub.text.strip()
                        elif sub_tag == "priority" and sub.text:
                            try:
                                priority = float(sub.text.strip())
                            except ValueError:
                                priority = 0.5
                    if loc:
                        if sitemap_url_or_name and not loc.startswith(("http://", "https://")):
                            loc = urllib.parse.urljoin(sitemap_url_or_name, loc)
                        urls.append({
                            "loc": loc,
                            "lastmod": lastmod,
                            "changefreq": changefreq,
                            "priority": priority
                        })
            return {"is_index": False, "child_sitemaps": [], "urls": urls}
            
        else:
            raise ValueError(f"Unrecognized XML sitemap root element '<{root.tag}>'. Expected <urlset> or <sitemapindex>.")

    async def extract_all(
        self,
        entry_sitemap_urls: List[str],
        progress_callback: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Recursively extract all URLs from entry sitemap URLs up to max_depth.
        Detects cycles, enforces limits, checks robots.txt, normalizes and deduplicates URLs.
        """
        visited_sitemaps: Set[str] = set()
        queue: List[Tuple[str, str, int]] = [] # (sitemap_url, parent_url, depth)
        
        for u in entry_sitemap_urls:
            cleaned = u.strip()
            if cleaned:
                queue.append((cleaned, "", 0))
                
        extracted_urls_map: Dict[str, Dict[str, Any]] = {} # normalized_url -> details
        duplicate_count = 0
        rejected_count = 0
        sitemap_sources: List[Dict[str, Any]] = []
        diagnostics: List[Dict[str, Any]] = []
        errors: List[str] = []

        while queue:
            # Check maximum URL limit
            if len(extracted_urls_map) >= self.max_urls:
                errors.append(f"Reached configured limit of {self.max_urls} extracted URLs. Extraction truncated.")
                break

            current_sitemap, parent_sitemap, depth = queue.pop(0)
            normalized_sitemap = URLNormalizer.normalize(current_sitemap)
            
            if normalized_sitemap in visited_sitemaps:
                continue
            visited_sitemaps.add(normalized_sitemap)

            if depth > self.max_depth:
                errors.append(f"Exceeded maximum sitemap nesting depth of {self.max_depth} for '{current_sitemap}'.")
                continue

            # Fetch sitemap
            t_fetch = time.perf_counter()
            async with self.semaphore:
                body, status_code, headers, redirects, diag = await self._safe_fetch(current_sitemap)
            fetch_meta = {"content_type": str((headers or {}).get("content-type") or (headers or {}).get("Content-Type") or "")[:120],
                          "response_time_ms": round((time.perf_counter() - t_fetch) * 1000.0, 1),
                          "size_bytes": len(body or b""), "redirects": len(redirects or [])}

            if diag:
                diagnostics.append({
                    "url": current_sitemap,
                    "diagnostic": diag,
                    "stage": "sitemap_download"
                })

            if status_code != 200 or not body:
                error_msg = diag.get("what_happened", f"HTTP {status_code}") if diag else f"HTTP {status_code}"
                
                # Granular failure classification
                if diag and diag.get("classification") in ["CDN_WAF_BLOCKED_OR_CHALLENGED", "ROBOTS_BLOCKED"]:
                    classification = "BLOCKED_BY_TARGET_OR_WAF"
                elif status_code == 403:
                    classification = "BLOCKED_BY_TARGET_OR_WAF" if (diag and diag.get("classification") == "CDN_WAF_BLOCKED_OR_CHALLENGED") else "SITEMAP_HTTP_403"
                elif status_code == 404:
                    classification = "SITEMAP_HTTP_404"
                elif status_code == 429:
                    classification = "SITEMAP_HTTP_429"
                elif status_code >= 500:
                    classification = "SITEMAP_HTTP_5XX"
                elif not body and status_code == 200:
                    classification = "SITEMAP_EMPTY"
                else:
                    err_lower = error_msg.lower()
                    if "timeout" in err_lower:
                        classification = "SITEMAP_TIMEOUT"
                    elif "dns" in err_lower or "name or service not known" in err_lower:
                        classification = "SITEMAP_DNS_ERROR"
                    elif "ssl" in err_lower or "cert" in err_lower:
                        classification = "SITEMAP_SSL_ERROR"
                    else:
                        classification = "SITEMAP_CONNECTION_ERROR"

                sitemap_sources.append({
                    "sitemap_url": current_sitemap,
                    "parent_sitemap_url": parent_sitemap,
                    "depth": depth,
                    "status": "FAILED",
                    "http_status": status_code,
                    "urls_found": 0,
                    "error_message": error_msg,
                    "classification": classification,
                    **fetch_meta,
                })
                errors.append(f"Failed to fetch sitemap '{current_sitemap}': {error_msg}")
                if progress_callback:
                    await progress_callback(len(sitemap_sources), len(extracted_urls_map), duplicate_count, rejected_count, len(errors))
                continue

            # Parse XML
            try:
                parsed = self.parse_sitemap_xml(body, current_sitemap)
            except Exception as e:
                err_msg = str(e)
                diag_xml = Diagnostics403.classify(
                    status_code=status_code,
                    headers=headers,
                    stage="sitemap_download",
                    robots_allowed=True,
                    error_type="SITEMAP_XML_INVALID"
                )
                diag_xml["what_happened"] = f"Failed to parse XML: {err_msg}"
                diagnostics.append({
                    "url": current_sitemap,
                    "diagnostic": diag_xml,
                    "stage": "sitemap_parse"
                })
                sitemap_sources.append({
                    "sitemap_url": current_sitemap,
                    "parent_sitemap_url": parent_sitemap,
                    "depth": depth,
                    "status": "FAILED",
                    "http_status": status_code,
                    "urls_found": 0,
                    "error_message": err_msg,
                    "classification": "SITEMAP_XML_INVALID",
                    **fetch_meta,
                })
                errors.append(f"Invalid XML in sitemap '{current_sitemap}': {err_msg}")
                if progress_callback:
                    await progress_callback(len(sitemap_sources), len(extracted_urls_map), duplicate_count, rejected_count, len(errors))
                continue

            # Process sitemap index child sitemaps
            if parsed["is_index"]:
                urls_found = len(parsed["child_sitemaps"])
                sitemap_sources.append({
                    "sitemap_url": current_sitemap,
                    "parent_sitemap_url": parent_sitemap,
                    "depth": depth,
                    "status": "INDEX_PARSED",
                    "http_status": status_code,
                    "urls_found": urls_found,
                    "error_message": "",
                    "classification": "VALID",
                    "kind": "INDEX", "duplicates": 0, "rejected": 0, "child_sitemap_count": urls_found,
                    **fetch_meta,
                })
                for child in parsed["child_sitemaps"]:
                    c_loc = child["loc"]
                    if URLNormalizer.is_safe_url(c_loc, allow_local=self.allow_local):
                        queue.append((c_loc, current_sitemap, depth + 1))
                    else:
                        rejected_count += 1
                        errors.append(f"Rejected child sitemap URL '{c_loc}' failing SSRF or protocol checks.")
            else:
                # Process URLs in urlset
                urls_found = len(parsed["urls"])
                src_rec = {
                    "sitemap_url": current_sitemap,
                    "parent_sitemap_url": parent_sitemap,
                    "depth": depth,
                    "status": "PARSED",
                    "http_status": status_code,
                    "urls_found": urls_found,
                    "error_message": "",
                    "classification": "VALID",
                    # additive per-sitemap hygiene stats (used by the on-page sitemap audit)
                    "kind": "URLSET", "duplicates": 0, "rejected": 0, "child_sitemap_count": 0,
                    **fetch_meta,
                }
                sitemap_sources.append(src_rec)

                for item in parsed["urls"]:
                    raw_url = item["loc"]
                    # 1. Validate Scheme
                    parsed_item = urllib.parse.urlparse(raw_url)
                    if parsed_item.scheme.lower() not in ["http", "https"]:
                        rejected_count += 1
                        src_rec["rejected"] += 1
                        continue

                    # 2. SSRF Check
                    if not URLNormalizer.is_safe_url(raw_url, allow_local=self.allow_local):
                        rejected_count += 1
                        src_rec["rejected"] += 1
                        continue

                    # 3. Normalize & Deduplicate
                    norm_url = URLNormalizer.normalize(raw_url)
                    if norm_url in extracted_urls_map:
                        duplicate_count += 1
                        src_rec["duplicates"] += 1
                        continue

                    # 4. Check Robots.txt policy
                    robots_ok = await self.robots_manager.is_allowed(norm_url)
                    status_str = "VALID" if robots_ok else "ROBOTS_DISALLOWED"

                    extracted_urls_map[norm_url] = {
                        "raw_url": raw_url,
                        "normalized_url": norm_url,
                        "source_sitemap_url": current_sitemap,
                        "lastmod": item.get("lastmod", ""),
                        "changefreq": item.get("changefreq", ""),
                        "priority": item.get("priority", 0.5),
                        "robots_allowed": robots_ok,
                        "status": status_str,
                        "selected_for_crawl": robots_ok, # Default select only robots-allowed URLs
                        "rejection_reason": "" if robots_ok else "Blocked by robots.txt policy"
                    }

                    if len(extracted_urls_map) >= self.max_urls:
                        break

            if progress_callback:
                await progress_callback(len(sitemap_sources), len(extracted_urls_map), duplicate_count, rejected_count, len(errors))

        success_sources = sum(1 for s in sitemap_sources if s.get("status") in ["PARSED", "INDEX_PARSED"])
        blocked_sources = sum(1 for s in sitemap_sources if s.get("classification") in ["BLOCKED_BY_TARGET_OR_WAF", "SITEMAP_HTTP_403"])
        not_found_sources = sum(1 for s in sitemap_sources if s.get("classification") == "SITEMAP_HTTP_404")
        rate_limited_sources = sum(1 for s in sitemap_sources if s.get("classification") == "SITEMAP_HTTP_429")
        server_error_sources = sum(1 for s in sitemap_sources if s.get("classification") == "SITEMAP_HTTP_5XX")
        timeout_sources = sum(1 for s in sitemap_sources if s.get("classification") == "SITEMAP_TIMEOUT")
        invalid_xml_sources = sum(1 for s in sitemap_sources if s.get("classification") == "SITEMAP_INVALID_XML")

        extraction_completeness = (
            "COMPLETE" if (len(errors) == 0 and len(sitemap_sources) > 0)
            else ("PARTIAL" if len(extracted_urls_map) > 0
            else ("BLOCKED_BY_TARGET_OR_WAF" if blocked_sources > 0
            else "FAILED"))
        )

        return {
            "total_sitemaps_discovered": len(sitemap_sources),
            "total_urls_extracted": len(extracted_urls_map),
            "unique_urls": list(extracted_urls_map.values()),
            "duplicate_count": duplicate_count,
            "rejected_count": rejected_count,
            "sitemap_sources": sitemap_sources,
            "diagnostics": diagnostics,
            "errors": errors,
            "complete": len(errors) == 0,
            "extraction_completeness": extraction_completeness,
            "hierarchy_summary": {
                "total_sitemaps": len(sitemap_sources),
                "successful": success_sources,
                "blocked_by_waf_or_403": blocked_sources,
                "not_found_404": not_found_sources,
                "rate_limited_429": rate_limited_sources,
                "server_error_5xx": server_error_sources,
                "timeouts": timeout_sources,
                "invalid_xml": invalid_xml_sources
            }
        }

    async def extract_from_uploaded_bytes(
        self,
        file_bytes: bytes,
        filename: str = "uploaded_sitemap.xml"
    ) -> Dict[str, Any]:
        """
        Extract URLs from uploaded XML or XML.GZ file directly.
        Bypasses remote network fetching for the initial file, allowing users to proceed
        even when remote sitemap download is blocked by edge WAF/firewall.
        """
        try:
            parsed = self.parse_sitemap_xml(file_bytes, filename)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "total_urls_extracted": 0,
                "unique_urls": [],
                "duplicate_count": 0,
                "rejected_count": 0,
                "sitemap_sources": [{
                    "sitemap_url": f"upload://{filename}",
                    "parent_sitemap_url": "",
                    "depth": 0,
                    "status": "FAILED",
                    "http_status": 200,
                    "urls_found": 0,
                    "error_message": str(e),
                    "classification": "SITEMAP_XML_INVALID"
                }],
                "diagnostics": [{
                    "url": f"upload://{filename}",
                    "diagnostic": {
                        "classification": "SITEMAP_XML_INVALID",
                        "what_happened": f"Uploaded file is not valid XML: {e}",
                        "where_it_happened": "During local file parsing.",
                        "what_verified": "XML syntax error.",
                        "recommended_action": "Check XML formatting or ensure gzip file is uncorrupted.",
                        "retry_appropriate": False,
                        "safe_headers": {},
                        "body_indicators": {}
                    }
                }],
                "errors": [str(e)]
            }

        extracted_urls_map = {}
        duplicate_count = 0
        rejected_count = 0

        if parsed["is_index"]:
            # If the uploaded file is an index, fetch child sitemaps using extract_all
            child_locs = [c["loc"] for c in parsed["child_sitemaps"]]
            res = await self.extract_all(child_locs)
            res["sitemap_sources"].insert(0, {
                "sitemap_url": f"upload://{filename}",
                "parent_sitemap_url": "",
                "depth": 0,
                "status": "INDEX_PARSED",
                "http_status": 200,
                "urls_found": len(child_locs),
                "error_message": "",
                "classification": "VALID"
            })
            return res
        else:
            for item in parsed["urls"]:
                raw_url = item["loc"]
                parsed_item = urllib.parse.urlparse(raw_url)
                if parsed_item.scheme.lower() not in ["http", "https"]:
                    rejected_count += 1
                    continue
                if not URLNormalizer.is_safe_url(raw_url, allow_local=self.allow_local):
                    rejected_count += 1
                    continue

                norm_url = URLNormalizer.normalize(raw_url)
                if norm_url in extracted_urls_map:
                    duplicate_count += 1
                    continue

                robots_ok = await self.robots_manager.is_allowed(norm_url)
                status_str = "VALID" if robots_ok else "ROBOTS_DISALLOWED"

                extracted_urls_map[norm_url] = {
                    "raw_url": raw_url,
                    "normalized_url": norm_url,
                    "source_sitemap_url": f"upload://{filename}",
                    "lastmod": item.get("lastmod", ""),
                    "changefreq": item.get("changefreq", ""),
                    "priority": item.get("priority", 0.5),
                    "robots_allowed": robots_ok,
                    "status": status_str,
                    "selected_for_crawl": robots_ok,
                    "rejection_reason": "" if robots_ok else "Blocked by robots.txt policy"
                }

            return {
                "success": True,
                "total_sitemaps_discovered": 1,
                "total_urls_extracted": len(extracted_urls_map),
                "unique_urls": list(extracted_urls_map.values()),
                "duplicate_count": duplicate_count,
                "rejected_count": rejected_count,
                "sitemap_sources": [{
                    "sitemap_url": f"upload://{filename}",
                    "parent_sitemap_url": "",
                    "depth": 0,
                    "status": "PARSED",
                    "http_status": 200,
                    "urls_found": len(parsed["urls"]),
                    "error_message": "",
                    "classification": "VALID"
                }],
                "diagnostics": [],
                "errors": [],
                "complete": True
            }
