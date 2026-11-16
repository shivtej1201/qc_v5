import asyncio
from classifier import Classifier

async def test_classifier():
    c = Classifier()
    
    # 1. Test data-pageType extraction (valid)
    html_valid = '<html><body><div data-pagetype="B2B-clp"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_valid)
    assert pt == "CLP" and biz == "B2B" and method == "HTML_DATA_PAGETYPE"
    
    # 2. Test mapped vs delimiter fallback
    html_b2c = '<html><body><div data-pagetype="B2C-pdp"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_b2c)
    assert pt == "PDP" and biz == "B2C" and method == "HTML_DATA_PAGETYPE"
    
    html_unmapped = '<html><body><div data-pagetype="b2b-faq"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_unmapped)
    assert pt == "FAQ" and biz == "B2B" and method == "HTML_DATA_PAGETYPE"

    # 3. Uppercase, lowercase, and mixed-case attribute names
    html_mixed = '<html><body><div DATA-PAGETYPE="B2B-clp"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_mixed)
    assert pt == "CLP" and biz == "B2B"
    
    # 4. Surrounding whitespace
    html_ws = '<html><body><div data-pagetype=" B2B-clp  "></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_ws)
    assert pt == "CLP" and biz == "B2B"
    
    # 5. Missing / empty
    html_empty = '<html><body><div data-pagetype=""></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_empty)
    assert pt is None
    
    # 6. Malformed/unknown
    html_malformed = '<html><body><div data-pagetype="random_string"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_malformed)
    assert pt is None and method == "HTML_DATA_PAGETYPE (Unknown: random_string)"
    
    # 7. Multiple identical values
    html_multi_same = '<html><body><div data-pagetype="B2B-clp"></div><div data-pagetype="b2b-clp"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_multi_same)
    assert pt == "CLP" and biz == "B2B"
    
    # 8. Conflicting values
    html_conflict = '<html><body><div data-pagetype="B2B-clp"></div><div data-pagetype="B2C-pdp"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_conflict)
    assert pt is None and "Ambiguous" in method
    
    # 9. Header exclusion
    html_header = '<html><header><div data-pagetype="B2C-header"></div></header><body><div data-pagetype="B2B-clp"></div></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_header)
    assert pt == "CLP" and biz == "B2B"
    
    # 10. Header fallback (no content pagetype)
    html_header_only = '<html><header><div data-pagetype="B2B-clp"></div></header><body></body></html>'
    pt, biz, method = c._classify_by_data_pagetype(html_header_only)
    assert pt == "CLP" and biz == "B2B"
    
    print("All unit tests passed.")

if __name__ == "__main__":
    asyncio.run(test_classifier())
