import asyncio
from browser import BrowserPool
from config import settings

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
