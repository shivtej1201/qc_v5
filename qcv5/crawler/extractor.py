import asyncio
import httpx
import urllib.parse
from qcv5.crawler.browser import BrowserPool
from qcv5.crawler.url_normalizer import URLNormalizer
from typing import List, Dict, Any, Tuple
from qcv5.core.config import settings
from qcv5.crawler.html_parser import Parser
import os

class LinkExtractor:
    def __init__(self, browser_pool: BrowserPool):
        self.browser_pool = browser_pool
        # Ensure we have a persistent async client
        # In a real app we'd close this cleanly, but here it's tied to daemon lifecycle
        self.client = httpx.AsyncClient(verify=False, timeout=settings.READ_TIMEOUT)

    async def _route_interceptor(self, route):
        # Abort requests for images, media, and fonts to save bandwidth/speed
        if route.request.resource_type in ["image", "media", "font"]:
            await route.abort()
        else:
            await route.continue_()

    async def _fetch_http(self, url: str):
        try:
            resp = await self.client.get(url, follow_redirects=True)
            return resp
        except Exception as e:
            print(f"HTTP fetch failed for {url}: {e}")
            return None

    def _parse_links(self, html: str, final_url: str, source_url: str) -> List[Dict[str, Any]]:
        parser = Parser(final_url)
        parser.feed(html)
        
        filtered = parser.get_filtered_links()
        links_data = []
        for el in filtered:
            href = parser.resolve_url(el['href'])
            if href and URLNormalizer.is_safe_url(href):
                norm = URLNormalizer.normalize(href)
                links_data.append({
                    "source_url": source_url,
                    "original_href": href,
                    "normalized_url": norm,
                    "anchor_text": el['text'].strip(),
                    "component_name": el['component']
                })
        return links_data
        
    def _parse_images(self, html: str, final_url: str, source_url: str) -> List[Dict[str, Any]]:
        parser = Parser(final_url)
        parser.feed(html)
        
        filtered = parser.get_filtered_images()
        images_data = []
        for el in filtered:
            url = parser.resolve_url(el['url'])
            if url and URLNormalizer.is_safe_url(url):
                norm = URLNormalizer.normalize(url)
                image_name = el['alt'].strip()
                if not image_name:
                    image_name = el['title'].strip()
                if not image_name:
                    parsed = urllib.parse.urlparse(norm)
                    image_name = os.path.basename(parsed.path)
                    
                images_data.append({
                    "source_url": source_url,
                    "original_url": el['url'],
                    "normalized_url": norm,
                    "image_name": image_name,
                    "loading_type": el['loading_type']
                })
        return images_data

    async def extract_links(self, source_url: str) -> Tuple[List[Dict[str, Any]], str, str]:
        resp = await self._fetch_http(source_url)
        use_browser = False
        err_msg = ""
        html = ""
        final_url = source_url
        
        if resp is None:
            use_browser = settings.BROWSER_FALLBACK
            err_msg = "HTTP Fetch Failed"
        elif resp.status_code in (403, 429, 503):
            use_browser = settings.BROWSER_FALLBACK
            err_msg = f"HTTP {resp.status_code} Blocked"
        else:
            html = resp.text
            final_url = str(resp.url)
            links_data = self._parse_links(html, final_url, source_url)
            
            if len(links_data) == 0:
                if len(html) > 500:
                    use_browser = settings.BROWSER_FALLBACK
                    err_msg = "0 Links Found (Possible SPA)"
                else:
                    return ([{"source_url": source_url, "original_href": "", "normalized_url": "", "anchor_text": "0 Links Found (Empty HTML)", "component_name": "System"}], html, final_url)
            else:
                return (links_data, html, final_url)

        if not use_browser:
            return ([{"source_url": source_url, "original_href": "", "normalized_url": "", "anchor_text": f"ERROR: {err_msg}", "component_name": "System"}], html, final_url)
            
        return await self._extract_links_browser(source_url)
        
    async def extract_images(self, source_url: str) -> List[Dict[str, Any]]:
        resp = await self._fetch_http(source_url)
        use_browser = False
        err_msg = ""
        
        if resp is None:
            use_browser = settings.BROWSER_FALLBACK
            err_msg = "HTTP Fetch Failed"
        elif resp.status_code in (403, 429, 503):
            use_browser = settings.BROWSER_FALLBACK
            err_msg = f"HTTP {resp.status_code} Blocked"
        else:
            html = resp.text
            images_data = self._parse_images(html, str(resp.url), source_url)
            
            if len(images_data) == 0:
                if len(html) > 500:
                    use_browser = settings.BROWSER_FALLBACK
                    err_msg = "0 Images Found (Possible SPA)"
                else:
                    return [{"source_url": source_url, "original_url": "", "normalized_url": "", "image_name": "0 Images Found (Empty HTML)", "loading_type": "System"}]
            else:
                return images_data
                
        if not use_browser:
            return [{"source_url": source_url, "original_url": "", "normalized_url": "", "image_name": f"ERROR: {err_msg}", "loading_type": "System"}]
            
        return await self._extract_images_browser(source_url)

    async def _extract_links_browser(self, source_url: str) -> Tuple[List[Dict[str, Any]], str, str]:
        page = await self.browser_pool.get_page()
        html = ""
        final_url = source_url
        if not page: return ([{"source_url": source_url, "original_href": "", "normalized_url": "", "anchor_text": "ERROR: Browser Launch Failed", "component_name": "System"}], html, final_url)
        links_data = []
        try:
            await page.route("**/*", self._route_interceptor)
            response = await page.goto(source_url, wait_until="domcontentloaded", timeout=settings.NAVIGATION_TIMEOUT)
            if getattr(settings, 'WAIT_FOR_SELECTOR', None):
                try: await page.wait_for_selector(settings.WAIT_FOR_SELECTOR, state="attached", timeout=settings.EXTRACTION_TIMEOUT)
                except Exception: pass
            else:
                await asyncio.sleep(getattr(settings, 'SETTLING_TIME', 0.5))
            
            if response and response.status < 400:
                final_url = response.url
                html = await page.content()
                links_data = self._parse_links(html, final_url, source_url)
        except Exception as e:
            return ([{"source_url": source_url, "original_href": "", "normalized_url": "", "anchor_text": f"ERROR: Browser Exception: {e}", "component_name": "System"}], html, final_url)
        finally:
            try: await page.unroute("**/*", self._route_interceptor)
            except: pass
            self.browser_pool.release_page(page)
            
        if not links_data:
            return ([{"source_url": source_url, "original_href": "", "normalized_url": "", "anchor_text": "0 Links Found (Browser rendering)", "component_name": "System"}], html, final_url)
        return (links_data, html, final_url)
        
    async def _extract_images_browser(self, source_url: str) -> List[Dict[str, Any]]:
        page = await self.browser_pool.get_page()
        if not page: return [{"source_url": source_url, "original_url": "", "normalized_url": "", "image_name": "ERROR: Browser Launch Failed", "loading_type": "System"}]
        images_data = []
        try:
            await page.route("**/*", self._route_interceptor)
            response = await page.goto(source_url, wait_until="domcontentloaded", timeout=settings.NAVIGATION_TIMEOUT)
            if getattr(settings, 'WAIT_FOR_SELECTOR', None):
                try: await page.wait_for_selector(settings.WAIT_FOR_SELECTOR, state="attached", timeout=settings.EXTRACTION_TIMEOUT)
                except Exception: pass
            else:
                await asyncio.sleep(getattr(settings, 'SETTLING_TIME', 0.5))
            
            if response and response.status < 400:
                final_url = response.url
                html = await page.content()
                images_data = self._parse_images(html, final_url, source_url)
        except Exception as e:
            return [{"source_url": source_url, "original_url": "", "normalized_url": "", "image_name": f"ERROR: Browser Exception: {e}", "loading_type": "System"}]
        finally:
            try: await page.unroute("**/*", self._route_interceptor)
            except: pass
            self.browser_pool.release_page(page)
            
        if not images_data:
            return [{"source_url": source_url, "original_url": "", "normalized_url": "", "image_name": "0 Images Found (Browser rendering)", "loading_type": "System"}]
        return images_data
