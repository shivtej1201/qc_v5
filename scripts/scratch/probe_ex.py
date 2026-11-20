import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/scratch)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import asyncio
from qcv5.crawler.browser import BrowserPool
from qcv5.crawler.extractor import LinkExtractor

async def main():
    bp = BrowserPool()
    await bp.start()
    ex = LinkExtractor(bp)
    
    # Grab page manually and screenshot it
    page = await bp.get_page()
    await page.goto('https://www.bajajfinserv.in/business-loan')
    await asyncio.sleep(2)
    await page.screenshot(path='test.png')
    bp.release_page(page)
    
    items = await ex.extract_links('https://www.bajajfinserv.in/business-loan')
    print(f"Extracted {len(items)} links.")
    for i in items[:5]:
        print(i)
    await bp.stop()

asyncio.run(main())
