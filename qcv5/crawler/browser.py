import asyncio
from playwright.async_api import async_playwright
from qcv5.core.config import settings

BROWSER_MIN_CAPACITY, BROWSER_MAX_CAPACITY = 1, 4
LEASE_TIMEOUT_SEC = 30.0


class BrowserPool:
    """Small Playwright page pool. HTTP-first crawling only borrows a page when rendering is required.
    Capacity is clamped to 1-4 (default 2 via BROWSER_CONCURRENCY); a lease waits at most LEASE_TIMEOUT_SEC."""

    def __init__(self, capacity: int = None, lease_timeout: float = LEASE_TIMEOUT_SEC):
        cap = settings.BROWSER_CONCURRENCY if capacity is None else capacity
        self.capacity = max(BROWSER_MIN_CAPACITY, min(BROWSER_MAX_CAPACITY, int(cap)))
        self.lease_timeout = lease_timeout
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages = []
        self.semaphore = asyncio.Semaphore(self.capacity)

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ignore_https_errors=True,
            service_workers="block"
        )
        for _ in range(self.capacity):
            page = await self.context.new_page()
            self.pages.append(page)

    async def stop(self):
        for page in self.pages:
            try:
                await page.close()
            except:
                pass
        if self.context: await self.context.close()
        if self.browser: await self.browser.close()
        if self.playwright: await self.playwright.stop()

    async def get_page(self):
        """Lease a page or return None (timeout / no usable page). The slot is ALWAYS returned on failure."""
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=self.lease_timeout)
        except asyncio.TimeoutError:
            return None
        for idx, page in enumerate(self.pages):
            if not getattr(page, "is_busy", False):
                if page.is_closed():
                    try:
                        page = await self.context.new_page()
                        self.pages[idx] = page
                    except Exception:
                        continue
                page.is_busy = True
                return page
        self.semaphore.release()   # no usable page: give the slot back (previously leaked)
        return None

    def release_page(self, page):
        if page:
            page.is_busy = False
            self.semaphore.release()
