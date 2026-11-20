"""Free SEO integrations (AI crawler access, security headers, structured data, PageSpeed / CrUX, Wayback,
IndexNow, Bing Webmaster, Google Search Console + GA4, Google update timeline).

Several checks are ported from claude-seo (MIT, (c) 2026 agricidaniel); see NOTICE.md.
"""
from .registry import BY_ID, INTEGRATIONS, catalogue, run_integration  # noqa: F401
