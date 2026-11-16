import asyncio
from classifier import Classifier

async def test_partial():
    c = Classifier()
    html_partial = '<html><body><div data-pagetype="B2B-only"></div></body></html>' # Will be split by - as B2B and ONLY
    # Wait, the fallback is delimiter split. If it's "B2B", split by - is len=1!
    html_single = '<html><body><div data-pagetype="B2B"></div></body></html>'
    
    pt, biz, method = c._classify_by_data_pagetype(html_single)
    # The delimiter split expects exactly 2 parts. If 1 part, it returns (None, None, "HTML_DATA_PAGETYPE (Unknown: b2b)")
    assert pt is None and biz is None
    
    # Let's map it explicitly to test partial return
    import config
    config.settings.PAGETYPE_MAPPING["b2b-only"] = {"business": "B2B"}
    
    html_partial = '<html><body><div data-pagetype="b2b-only"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_partial)
    assert biz == "B2B" and pt is None and method == "HTML_DATA_PAGETYPE"
    print("Partial tests passed.")

if __name__ == "__main__":
    asyncio.run(test_partial())
