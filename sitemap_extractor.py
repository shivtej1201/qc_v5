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

from config import settings
from url_normalizer import URLNormalizer
from diagnostics_403 import Diagnostics403

def _strip_tag_ns(tag: str) -> str:
    """Strip XML namespace prefix from tag name."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag

class RobotsManager:
    """Caches and evaluates robots.txt policies per domain with SSRF validation."""
    def __init__(self, http_client: httpx.AsyncClient, user_agent: str = "Mozilla/5.0"):
        self.http_client = http_client
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
            if not URLNormalizer.is_safe_url(robots_url, allow_local=getattr(settings, "ALLOW_LOCAL_TESTING", False)):
                self.parsers[origin] = None
                return None

            parser = urllib.robotparser.RobotFileParser()
            try:
                resp = await self.http_client.get(robots_url, timeout=5.0)
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
        max_response_size: int = 10 * 1024 * 1024, # 10MB
        max_urls: int = 50000,
        timeout: float = 15.0,
        concurrency: int = 5,
        allow_local: bool = False
    ):
        self.max_depth = max_depth
        self.max_response_size = max_response_size
        self.max_urls = max_urls
        self.timeout = timeout
        self.concurrency = concurrency
        self.allow_local = allow_local
        
        self.client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(connect=5.0, read=self.timeout, write=5.0, pool=5.0),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LinkAuditorBot/2.0"},
            follow_redirects=False # Manual redirect tracking for SSRF check
        )
        self.robots_manager = RobotsManager(self.client, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) LinkAuditorBot/2.0")
        self.semaphore = asyncio.Semaphore(self.concurrency)

    async def close(self):
        await self.client.aclose()

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
        root = self._safe_parse_xml(decompressed)
        
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
            async with self.semaphore:
                body, status_code, headers, redirects, diag = await self._safe_fetch(current_sitemap)

            if diag:
                diagnostics.append({
                    "url": current_sitemap,
                    "diagnostic": diag,
                    "stage": "sitemap_download"
                })

            if status_code != 200 or not body:
                error_msg = diag.get("what_happened", f"HTTP {status_code}") if diag else f"HTTP {status_code}"
                sitemap_sources.append({
                    "sitemap_url": current_sitemap,
                    "parent_sitemap_url": parent_sitemap,
                    "depth": depth,
                    "status": "FAILED",
                    "http_status": status_code,
                    "urls_found": 0,
                    "error_message": error_msg,
                    "classification": diag.get("classification", "SITEMAP_FETCH_FAILED") if diag else "SITEMAP_FETCH_FAILED"
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
                    "classification": "SITEMAP_XML_INVALID"
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
                    "classification": "VALID"
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
                sitemap_sources.append({
                    "sitemap_url": current_sitemap,
                    "parent_sitemap_url": parent_sitemap,
                    "depth": depth,
                    "status": "PARSED",
                    "http_status": status_code,
                    "urls_found": urls_found,
                    "error_message": "",
                    "classification": "VALID"
                })

                for item in parsed["urls"]:
                    raw_url = item["loc"]
                    # 1. Validate Scheme
                    parsed_item = urllib.parse.urlparse(raw_url)
                    if parsed_item.scheme.lower() not in ["http", "https"]:
                        rejected_count += 1
                        continue

                    # 2. SSRF Check
                    if not URLNormalizer.is_safe_url(raw_url, allow_local=self.allow_local):
                        rejected_count += 1
                        continue

                    # 3. Normalize & Deduplicate
                    norm_url = URLNormalizer.normalize(raw_url)
                    if norm_url in extracted_urls_map:
                        duplicate_count += 1
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

        return {
            "total_sitemaps_discovered": len(sitemap_sources),
            "total_urls_extracted": len(extracted_urls_map),
            "unique_urls": list(extracted_urls_map.values()),
            "duplicate_count": duplicate_count,
            "rejected_count": rejected_count,
            "sitemap_sources": sitemap_sources,
            "diagnostics": diagnostics,
            "errors": errors,
            "complete": len(errors) == 0
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
