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
    
    info = await page.evaluate(f'''(selectors) => {{
        let container = document.body;
        let matched_sel = "body_default";
        for (const sel of selectors) {{
            const el = document.querySelector(sel);
            if (el && el.innerText.trim().length > 50) {{
                container = el;
                matched_sel = sel;
                break;
            }}
        }}
        
        let anchors = Array.from(container.querySelectorAll('a'));
        return {{
            sel: matched_sel,
            anchors: anchors.length,
            html: container.outerHTML.substring(0, 200)
        }};
    }}''', settings.CONTENT_SELECTORS)
    
    print(info)
    bp.release_page(page)
    await bp.stop()

asyncio.run(main())
