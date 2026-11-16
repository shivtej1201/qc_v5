import re
html = '''<div data-pageType="B2B-clp">...</div>
<span data-PAGETYPE = 'B2C-pdp' >...
<div data-pagetype=B2B-plp >...
'''
pattern = re.compile(r'(?i)data-pagetype\s*=\s*(?:(["\'])(.*?)\1|([^\s>]+))')
for match in pattern.finditer(html):
    val = match.group(2) if match.group(2) is not None else match.group(3)
    print(val)
