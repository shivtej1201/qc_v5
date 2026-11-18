# QC V5 — Bajaj Mall 12-Sitemap On-Page URL Extraction & Status Audit (As-of-Now Completion Report)

- **Snapshot Timestamp (UTC):** `2026-11-18T03:08:43.128735Z`
- **QC V5 Job ID:** `onpage_9c0c3263702e`
- **Audit Phase:** Live Snapshot during Stage A (`93,200 / 124,474` source pages completed = **74.88%**; background crawl still running cleanly)
- **Akamai Edge Status:** **0 WAF blocks / 0 cooldowns** across `93,200` live requests

## 1. As-of-Now Measured Metrics

```text
QC V5 ON-PAGE URL AUDIT — AS-OF-NOW SNAPSHOT

Source pages total:                 124,474
Source pages processed (so far):    93,200 (74.88%)
  - Completed (fetched & parsed):   92,897
  - Timed out:                      281
  - Failed (DNS/Network/Error):     22
  - Blocked (WAF / Robots):         0
  - Remaining (Pending Stage A):    31,274

URL occurrences discovered:         3,563,988
Relationship rows persisted:        3,527,721
Unique HTTP destinations:           100,208
  - Already validated (live):       92,681 (92.49%)
  - Pending Stage B validation:     7,527

New HTTP validations (NEW_CHECK):   100,208
Reused validations (REUSED):        3,427,513
Validation Cache Reuse Rate:        97.16%

Unique HTTP Destination Buckets (So Far):
  - 2xx                92,558
  - 3xx                0
  - 4xx                0
  - 5xx                0
  - Timeout            118
  - DNS/Network        5
  - Robots blocked     0
  - WAF blocked        0
  - PENDING            7,527

Snapshot Reconciliation:            PASS
Security (SSRF / Robots / WAF):     PASS
Final Full-Run Gate:                IN_PROGRESS (awaiting remaining pending source pages & Stage B)
```

## 2. Sitemap-Wise Progress Across All 12 Production Sitemaps

| # | Sitemap URL | Assigned Unique | Processed | Pending | Completed | Failed | Timed Out | Completion % | Relationships |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-en-pdp-b2b-bmall-sitemap-4.xml` | 16,347 | 16,347 | 0 | 16,347 | 0 | 0 | 100.0% | 757,966 |
| 2 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-bmall-sitemap.xml` | 17,514 | 17,514 | 0 | 17,514 | 0 | 0 | 100.0% | 557,832 |
| 3 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-en-pdp-b2b-bmall-sitemap-2.xml` | 16,493 | 16,493 | 0 | 16,493 | 0 | 0 | 100.0% | 584,596 |
| 4 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-bmall-sitemap-3.xml` | 628 | 628 | 0 | 628 | 0 | 0 | 100.0% | 26,665 |
| 5 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-en-plppage-bmall-sitemap.xml` | 2,183 | 2,183 | 0 | 2,183 | 0 | 0 | 100.0% | 89,011 |
| 6 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-hi-pdp-bmall-sitemap.xml` | 13,204 | 13,204 | 0 | 13,204 | 0 | 0 | 100.0% | 536,461 |
| 7 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-en-pdp-b2b-bmall-sitemap-3.xml` | 17,725 | 17,725 | 0 | 17,725 | 0 | 0 | 100.0% | 579,875 |
| 8 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-hi-pdp-bmall-sitemap-2.xml` | 5,700 | 5,700 | 0 | 5,557 | 3 | 140 | 100.0% | 187,784 |
| 9 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-en-pdp-b2b-bmall-sitemap.xml` | 14,731 | 3,406 | 11,325 | 3,246 | 19 | 141 | 23.12% | 207,531 |
| 10 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-hi-bmall-sitemap.xml` | 1,639 | 0 | 1,639 | 0 | 0 | 0 | 0.0% | 0 |
| 11 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-bmall-sitemap-2.xml` | 18,292 | 0 | 18,292 | 0 | 0 | 0 | 0.0% | 0 |
| 12 | `https://www.bajajfinserv.in/bajajfinserv.sitemap.bajajmall-site-in-en-clppages-b2b-clp-bmall-sitemap.xml` | 18 | 0 | 18 | 0 | 0 | 0 | 0.0% | 0 |

