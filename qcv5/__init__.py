"""QC V5 — on-page URL, image and SEO audit platform.

Packages:
    qcv5.core          settings, database, models, shared errors
    qcv5.crawler       audit engine, HTTP pools, HTML parsing, URL normalisation, sitemaps, validators
    qcv5.audit         issue rules and store, SEO checks, remediation
    qcv5.reports       CSV / ZIP / Excel report packaging
    qcv5.services      on-page audit service, job manager, worker daemon
    qcv5.integrations  free SEO integrations (AI crawlers, security headers, schema, PageSpeed, Wayback, IndexNow,
                       Bing Webmaster, Search Console + GA4, Google update timeline)
    qcv5.api           FastAPI app (REST API + dashboard)
    qcv5.cli           command-line audit worker
"""
__version__ = "5.1.0"
