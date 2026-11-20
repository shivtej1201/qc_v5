# Backend audit and gap analysis (CrawlPulse revamp, phases 1–2)

Scope: the backend in `shivtej1201/qc_v5` (`claude/onpage-perf-url-image-audit` @ `753f100`). The CrawlPulse
frontend (Watermelon UI, GSAP, React) is **not in the repository** and is out of scope until it is pushed;
everything below is backend, data path, reports, API and tests.

## 1. What exists

| Area | Implementation | State |
|---|---|---|
| On-page crawl pipeline | `on_page_audit_engine.py`: bounded queues fetch → parse (1 parse/page, thread pool) → URL + image validators → single batched SQLite writer; shared primary rate budget, per-host budgets, adaptive WAF cooldown, three-level dedup, checkpoint/resume | Working, measured (10K in 13.1 min @ 14 req/s, simulated edge) |
| Sitemap | `sitemap_extractor.py` (SSRF per hop, robots, gzip, nested indexes, limits, 403 diagnostics) + `sitemap_config.py` (domain/robots discovery, selection, profiles) | Working |
| Normalization | `url_normalizer.URLNormalizer.normalize` (cache key) used by engine, sitemap, packager | One duplicate: `remediation_engine.normalize_remediation_url` |
| Extraction | `html_parser.Parser`: `<a>`, `<area>`, `<iframe>`, canonical, hreflang, prev/next/amphtml/alternate, images (img src/srcset/data-*, picture source, og/twitter image), SEO signals | `<script src>`, stylesheet/icon/preload `<link>`, `<video>/<audio>/<source>` media, `<form action>`, `og:url`, JSON-LD URLs **not extracted** |
| HTTP validation | GET, manual redirects (chain, loop, broken redirect), SSRF per hop, robots, WAF detection; buckets 2xx/3xx/4xx/5xx/Timeout/DNS-Network/SSL/Robots/WAF; error_type DNS / Connection / SSL / Timeout | Retry is bounded but **linear without jitter**, ignores `Retry-After` |
| Issues | `on_page_issue_rules.py` + `seo_audit.py`, ~100 types with severity + action; computed at report time into `12_issues.csv` | **Not persisted** (CSV only), no lifecycle status, issue explorer scans the CSV per request |
| Score | `health_score` = share of pages without HIGH issues; `seo_overview.score` = average pass rate | Not the 35/25/20/10/10 weighted model; not stored; no formula version |
| Remediation | `remediation_engine.py` + `remediations` table + `/api/jobs/{id}/remediations*` (OPEN/IN_PROGRESS/RESOLVED/DISMISSED/NEEDS_REVIEW) | Reads **classic** link-audit tables only — on-page audits never reach it |
| Jobs | Worker process per audit; states QUEUED/RUNNING/STOPPING/STOPPED/PARTIAL/COMPLETED/FAILED/INTERRUPTED; stop = graceful + resumable; resume from checkpoint | No CANCELLED, no PAUSED naming, no lifecycle history; throughput/ETA computed in the browser |
| Reports | 16 streamed CSVs + ZIP (deflate, verified), 31-check trust gate | No manifest (row count, size, checksum per file) |
| Explorers | Issues (CSV scan), worst pages | No server-side URL / link / image explorers |
| BrowserPool | `browser.py` (Playwright, `BROWSER_CONCURRENCY=2`) used by classic checker | `get_page()` leaks a semaphore slot when no page is available; no lease timeout; not used by the on-page engine (no hydration detection) |
| DB | SQLite WAL, one writer per job, batched transactions, indexes on relationship/cache keys | OK; new explorer queries need indexes |
| Security | SSRF (DNS-aware, every hop), robots, CSV formula guard, id/file-name validation, reverse-proxy auth middleware (`AUTH_REQUIRED` / `PROXY_SECRET_VALUE`) | No workspace/project isolation model exists (single-tenant) |
| Tests | 41 test files; on-page suites 8 + 3 + 1 (the "12/12" baseline) + 8 SEO/sitemap | 17 environment-only failures (no public DNS / async scripts) |

## 2. Gaps (classified)

| # | Gap | Class | Fix in this phase |
|---|---|---|---|
| G1 | Issues exist only in a CSV: no persistence, no dedup constraint, no lifecycle, explorer re-reads the whole CSV per request (O(issues) per page view; slow at 150K pages) | CRITICAL | `on_page_issues` table, UNIQUE (job, page, rule, subject), statuses carried across audits of the same project, indexed paging |
| G2 | Many on-page URLs never extracted (scripts, stylesheets, icons/preload/manifest, media, forms, og:url, JSON-LD) → "all URLs" requirement unmet | HIGH | Parser extracts them as typed categories; validated with the same dedup (forms recorded, not requested: GET on a POST endpoint is not a valid check) |
| G3 | On-page audits not connected to Remediation | HIGH | Generate remediations from persisted on-page issues (grouped per rule + target) into the existing table/APIs; add VERIFIED status |
| G4 | No weighted deterministic health score (35/25/20/10/10), not stored | HIGH | `formula_version`-tagged score computed from persisted issues, stored per job |
| G5 | Job lifecycle lacks CANCELLED / PAUSED / lifecycle history; throughput/ETA only in the browser | HIGH | pause / cancel APIs, lifecycle field + event history, backend throughput + ETA |
| G6 | No report manifest (rows, bytes, checksum) | MEDIUM | `00_manifest.csv` + manifest in result, checksums verified after ZIP |
| G7 | No server-side URL / link / image explorers for large audits | MEDIUM | Paged, filtered, sorted endpoints over indexed tables |
| G8 | Retry backoff linear, no jitter, ignores Retry-After | MEDIUM | Exponential backoff + jitter, Retry-After honored (capped), no retry on 4xx except 429 |
| G9 | Link/image/URL report columns incomplete vs required fields (rel/nofollow, business, page_type, language in 03; image format, lazy flag in 05; canonical/robots/title/description/H1/lang/word count/link+image counts in 02) | MEDIUM | Append columns (existing column order unchanged) |
| G10 | Integrity equation (sitemap URLs = processed + failed + skipped + queued) not reported; duplicate/invalid input lines not counted | MEDIUM | Inventory accounting in summary + trust gate |
| G11 | BrowserPool semaphore leak, no lease timeout; no JS-rendering detection | MEDIUM | Fix leak + lease timeout; flag `JS_RENDERING_SUSPECTED` pages (HTTP-first; rendering stays opt-in) |
| G12 | Duplicate normalizer in remediation engine | LOW | Delegate to `URLNormalizer` semantics where keys are compared |
| G13 | Sitemap diagnostics lack content-type and response time | LOW | Recorded by `sitemap_config` |
| G14 | Fine-grained HTTP class (200, 301, 302, 404, 410, 429, 503, DNS, SSL…) only as code + bucket | LOW | `status_class` column in URL/link/image reports |
| G15 | No structured (JSON) stage log for observability | LOW | `events.jsonl` per audit: stage, job_id, request/url on errors |
| G16 | 150K preset (16/18 req/s, 35/35/50) is not in the backend and has never been measured | HIGH (unverified claim) | Add `enterprise_150k` preset; measure on the simulated edge |
| G17 | Workspace / RBAC isolation | n/a | Not present in this codebase (single-tenant, proxy auth). Reported as NOT VERIFIED rather than invented |

Order of work follows the story: correctness and data integrity (G1–G3, G9, G10), lifecycle and reports (G4–G7),
reliability (G8, G11), then performance validation (G16) and security regression.
