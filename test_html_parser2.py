from html.parser import HTMLParser

class PageTypeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found_types = []
        self.ignored_stack = []
        
    def handle_starttag(self, tag, attrs):
        self._process_tag(tag, attrs, is_startend=False)
        
    def handle_startendtag(self, tag, attrs):
        self._process_tag(tag, attrs, is_startend=True)
        
    def _process_tag(self, tag, attrs, is_startend):
        attr_dict = dict(attrs)
        
        is_ignored = False
        if tag in ('header', 'nav', 'footer'):
            is_ignored = True
            
        classes = attr_dict.get('class', '').lower()
        id_str = attr_dict.get('id', '').lower()
        if 'modal' in classes or 'modal' in id_str:
            is_ignored = True
            
        current_ignore = is_ignored or (self.ignored_stack and self.ignored_stack[-1])
        if not is_startend:
            self.ignored_stack.append(current_ignore)
            
        for k, v in attrs:
            if k.lower() == 'data-pagetype':
                if not current_ignore and v:
                    self.found_types.append(v.strip())

    def handle_endtag(self, tag):
        # We can't strictly match tag names if HTML is malformed, 
        # but in most cases popping the stack is enough if it's well-formed.
        # HTMLParser doesn't guarantee well-formedness.
        pass

# Wait, if HTML is malformed, just popping the stack on handle_endtag will completely break!
# A safer approach is to not use a stack, but just find the first `data-pagetype` attribute 
# and if there are multiple, resolve them.
