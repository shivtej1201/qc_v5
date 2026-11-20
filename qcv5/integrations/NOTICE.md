# Third-party notice

Parts of this package are ported or adapted from **claude-seo**
(https://github.com/AgriciDaniel/claude-seo), MIT License, Copyright (c) 2026 agricidaniel.
The full licence text is in `data/CLAUDE_SEO_LICENSE.txt`.

| QC V5 module | claude-seo source |
|---|---|
| `ai_access.py` (robots.txt RFC 9309 parser, AI agent list, Content-Signal, llms.txt) | `scripts/agentic_check.py` |
| `security_headers.py` (HSTS / CSP / framing / Referrer-Policy / cookie grading) | `scripts/transport_check.py` |
| `structured_data.py` (Product / Offer rules, retired rich-result types) | `scripts/schema_ecommerce_validate.py` |
| `data/schema_templates.json` | `schema/templates.json` (copied unchanged) |
| `pagespeed.py` (PSI / CrUX parsing, Core Web Vitals thresholds) | `scripts/pagespeed_check.py` |
| `indexnow.py` | `scripts/indexnow_submit.py` |
| `bing_webmaster.py` | `scripts/bing_webmaster.py` |
| `google_updates.py`, `data/google_updates.json` | `scripts/seo_updates.py`, `data/google-updates.json` (copied unchanged) |
| `wayback.py` | idea from `scripts/domain_history.py` (archive.org CDX instead of WHOIS) |
| `google_search_console.py` | scope of `scripts/gsc_query.py`, `gsc_inspect.py`, `ga4_report.py` (own JWT auth) |

All network access goes through QC V5's own SSRF-guarded fetcher (`base.SafeFetcher`), not claude-seo's.
