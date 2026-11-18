from html.parser import HTMLParser
import urllib.parse
from config import settings
from typing import List, Dict, Any


def parse_srcset(value: str) -> List[str]:
    """Split a srcset/data-srcset value into its candidate URLs (HTML spec tokenizer):
    candidates are separated by commas, a URL runs until whitespace, descriptors (400w, 2x) are
    dropped, and commas INSIDE a URL (e.g. CDN transforms 'w_400,h_300') are kept."""
    urls: List[str] = []
    if not value:
        return urls
    s, i, n = value, 0, len(value)
    while i < n:
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not s[j].isspace():
            j += 1
        url = s[i:j]
        if url.endswith(","):
            url = url.rstrip(",")
            i = j
        else:
            i = j
            depth = 0
            while i < n:                      # skip descriptors up to the next top-level comma
                c = s[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth = max(0, depth - 1)
                elif c == "," and depth == 0:
                    break
                i += 1
        if url:
            urls.append(url)
    return urls

class Parser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.final_base_href = base_url
        
        self.in_head = False
        self.title = ""
        self.meta_desc = ""
        self.canonical = ""
        self.meta_tags = []
        self.content_data_pagetypes = []
        self.header_data_pagetypes = []
        
        self.links = []
        self.images = []
        self.all_on_page_urls = []
        self.canonicals = []
        self.hreflangs = []
        # On-page image audit: every image-bearing attribute occurrence (collected in the same pass)
        self.all_on_page_images = []
        self._img_attrs = tuple(a.lower() for a in getattr(settings, "ON_PAGE_IMAGE_ATTRIBUTES", ["src", "srcset"]))
        self._src_attrs = tuple(a.lower() for a in getattr(settings, "ON_PAGE_IMAGE_SOURCE_ATTRIBUTES", ["srcset"]))
        self._img_meta = frozenset(k.lower() for k in getattr(settings, "ON_PAGE_IMAGE_META_KEYS", ["og:image", "twitter:image"]))
        
        self.current_tag = None
        self.current_anchor = None
        self.current_on_page_anchor = None
        
        self.path = []
        self.ignored_elements = ('header', 'footer', 'nav')
        self.preferred_selectors = ('main', 'article') # simplified preferred

    @property
    def data_pagetype(self) -> str:
        candidates = self.content_data_pagetypes or self.header_data_pagetypes
        return candidates[0] if candidates else ""
        
    def handle_starttag(self, tag, attrs):
        attr_dict = {k.lower(): (v.lower() if k.lower() in ('class', 'id', 'name', 'rel') and isinstance(v, str) else (v if v is not None else "")) for k, v in attrs if k}
        self.path.append((tag, attr_dict))
        
        if tag == 'head':
            self.in_head = True
        elif tag == 'title' and self.in_head:
            self.current_tag = 'title'
            
        elif tag == 'base':
            href = attr_dict.get('href')
            if href:
                self.final_base_href = urllib.parse.urljoin(self.base_url, href)
                
        elif tag == 'meta':
            self.meta_tags.append(attr_dict)
            name = attr_dict.get('name', '')
            if name == 'description':
                self.meta_desc = attr_dict.get('content', '')
            meta_key = (attr_dict.get('property') or name or '').strip().lower()
            if meta_key in self._img_meta:
                content = (attr_dict.get('content') or '').strip()
                if content:
                    self._add_image(content, meta_key, None, {}, 'Head' if self.in_head else self._identify_component())
                
        elif tag == 'link':
            rel = attr_dict.get('rel', '').strip()
            rel_tokens = set(rel.split()) if rel else set()
            link_href = attr_dict.get('href', '').strip()
            hreflang = attr_dict.get('hreflang', '').strip()
            if 'canonical' in rel_tokens:
                self.canonical = link_href
                if link_href:
                    self.canonicals.append(link_href)
                    self.all_on_page_urls.append({
                        'href': link_href,
                        'text': '',
                        'url_type': 'CANONICAL',
                        'rel': rel,
                        'hreflang': hreflang,
                        'component': 'Head' if self.in_head else self._identify_component(),
                    })
            elif 'alternate' in rel_tokens and hreflang and link_href:
                self.hreflangs.append({'href': link_href, 'hreflang': hreflang})
                self.all_on_page_urls.append({
                    'href': link_href,
                    'text': '',
                    'url_type': 'HREFLANG',
                    'rel': rel,
                    'hreflang': hreflang,
                    'component': 'Head' if self.in_head else self._identify_component(),
                })
            elif link_href and rel_tokens.intersection({'prev', 'next', 'amphtml', 'alternate'}):
                self.all_on_page_urls.append({
                    'href': link_href,
                    'text': '',
                    'url_type': 'LINK_META',
                    'rel': rel,
                    'hreflang': hreflang,
                    'component': 'Head' if self.in_head else self._identify_component(),
                })
                
        dpt = attr_dict.get('data-pagetype')
        if dpt and str(dpt).strip():
            val = str(dpt).strip()
            in_header = any(p[0] == 'header' for p in self.path)
            if in_header:
                self.header_data_pagetypes.append(val)
            else:
                self.content_data_pagetypes.append(val)
                    
        if tag == 'a':
            href = attr_dict.get('href')
            if href is not None and str(href).strip():
                comp = self._identify_component()
                rel = attr_dict.get('rel', '').strip()
                hreflang = attr_dict.get('hreflang', '').strip()
                self.current_anchor = {
                    'href': href,
                    'text': '',
                    'component': comp,
                    'container_type': self._identify_container_type(comp),
                    'rel': rel,
                    'hreflang': hreflang,
                }
                self.current_on_page_anchor = {
                    'href': str(href).strip(),
                    'text': '',
                    'url_type': 'ANCHOR',
                    'rel': rel,
                    'hreflang': hreflang,
                    'component': comp,
                }
        elif tag == 'area':
            href = attr_dict.get('href', '').strip()
            if href:
                self.all_on_page_urls.append({
                    'href': href,
                    'text': attr_dict.get('alt', '').strip(),
                    'url_type': 'AREA',
                    'rel': attr_dict.get('rel', '').strip(),
                    'hreflang': '',
                    'component': self._identify_component(),
                })
        elif tag == 'iframe':
            src = attr_dict.get('src', '').strip()
            if src:
                self.all_on_page_urls.append({
                    'href': src,
                    'text': attr_dict.get('title', '').strip(),
                    'url_type': 'IFRAME',
                    'rel': '',
                    'hreflang': '',
                    'component': self._identify_component(),
                })
                
        elif tag == 'source' and any(p[0] == 'picture' for p in self.path):
            comp = None
            for a in self._src_attrs:
                v = attr_dict.get(a)
                if v and str(v).strip():
                    comp = comp or self._identify_component()
                    for u in parse_srcset(str(v)):
                        self._add_image(u, f"source.{a}", None, attr_dict, comp)

        if tag == 'img':
            self._collect_img_attributes(attr_dict)

        if tag == 'img':
            src = attr_dict.get('src') or attr_dict.get('data-src') or attr_dict.get('data-lazy-src') or attr_dict.get('data-original')
            if not src and 'srcset' in attr_dict:
                src = attr_dict['srcset'].split(',')[0].strip().split(' ')[0]
            if not src and 'data-srcset' in attr_dict:
                src = attr_dict['data-srcset'].split(',')[0].strip().split(' ')[0]
                
            if src:
                alt = attr_dict.get('alt', '')
                title_attr = attr_dict.get('title', '')
                loading = attr_dict.get('loading', 'eager')
                if 'data-src' in attr_dict or 'data-lazy-src' in attr_dict:
                    loading = 'lazy'
                    
                comp = self._identify_component()
                self.images.append({
                    'url': src,
                    'alt': alt,
                    'title': title_attr,
                    'loading_type': loading,
                    'component': comp,
                    'container_type': self._identify_container_type(comp)
                })
                
    def _add_image(self, raw: str, attribute_source: str, alt, attrs: Dict[str, Any], component: str):
        # alt: None = not applicable/unknown (meta tags, <source>); otherwise (present, text) from the <img>
        self.all_on_page_images.append({
            'src': raw,
            'attribute_source': attribute_source,
            'alt_present': None if alt is None else alt[0],
            'alt_text': '' if alt is None else alt[1],
            'width': str(attrs.get('width', '') or '').strip()[:20],
            'height': str(attrs.get('height', '') or '').strip()[:20],
            'loading': str(attrs.get('loading', '') or '').strip()[:20],
            'component': component,
        })

    def _collect_img_attributes(self, attr_dict: Dict[str, Any]):
        alt = ('alt' in attr_dict, ' '.join(str(attr_dict.get('alt') or '').split())[:250])
        comp = None
        for a in self._img_attrs:
            v = attr_dict.get(a)
            if v is None or not str(v).strip():
                continue
            comp = comp or self._identify_component()
            if a.endswith('srcset'):
                for u in parse_srcset(str(v)):
                    self._add_image(u, f"img.{a}", alt, attr_dict, comp)
            else:
                self._add_image(str(v).strip(), f"img.{a}", alt, attr_dict, comp)

    def handle_endtag(self, tag):
        if tag == 'head':
            self.in_head = False
        elif tag == 'title':
            self.current_tag = None
            
        if tag == 'a':
            if self.current_anchor:
                self.links.append(self.current_anchor)
                self.current_anchor = None
            if self.current_on_page_anchor:
                self.all_on_page_urls.append(self.current_on_page_anchor)
                self.current_on_page_anchor = None
            
        if self.path:
            self.path.pop()
            
    def handle_data(self, data):
        if self.current_tag == 'title':
            self.title += data
        if self.current_anchor:
            self.current_anchor['text'] += data
        if self.current_on_page_anchor:
            self.current_on_page_anchor['text'] += data
            
    def _identify_component(self):
        for tag, attrs in reversed(self.path):
            if tag in self.ignored_elements:
                return 'Header/Footer'
            classes = attrs.get('class', '')
            id_str = attrs.get('id', '')
            if 'modal' in classes or 'modal' in id_str:
                return 'Header/Footer'
        return 'Content Section'
        
    def _identify_container_type(self, comp: str = None):
        # returns 'ignored', 'preferred', or 'body'
        if comp is None:
            comp = self._identify_component()
        if comp == 'Header/Footer':
            return 'ignored'
        for tag, attrs in self.path:
            if tag in self.preferred_selectors:
                return 'preferred'
            classes = attrs.get('class', '')
            id_str = attrs.get('id', '')
            if 'main' in classes or 'main' in id_str or 'content' in classes or 'content' in id_str:
                return 'preferred'
            if attrs.get('role') == 'main':
                return 'preferred'
        return 'body'
        
    def resolve_url(self, url):
        if not url: return None
        url = url.strip()
        if url.startswith(('javascript:', 'mailto:', 'tel:', 'data:', 'blob:', '#')):
            return None
        return urllib.parse.urljoin(self.final_base_href, url)

    def get_filtered_links(self):
        # Priority: 1. preferred, 2. body. (exclude ignored)
        preferred = [l for l in self.links if l['container_type'] == 'preferred']
        if preferred: return preferred
        
        body = [l for l in self.links if l['container_type'] == 'body']
        return body

    def get_filtered_images(self):
        preferred = [img for img in self.images if img['container_type'] == 'preferred']
        if preferred: return preferred
        
        body = [img for img in self.images if img['container_type'] == 'body']
        return body

    def get_all_on_page_urls(self) -> List[Dict[str, Any]]:
        """
        Return all discovered URL-bearing elements (anchor, canonical, hreflang,
        link meta, area, iframe) across the entire document, preserving rel,
        hreflang, anchor text, and component location.
        """
        if self.current_on_page_anchor is not None:
            self.all_on_page_urls.append(self.current_on_page_anchor)
            self.current_on_page_anchor = None
        return list(self.all_on_page_urls)


