import asyncio
from playwright.async_api import async_playwright
from config import settings

class BrowserPool:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages = []
        self.semaphore = asyncio.Semaphore(settings.BROWSER_CONCURRENCY)

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(ignore_https_errors=True, service_workers="block")
        for _ in range(settings.BROWSER_CONCURRENCY):
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
        await self.semaphore.acquire()
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
        return None

    def release_page(self, page):
        if page:
            page.is_busy = False
            self.semaphore.release()