## 3. Business-Wise Summary (Processed Source Pages)

| Business | Source Pages Processed | Completed | Failed | Blocked | Timed Out | Relationships Extracted |
|---|---:|---:|---:|---:|---:|---:|
| CD | 38,069 | 38,069 | 0 | 0 | 0 | 1,640,017 |
| NA | 22,858 | 22,858 | 0 | 0 | 0 | 642,219 |
| LSF | 15,832 | 15,832 | 0 | 0 | 0 | 581,485 |
| DPF | 11,683 | 11,683 | 0 | 0 | 0 | 533,589 |
| NCF | 1,711 | 1,711 | 0 | 0 | 0 | 74,293 |
| NEEDS_REVIEW | 1,159 | 856 | 22 | 0 | 281 | 17,444 |
| TW | 962 | 962 | 0 | 0 | 0 | 14,514 |
| TRACTOR | 178 | 178 | 0 | 0 | 0 | 570 |
| సిడి | 148 | 148 | 0 | 0 | 0 | 7,311 |
| BMALL1 | 147 | 147 | 0 | 0 | 0 | 447 |
| ఎన్‌సిఎఫ్ | 143 | 143 | 0 | 0 | 0 | 8,505 |
| టిడబ్ల్యూ | 93 | 93 | 0 | 0 | 0 | 4,798 |
| UCF | 84 | 84 | 0 | 0 | 0 | 1,012 |
| REMI | 63 | 63 | 0 | 0 | 0 | 207 |
| ఎల్ఎస్ఎఫ్ | 32 | 32 | 0 | 0 | 0 | 819 |
| ట్రాక్టర్ | 12 | 12 | 0 | 0 | 0 | 233 |
| BMALL | 8 | 8 | 0 | 0 | 0 | 114 |
| ELECTRIC BICYCLE | 2 | 2 | 0 | 0 | 0 | 8 |
| LCF | 2 | 2 | 0 | 0 | 0 | 21 |
| TEST10 | 2 | 2 | 0 | 0 | 0 | 4 |
| TYRES | 2 | 2 | 0 | 0 | 0 | 4 |
| ఎల్‌సిఎఫ్ | 2 | 2 | 0 | 0 | 0 | 24 |
| FARM EQUIPMENT | 1 | 1 | 0 | 0 | 0 | 7 |
| NCF-UCF | 1 | 1 | 0 | 0 | 0 | 7 |
| NEW-CARS | 1 | 1 | 0 | 0 | 0 | 7 |
| PLP | 1 | 1 | 0 | 0 | 0 | 6 |
| SOLAR | 1 | 1 | 0 | 0 | 0 | 6 |
| WHEELS | 1 | 1 | 0 | 0 | 0 | 7 |
| ఆర్ఇఎంఐ | 1 | 1 | 0 | 0 | 0 | 16 |
| బిమాల్ | 1 | 1 | 0 | 0 | 0 | 27 |

## 4. Language Summary (Processed Source Pages)

| Language | Source Pages Processed | Completed | Failed | Blocked | Timed Out | Relationships Extracted |
|---|---:|---:|---:|---:|---:|---:|
| EN | 73,670 | 73,510 | 19 | 0 | 141 | 2,776,836 |
| HI | 18,904 | 18,761 | 3 | 0 | 140 | 724,245 |
| TE | 626 | 626 | 0 | 0 | 0 | 26,640 |

## 5. Page-Type Summary (Processed Source Pages)

