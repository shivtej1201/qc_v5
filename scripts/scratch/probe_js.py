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
    
    elements = await page.evaluate(f'''(selectors) => {{
        let container = document.body;
        for (const sel of selectors) {{
            const el = document.querySelector(sel);
            if (el && el.innerText.trim().length > 50) {{
                container = el;
                break;
            }}
        }}
        
        let anchors = Array.from(container.querySelectorAll('a'));
        
        return anchors.map(a => ({{
            href: a.getAttribute('href') || '',
            text: a.innerText || ''
        }}));
    }}''', settings.CONTENT_SELECTORS)
    
    print(f"Found {len(elements)} anchors.")
    bp.release_page(page)
    await bp.stop()

asyncio.run(main())
