import urllib.parse
from typing import Dict, Optional, Tuple, List, Any
from httpx import AsyncClient
from qcv5.core.config import settings
from qcv5.crawler.html_parser import Parser
import re

class ClassificationResult(tuple):
    """
    Classification result tuple supporting both 3-tuple legacy unpacking:
      pt, biz, method = result
    and rich property access:
      result.page_type, result.business, result.method, result.tag_1, result.tag_2, result.diagnostic_notes
    as well as full 6-tuple conversion:
      result.to_full_tuple()
    """
    def __new__(cls, page_type: str, business: str, method: str, 
                tag_1: Optional[str] = None, tag_2: Optional[str] = None, 
                diagnostic_notes: Optional[str] = None):
        instance = super().__new__(cls, (page_type, business, method))
        instance.page_type = page_type
        instance.business = business
        instance.method = method
        instance.classification_method = method
        instance.tag_1 = tag_1
        instance.tag_2 = tag_2
        instance.diagnostic_notes = diagnostic_notes or ""
        return instance

    def to_full_tuple(self):
        return (self.page_type, self.business, self.method, self.tag_1, self.tag_2, self.diagnostic_notes)

    def to_dict(self):
        return {
            "page_type": self.page_type,
            "business": self.business,
            "classification_method": self.method,
            "method": self.method,
            "tag_1": self.tag_1,
            "tag_2": self.tag_2,
            "diagnostic_notes": self.diagnostic_notes
        }

