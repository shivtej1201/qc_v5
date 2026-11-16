import re

html = '''
<html>
<header class="main-header"><div data-pagetype="B2C-header"></div></header>
<main>
  <div data-pagetype=" B2B-clp "></div>
  <div class="modal fade"><span data-pagetype="B2B-modal"></span></div>
</main>
<footer id="footer"><span data-pagetype="footer-only"></span></footer>
</html>
'''

def extract_data_pagetype(html_content: str):
    # Remove headers, footers, navs
    clean_html = re.sub(r'<(header|footer|nav)\b[^>]*>.*?</\1>', '', html_content, flags=re.IGNORECASE | re.DOTALL)
    
    # Remove modals (heuristic: any div with class or id containing 'modal')
    clean_html = re.sub(r'<div\b[^>]*(?:class|id)=["\'][^"\']*modal[^"\']*["\'][^>]*>.*?</div>', '', clean_html, flags=re.IGNORECASE | re.DOTALL)
    
    # Now find all data-pagetype attributes
    pattern = re.compile(r'(?i)data-pagetype\s*=\s*(?:(["\'])(.*?)\1|([^\s>]+))')
    
    found = []
    for match in pattern.finditer(clean_html):
        val = match.group(2) if match.group(2) is not None else match.group(3)
        if val:
            found.append(val.strip())
            
    return found

print(extract_data_pagetype(html))
