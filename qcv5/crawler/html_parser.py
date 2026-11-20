from html.parser import HTMLParser
import hashlib
import json
import re
import urllib.parse
from qcv5.core.config import settings
from typing import List, Dict, Any


IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif|svg|bmp|ico|tiff?|jfif)(?:[?#]|$)", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)
REFRESH_URL_RE = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.IGNORECASE)
# <link rel> values whose href is a fetchable resource (preconnect/dns-prefetch name origins, not resources)
RESOURCE_RELS = {"icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon", "manifest",
                 "preload", "prefetch", "modulepreload", "prerender"}
JSONLD_IMAGE_KEYS = {"image", "logo", "thumbnailurl", "contenturl"}
JSONLD_URL_KEYS = {"url", "@id", "sameas", "item", "mainentityofpage", "target"}


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
        
        # SEO / technical-SEO signals, collected in the SAME pass (no second parse)
        self.title_count = 0
        self.meta_desc_count = 0
        self.html_lang = None              # None = no <html lang>, '' = empty attribute
        self.has_viewport = False
        self.has_charset = False
        self.meta_robots: List[str] = []   # content of <meta name=robots|googlebot>
        self.meta_refresh = ""
        self.meta_props: Dict[str, str] = {}   # og:* / twitter:* (first value wins)
        self.h1s: List[str] = []
        self.heading_counts = [0] * 7
        self.heading_skips: List[str] = []     # e.g. 'h2->h4'
        self._last_heading = 0
        self._h1_buf = None
        self.jsonld_blocks: List[str] = []
        self._jsonld_buf = None
        self._jsonld_bytes = 0
        self.canonical_outside_head = 0
        self.insecure_resources: List[str] = []   # http:// scripts, stylesheets, iframes (images added later)
        self.word_count = 0
        self._text_hash = hashlib.blake2b(digest_size=12)
        self._skip_depth = 0
        self._style_buf = None
        self._finalized = False
        self.script_count = 0          # external + inline scripts (JSON-LD excluded)
        self.spa_root = ''             # id/attribute of a client-side app mount point, if any

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
            self.title_count += 1
        elif tag == 'html':
            if 'lang' in attr_dict and self.html_lang is None:
                self.html_lang = str(attr_dict.get('lang') or '').strip()[:35]
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            lvl = int(tag[1])
            self.heading_counts[lvl] += 1
            if self._last_heading and lvl > self._last_heading + 1 and len(self.heading_skips) < 10:
                self.heading_skips.append(f"h{self._last_heading}->h{lvl}")
            self._last_heading = lvl
            if lvl == 1:
                self._h1_buf = []
        elif tag in ('script', 'style', 'noscript', 'template'):
            self._skip_depth += 1
            if tag == 'script':
                stype = str(attr_dict.get('type') or '').strip().lower()
                if stype != 'application/ld+json':
                    self.script_count += 1
                if stype == 'application/ld+json' and len(self.jsonld_blocks) < 50:
                    self._jsonld_buf = []
                src = str(attr_dict.get('src') or '').strip()
                if src.lower().startswith('http://') and len(self.insecure_resources) < 50:
                    self.insecure_resources.append('script:' + src[:300])
                if src:
                    self._add_url(src, 'SCRIPT', attr_dict)
            elif tag == 'style':
                self._style_buf = []
            
        elif tag == 'base':
            href = attr_dict.get('href')
            if href:
                self.final_base_href = urllib.parse.urljoin(self.base_url, href)
                
        elif tag == 'meta':
            self.meta_tags.append(attr_dict)
            name = attr_dict.get('name', '')
            if name == 'description':
                self.meta_desc_count += 1
                if self.meta_desc_count == 1:
                    self.meta_desc = attr_dict.get('content', '')
            elif name in ('robots', 'googlebot'):
                self.meta_robots.append(str(attr_dict.get('content') or '').strip().lower()[:200])
            elif name == 'viewport':
                self.has_viewport = True
            if 'charset' in attr_dict:
                self.has_charset = True
            http_equiv = str(attr_dict.get('http-equiv') or '').strip().lower()
            if http_equiv == 'content-type' and 'charset' in str(attr_dict.get('content') or '').lower():
                self.has_charset = True
            elif http_equiv == 'refresh' and not self.meta_refresh:
                self.meta_refresh = str(attr_dict.get('content') or '').strip()[:200] or '0'
                m = REFRESH_URL_RE.search(self.meta_refresh)
                if m and m.group(1).strip():
                    self._add_url(m.group(1).strip(), 'META_REFRESH', attr_dict)
            prop = str(attr_dict.get('property') or name or '').strip().lower()
            if (prop.startswith('og:') or prop.startswith('twitter:')) and prop not in self.meta_props:
                self.meta_props[prop] = str(attr_dict.get('content') or '').strip()[:300]
            if prop in ('og:url', 'twitter:url'):
                content = str(attr_dict.get('content') or '').strip()
                if content:
                    self._add_url(content, 'SOCIAL_URL', attr_dict)
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
            if 'stylesheet' in rel_tokens and link_href.lower().startswith('http://') \
                    and len(self.insecure_resources) < 50:
                self.insecure_resources.append('stylesheet:' + link_href[:300])
            if link_href and 'stylesheet' in rel_tokens:
                self._add_url(link_href, 'STYLESHEET', attr_dict)
            elif link_href and rel_tokens & RESOURCE_RELS:
                self._add_url(link_href, 'LINK_RESOURCE', attr_dict)
            if 'canonical' in rel_tokens:
                if not self.in_head:
                    self.canonical_outside_head += 1
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
                    
        if not self.spa_root and tag == 'div':
            did = str(attr_dict.get('id') or '')
            if did in ('root', 'app', '__next', '__nuxt', '___gatsby', 'svelte'):
                self.spa_root = did
            elif 'data-reactroot' in attr_dict or 'ng-version' in attr_dict:
                self.spa_root = 'data-reactroot' if 'data-reactroot' in attr_dict else 'ng-version'
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
            if src.lower().startswith('http://') and len(self.insecure_resources) < 50:
                self.insecure_resources.append('iframe:' + src[:300])
            if src:
                self.all_on_page_urls.append({
                    'href': src,
                    'text': attr_dict.get('title', '').strip(),
                    'url_type': 'IFRAME',
                    'rel': '',
                    'hreflang': '',
                    'component': self._identify_component(),
                })
                
        elif tag in ('video', 'audio', 'track', 'embed') or (tag == 'source' and any(p[0] in ('video', 'audio') for p in self.path)):
            src = str(attr_dict.get('src') or '').strip()
            if src:
                self._add_url(src, 'EMBED' if tag == 'embed' else 'MEDIA', attr_dict)
            poster = str(attr_dict.get('poster') or '').strip()
            if poster:
                self._add_image(poster, 'video.poster', None, attr_dict, self._identify_component())
        elif tag == 'object':
            data = str(attr_dict.get('data') or '').strip()
            if data:
                self._add_url(data, 'EMBED', attr_dict)
        elif tag == 'form':
            action = str(attr_dict.get('action') or '').strip()
            if action:
                self._add_url(action, 'FORM', attr_dict, text=str(attr_dict.get('method') or 'get').lower())
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
        style = attr_dict.get('style')
        if style and 'url(' in str(style).lower():
            self._css_urls(str(style), 'css.style')

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
                
    def _add_url(self, href: str, url_type: str, attrs: Dict[str, Any], text: str = ''):
        """Any non-anchor URL on the page (scripts, styles, media, forms, social, JSON-LD...) as a typed entry."""
        self.all_on_page_urls.append({
            'href': href[:2000], 'text': text, 'url_type': url_type, 'rel': str(attrs.get('rel') or '').strip(),
            'hreflang': '', 'component': 'Head' if self.in_head else self._identify_component(),
        })

    def _css_urls(self, css: str, source: str):
        """url(...) references in CSS: images go to the image audit, other assets (fonts...) to CSS_RESOURCE."""
        for m in CSS_URL_RE.finditer(css[:200000]):
            u = m.group(2).strip()
            if not u or u.lower().startswith(('data:', '#')):
                continue
            if IMAGE_EXT_RE.search(u):
                self._add_image(u, source, None, {}, 'Head' if self.in_head else self._identify_component())
            else:
                self._add_url(u, 'CSS_RESOURCE', {})

    def _jsonld_walk(self, node: Any, depth: int = 0, key: str = ''):
        if depth > 8:
            return
        if isinstance(node, list):
            for n in node[:200]:
                self._jsonld_walk(n, depth + 1, key)
        elif isinstance(node, dict):
            for k, v in list(node.items())[:200]:
                self._jsonld_walk(v, depth + 1, str(k).lower())
        elif isinstance(node, str) and key and node[:8].lower().startswith(('http://', 'https://', '/')):
            if key in JSONLD_IMAGE_KEYS:
                self._add_image(node.strip(), 'jsonld.' + key, None, {}, 'Head')
            elif key in JSONLD_URL_KEYS and not node.startswith('_:'):
                self.all_on_page_urls.append({'href': node.strip()[:2000], 'text': key, 'url_type': 'JSONLD_URL',
                                              'rel': '', 'hreflang': '', 'component': 'Structured data'})

    def finalize(self):
        """Once, after feed(): URLs and images referenced from JSON-LD. Idempotent."""
        if self._finalized:
            return
        self._finalized = True
        for b in self.jsonld_blocks:
            try:
                self._jsonld_walk(json.loads(b))
            except (ValueError, RecursionError):
                continue   # invalid JSON-LD is reported by the SEO rules, not here

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
        elif tag == 'h1' and self._h1_buf is not None:
            if len(self.h1s) < 10:
                self.h1s.append(' '.join(''.join(self._h1_buf).split())[:200])
            self._h1_buf = None
        elif tag in ('script', 'style', 'noscript', 'template'):
            self._skip_depth = max(0, self._skip_depth - 1)
            if tag == 'style' and self._style_buf is not None:
                self._css_urls(''.join(self._style_buf), 'css.style_block')
                self._style_buf = None
            if tag == 'script' and self._jsonld_buf is not None:
                self.jsonld_blocks.append(''.join(self._jsonld_buf))
                self._jsonld_buf = None
            
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
        if self._skip_depth:
            if self._jsonld_buf is not None and self._jsonld_bytes < 512 * 1024:
                self._jsonld_buf.append(data)
                self._jsonld_bytes += len(data)
            elif self._style_buf is not None:
                self._style_buf.append(data)
        elif not self.in_head and self.current_tag != 'title':
            words = data.split()
            if words:
                self.word_count += len(words)
                self._text_hash.update((' '.join(words).lower() + ' ').encode('utf-8', 'ignore'))
            if self._h1_buf is not None:
                self._h1_buf.append(data)
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

    @property
    def content_hash(self) -> str:
        """Hash of the visible body text (whitespace/case-normalized); '' when the page has no text."""
        return self._text_hash.hexdigest() if self.word_count else ''

    def get_all_on_page_urls(self) -> List[Dict[str, Any]]:
        """
        Return all discovered URL-bearing elements (anchor, canonical, hreflang,
        link meta, area, iframe) across the entire document, preserving rel,
        hreflang, anchor text, and component location.
        """
        if self.current_on_page_anchor is not None:
            self.all_on_page_urls.append(self.current_on_page_anchor)
            self.current_on_page_anchor = None
        self.finalize()
        return list(self.all_on_page_urls)