class Classifier:
    def __init__(self):
        # Layer A: URL Regex Rules
        self.url_rules = [
            (r'loan', 'Business Loan', 'PDP'),
            (r'insurance', 'Insurance', 'PDP'),
            (r'category', 'Unknown', 'PLP'),
            (r'^https?://[^/]+/?$', 'Unknown', 'Home'),
        ]

    def _classify_by_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        for pattern, business, p_type in self.url_rules:
            if re.search(pattern, url, re.IGNORECASE):
                return p_type, business
        return None, None
        
    def _classify_by_data_pagetype(self, html: str, parser: Optional[Parser] = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if not html:
            return None, None, None
        if parser is None:
            parser = Parser("http://localhost")
            try:
                parser.feed(html)
            except Exception:
                pass
            
        candidates = parser.content_data_pagetypes or parser.header_data_pagetypes
        if not candidates:
            return None, None, None
            
        distinct_vals = list(dict.fromkeys([c.strip() for c in candidates if c.strip()]))
        if not distinct_vals:
            return None, None, None
            
        lower_map = {}
        for v in distinct_vals:
            lower_map[v.lower()] = v
        if len(lower_map) > 1:
            return None, None, f"Ambiguous (Conflicting data-pageType: {', '.join(distinct_vals)})"
            
        raw_val = distinct_vals[0]
        raw_val_lower = raw_val.lower()
        mapped = settings.PAGETYPE_MAPPING.get(raw_val_lower)
        if mapped:
            return mapped.get("page_type"), mapped.get("business"), "HTML_DATA_PAGETYPE"
        
        parts = raw_val.split(settings.PAGETYPE_DELIMITER)
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1].upper(), parts[0].upper(), "HTML_DATA_PAGETYPE"
        if len(parts) == 1 and parts[0]:
            single_token = parts[0].strip().upper()
            if single_token in ["PLP", "PDP", "CLP", "HOME"]:
                return single_token, None, "HTML_DATA_PAGETYPE"
            
        return None, None, f"HTML_DATA_PAGETYPE (Unknown: {raw_val})"

    def extract_meta_tags(self, html: str, parser: Optional[Parser] = None) -> Tuple[Optional[str], Optional[str], List[str]]:
        tag_1_candidates: List[str] = []
        tag_2_candidates: List[str] = []
        diagnostics: List[str] = []

        # 1. From parser if available
        if parser and getattr(parser, "meta_tags", None):
            for attrs in parser.meta_tags:
                name = (attrs.get("name") or "").strip().lower()
                content = (attrs.get("content") or "").strip()
                if name == "tag-1" and content:
                    tag_1_candidates.append(content)
                elif name == "tag-2" and content:
                    tag_2_candidates.append(content)

        # 2. Regex scan on raw HTML (handles arbitrary attribute order, quote styles, spacing, self-closing, malformed unclosed tags)
        if html:
            meta_matches = re.finditer(r'<meta\b[^>]*?(?:>|(?=<)|$)', html, re.IGNORECASE)
            for m in meta_matches:
                tag_str = m.group(0)
                if '>' in tag_str:
                    tag_str = tag_str.split('>', 1)[0] + '>'
                name_m = re.search(r'\bname\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag_str, re.IGNORECASE)
                content_m = re.search(r'\bcontent\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag_str, re.IGNORECASE)
                if name_m and content_m:
                    name_val = (name_m.group(1) or name_m.group(2) or name_m.group(3) or "").strip().lower()
                    c_val = (content_m.group(1) or content_m.group(2) or content_m.group(3) or "").strip()
                    if c_val:
                        if name_val == "tag-1":
                            tag_1_candidates.append(c_val)
                        elif name_val == "tag-2":
                            tag_2_candidates.append(c_val)

        tag_1_selected: Optional[str] = None
        tag_2_selected: Optional[str] = None

        if tag_1_candidates:
            # First valid non-empty value trimmed and normalized to uppercase
            first_valid = next((v.strip().upper() for v in tag_1_candidates if v.strip()), None)
            if first_valid:
                tag_1_selected = first_valid
                distinct_1 = list(dict.fromkeys([v.strip().upper() for v in tag_1_candidates if v.strip()]))
                if len(distinct_1) > 1:
                    diagnostics.append(f"Conflicting tag-1 meta tags found: {', '.join(distinct_1)}; selected first valid: '{tag_1_selected}'.")

        if tag_2_candidates:
            first_valid = next((v.strip().upper() for v in tag_2_candidates if v.strip()), None)
            if first_valid:
                tag_2_selected = first_valid
                distinct_2 = list(dict.fromkeys([v.strip().upper() for v in tag_2_candidates if v.strip()]))
                if len(distinct_2) > 1:
                    diagnostics.append(f"Conflicting tag-2 meta tags found: {', '.join(distinct_2)}; selected first valid: '{tag_2_selected}'.")

        return tag_1_selected, tag_2_selected, diagnostics

    def classify_from_html(self, requested_url: str, final_url: str, html: str, parser: Optional[Parser] = None) -> ClassificationResult:
        # Note: Gating on "bajajfinserv.in" removed per Phase 10.9.2 requirements
        # `parser` may be an already-fed Parser for this exact HTML (on-page audit parse-once path);
        # when omitted the HTML is parsed here exactly as before.
        if parser is None and html:
            parser = Parser(final_url or requested_url)
            try:
                parser.feed(html)
            except Exception:
                pass

        tag_1, tag_2, meta_diags = self.extract_meta_tags(html, parser=parser)
        
        page_type: Optional[str] = None
        business: Optional[str] = None
        method_components: List[str] = []
        diagnostics: List[str] = list(meta_diags)

        # 1. Level 1: META_TAG precedence
        if tag_1:
            page_type = tag_1
            method_components.append("META_TAG")
        if tag_2:
            business = tag_2
            if "META_TAG" not in method_components:
                method_components.append("META_TAG")

        # 2. Level 2: Trusted HTML rules (data-pageType)
        if not page_type or not business:
            pt_html, biz_html, method_html = self._classify_by_data_pagetype(html, parser=parser)
            if method_html and ("Ambiguous" in method_html or "Unknown:" in method_html):
                diagnostics.append(method_html)
            if not page_type and pt_html:
                page_type = pt_html
                method_components.append("HTML_DATA_PAGETYPE")
            if not business and biz_html:
                business = biz_html
                if "HTML_DATA_PAGETYPE" not in method_components:
                    method_components.append("HTML_DATA_PAGETYPE")

        # 3. Level 3: Source URL pattern fallback
        if not page_type or not business:
            url_to_check = final_url or requested_url
            pt_url, biz_url = self._classify_by_url(url_to_check)
            if not page_type and pt_url and pt_url != "Unknown":
                page_type = pt_url
                method_components.append("URL_FALLBACK")
            if not business and biz_url and biz_url != "Unknown":
                business = biz_url
                if "URL_FALLBACK" not in method_components:
                    method_components.append("URL_FALLBACK")

        # HTML title fallback (Home) if applicable
        if (not page_type or page_type == "Unknown") and parser and parser.title:
            title_lower = parser.title.lower()
            if "home" in title_lower or "homepage" in title_lower:
                page_type = "Home"
                if "HTML_METADATA" not in method_components:
                    method_components.append("HTML_METADATA")

        # 4. Level 4: Explicit NEEDS_REVIEW / NOT_AVAILABLE when evidence insufficient
        if not page_type or page_type == "Unknown":
            page_type = "NEEDS_REVIEW"
        if not business or business == "Unknown":
            business = "NEEDS_REVIEW"

        # Determine method string
        if not method_components:
            if not html or not html.strip():
                method = "NO_HTML"
                diagnostics.append("Empty HTML content received; unable to inspect metadata or attributes.")
            else:
                method = "INSUFFICIENT_EVIDENCE"
                diagnostics.append("No tag-1/tag-2 meta tags, data-pageType attributes, or URL patterns matched.")
        elif len(method_components) == 1:
            method = method_components[0]
        else:
            method = " + ".join(method_components)

        diag_notes = "; ".join(diagnostics) if diagnostics else "Classification completed successfully."

        return ClassificationResult(
            page_type=page_type,
            business=business,
            method=method,
            tag_1=tag_1,
            tag_2=tag_2,
            diagnostic_notes=diag_notes
        )

    def classify_full(self, requested_url: str, final_url: str, html: str) -> Tuple[str, str, str, Optional[str], Optional[str], str]:
        res = self.classify_from_html(requested_url, final_url, html)
        return res.to_full_tuple()

    async def classify_url(self, url: str) -> ClassificationResult:
        html = ""
        final_url = url
        try:
            async with AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code == 200:
                    html = resp.text
                    final_url = str(resp.url)
        except Exception:
            pass
            
        return self.classify_from_html(url, final_url, html)