| Page Type | Source Pages Processed | Completed | Failed | Blocked | Timed Out | Relationships Extracted |
|---|---:|---:|---:|---:|---:|---:|
| PDP | 67,501 | 67,501 | 0 | 0 | 0 | 2,707,318 |
| LSF-PDP | 16,257 | 16,257 | 0 | 0 | 0 | 557,301 |
| PLP | 2,756 | 2,756 | 0 | 0 | 0 | 115,094 |
| 4W-PDP | 2,704 | 2,704 | 0 | 0 | 0 | 114,251 |
| REMI-PDP | 1,399 | 1,399 | 0 | 0 | 0 | 3,496 |
| 2W-PDP | 1,190 | 1,190 | 0 | 0 | 0 | 11,266 |
| PDP-DYNAMIC | 740 | 740 | 0 | 0 | 0 | 17,804 |
| NEEDS_REVIEW | 312 | 9 | 22 | 0 | 281 | 46 |
| TRACTOR-PDP | 174 | 174 | 0 | 0 | 0 | 348 |
| LCF-PDP | 119 | 119 | 0 | 0 | 0 | 253 |
| BUYING_GUIDE_B2B | 14 | 14 | 0 | 0 | 0 | 140 |
| B2B-MCLP | 10 | 10 | 0 | 0 | 0 | 179 |
| BUYING_GUIDE_LSF | 3 | 3 | 0 | 0 | 0 | 31 |
| LSF-MCLP | 3 | 3 | 0 | 0 | 0 | 52 |
| BUYING_GUIDE_REMI | 2 | 2 | 0 | 0 | 0 | 16 |
| MCLP | 2 | 2 | 0 | 0 | 0 | 19 |
| 2W-MCLP | 1 | 1 | 0 | 0 | 0 | 9 |
| 4W-MCLP | 1 | 1 | 0 | 0 | 0 | 7 |
| BRAND_LANDING | 1 | 1 | 0 | 0 | 0 | 9 |
| BUYING_GUIDE | 1 | 1 | 0 | 0 | 0 | 3 |
| BUYING_GUIDE_2W | 1 | 1 | 0 | 0 | 0 | 7 |
| BUYING_GUIDE_4W | 1 | 1 | 0 | 0 | 0 | 7 |
| CAMPAIGN_LANDING | 1 | 1 | 0 | 0 | 0 | 10 |
| CLP | 1 | 1 | 0 | 0 | 0 | 6 |
| DSF | 1 | 1 | 0 | 0 | 0 | 5 |
| EMI_LANDING | 1 | 1 | 0 | 0 | 0 | 9 |
| Home | 1 | 1 | 0 | 0 | 0 | 2 |
| OFFER_LANDING | 1 | 1 | 0 | 0 | 0 | 19 |
| SAVING_CALCULATOR | 1 | 1 | 0 | 0 | 0 | 8 |
| SEARCH | 1 | 1 | 0 | 0 | 0 | 6 |

## 6. Discovered URL Scope & Element Type Breakdown

| Scope | Relationship Count |
|---|---:|
| INTERNAL | 3,434,814 |
| EXTERNAL | 92,907 |

| Element Type | Relationship Count |
|---|---:|
| ANCHOR | 3,341,928 |
| IFRAME | 92,897 |
| CANONICAL | 92,896 |

## 7. Generated Artifacts in This Snapshot Folder

- `BAJAJ_MALL_ON_PAGE_AUDIT_SNAPSHOT_REPORT.md` — This executive & reconciliation report
- `BAJAJ_MALL_ON_PAGE_AUDIT_AS_OF_NOW_SUMMARY.xlsx` — Multi-sheet consolidated summary & issues Excel workbook
- `sitemap_progress_summary.csv` — Progress across all 12 Bajaj Mall sitemaps
- `business_summary.csv` / `language_summary.csv` / `page_type_summary.csv` / `destination_status_summary.csv`
- `source_page_issues.csv` — All failed / timed-out / non-200 source pages observed so far
- `broken_and_timeout_urls.csv` — All broken / timed-out / error link relationships observed so far
- `redirect_urls.csv` — All redirected link relationships observed so far
- `external_urls_unique.csv` — All unique external URLs discovered on Bajaj Mall pages
- `snapshot_reconciliation.json` — Machine-readable reconciliation & validation cache reuse proof
