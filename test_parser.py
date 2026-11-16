from html_parser import Parser
html = '<html><body><main><a href="http://example.com/test">link</a></main></body></html>'
p = Parser('http://example.com/')
p.feed(html)
print('All links:', p.links)
print('Filtered:', p.get_filtered_links())
