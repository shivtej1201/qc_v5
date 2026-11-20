import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/scratch)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
from html.parser import HTMLParser

class PageTypeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found_types = []
        self.ignored_stack = []
        
    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        
        # Check if we are entering an ignored element
        is_ignored = False
        if tag in ('header', 'nav', 'footer'):
            is_ignored = True
            
        classes = attr_dict.get('class', '').lower()
        id_str = attr_dict.get('id', '').lower()
        if 'modal' in classes or 'modal' in id_str:
            is_ignored = True
            
        if is_ignored or (self.ignored_stack and self.ignored_stack[-1]):
            self.ignored_stack.append(True)
        else:
            self.ignored_stack.append(False)
            
        for k, v in attrs:
            if k.lower() == 'data-pagetype':
                if not self.ignored_stack[-1] and v:
                    self.found_types.append(v.strip())

    def handle_endtag(self, tag):
        if self.ignored_stack:
            self.ignored_stack.pop()

parser = PageTypeParser()
parser.feed('''
<html>
<header><div data-pagetype="B2C-header"></div></header>
<main>
  <div data-pagetype=" B2B-clp "></div>
  <div class="modal"><span data-pagetype="B2B-modal"></span></div>
</main>
<footer><span data-pagetype="footer-only"></span></footer>
</html>
''')
print(parser.found_types)
