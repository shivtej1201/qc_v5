import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/scratch)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
from qcv5.crawler.html_parser import Parser
html = '<html><body><main><a href="http://example.com/test">link</a></main></body></html>'
p = Parser('http://example.com/')
p.feed(html)
print('All links:', p.links)
print('Filtered:', p.get_filtered_links())
