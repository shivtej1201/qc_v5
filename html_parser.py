from html.parser import HTMLParser
import urllib.parse
from config import settings
from typing import List, Dict, Any

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
        
        self.current_tag = None
        self.current_anchor = None
        
        self.path = []
        self.ignored_elements = ('header', 'footer', 'nav')
        self.preferred_selectors = ('main', 'article') # simplified preferred

    @property
    def data_pagetype(self) -> str:
        candidates = self.content_data_pagetypes or self.header_data_pagetypes
        return candidates[0] if candidates else ""
        
    def handle_starttag(self, tag, attrs):
        attr_dict = {k.lower(): (v if v is not None else "") for k, v in attrs if k}
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
            name = attr_dict.get('name', '').lower()
            if name == 'description':
                self.meta_desc = attr_dict.get('content', '')
                
        elif tag == 'link':
            rel = attr_dict.get('rel', '').lower()
            if rel == 'canonical':
                self.canonical = attr_dict.get('href', '')
                
        for k, v in attrs:
            if k.lower() == 'data-pagetype' and v and v.strip():
                val = v.strip()
                in_header = any(p[0] == 'header' for p in self.path)
                if in_header:
                    self.header_data_pagetypes.append(val)
                else:
                    self.content_data_pagetypes.append(val)
                    
        if tag == 'a':
            href = attr_dict.get('href')
            if href:
                self.current_anchor = {
                    'href': href,
                    'text': '',
                    'component': self._identify_component(),
                    'container_type': self._identify_container_type()
                }
                
        elif tag == 'img':
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
                    
                self.images.append({
                    'url': src,
                    'alt': alt,
                    'title': title_attr,
                    'loading_type': loading,
                    'component': self._identify_component(),
                    'container_type': self._identify_container_type()
                })
                
    def handle_endtag(self, tag):
        if tag == 'head':
            self.in_head = False
        elif tag == 'title':
            self.current_tag = None
            
        if tag == 'a' and self.current_anchor:
            self.links.append(self.current_anchor)
            self.current_anchor = None
            
        if self.path:
            self.path.pop()
            
    def handle_data(self, data):
        if self.current_tag == 'title':
            self.title += data
        if self.current_anchor:
            self.current_anchor['text'] += data
            
    def _identify_component(self):
        for tag, attrs in reversed(self.path):
            if tag in self.ignored_elements:
                return 'Header/Footer'
            classes = attrs.get('class', '').lower()
            id_str = attrs.get('id', '').lower()
            if 'modal' in classes or 'modal' in id_str:
                return 'Header/Footer'
        return 'Content Section'
        
    def _identify_container_type(self):
        # returns 'ignored', 'preferred', or 'body'
        comp = self._identify_component()
        if comp == 'Header/Footer':
            return 'ignored'
        for tag, attrs in self.path:
            if tag in self.preferred_selectors:
                return 'preferred'
            classes = attrs.get('class', '').lower()
            id_str = attrs.get('id', '').lower()
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

