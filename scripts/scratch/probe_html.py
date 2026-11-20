import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/scratch)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import asyncio
from qcv5.crawler.browser import BrowserPool
from qcv5.core.config import settings

async def main():
    bp = BrowserPool()
    await bp.start()
    
    page = await bp.get_page()
    await page.goto('https://www.bajajfinserv.in/business-loan')
    await asyncio.sleep(2)
    html = await page.content()
    print(f"HTML length: {len(html)}")
    print(f"Number of 'a' tags in raw HTML: {html.count('<a')}")
    bp.release_page(page)
    await bp.stop()

asyncio.run(main())
