import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/scratch)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import re
html = '''<div data-pageType="B2B-clp">...</div>
<span data-PAGETYPE = 'B2C-pdp' >...
<div data-pagetype=B2B-plp >...
'''
pattern = re.compile(r'(?i)data-pagetype\s*=\s*(?:(["\'])(.*?)\1|([^\s>]+))')
for match in pattern.finditer(html):
    val = match.group(2) if match.group(2) is not None else match.group(3)
    print(val)
