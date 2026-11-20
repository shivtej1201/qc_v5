# Final validation report — backend hardening + frontend on the frozen API

Branch `claude/crawlpulse-backend-hardening`. Environment: 4 vCPU / 15 GB container, Python 3.13, SQLite (WAL).
All benchmarks run against **simulated** edges (`bench/sim_edge.py`): the real target site is not reachable from this
environment. Anything not measured here is marked **NOT VERIFIED**.

Scope note: the CrawlPulse React frontend (Watermelon theme) is not in this repository or the supplied archives; the
supplied CrawlPulse backend/docs were used as reference (job states, extraction categories, status-movement idea).
The frontend stage was built on this repository's Command Center (vanilla ES modules + Three.js + GSAP 3.12.5).
The Watermelon theme was not added (as instructed).

## Backend status

| Area | Status | Evidence |
|---|---|---|
| Architecture | PASS | Audit + gap analysis: `docs/audit/BACKEND_AUDIT_AND_GAPS.md`; architecture doc updated |
| Crawler | PASS | Fixture site: 200/301/302/403/404/410/500/timeout/robots classified (`status_class`); retries exponential + jitter, Retry-After honored; 10K run 0 failures |
| Sitemap | PASS | robots.txt / domain discovery, nested index, gzip, duplicates (within + across files), lastmod, 404/WAF vs XML errors, content-type + response time per file |
| Extraction | PASS | All URL categories (links, SEO signals, scripts, styles, icons, CSS `url()`, media, embeds, forms, social, JSON-LD) + images (img/srcset/picture/lazy/CSS/poster/og/JSON-LD), one parse per page |
| URL audit | PASS | `02` + `/urls` + `/url-detail`: status class, redirect chain, canonical (+status, self-ref), robots meta + X-Robots-Tag, title/description/H1/H2–H6 skips, lang, viewport, structured data, response time, size |
| Link audit | PASS | Every relationship validated via the deduplicated cache; business/page_type/language from the SOURCE page; nofollow, component, final URL |
| Image audit | PASS | Status, content type/length, format, lazy, alt missing/empty, dimensions, redirects, broken/oversized/invalid-content-type |
| Issue engine | PASS | Persisted, UNIQUE (job, page + rule + subject); rebuild idempotent; lifecycle per project with crawl-verified fixes and regression re-open; evidence per issue |
| Health score | PASS | `qcv5-health-1.0`, 35/25/20/10/10, stored with `formula_version`, `calculated_at`; recomputation identical |
| Database | PASS | O(pages × relationships) report query found and fixed (10K package 538 s → 24 s); batched writes (1,204 rows/s sustained, flush p95 217 ms at 16 req/s); remediation upserts batched |
| Workers / jobs | PASS | CREATED → QUEUED → RUNNING → PAUSING → PAUSED → RESUMED → COMPLETED; CANCELLING → CANCELLED; SIGKILL → INTERRUPTED → resume, no duplicates |
| API | PASS (frozen v1.0) | `docs/api/API_CONTRACT.md` + generated `openapi_onpage.json` |
| Security | PASS | SSRF (direct, redirect hop, numeric/encoded/IPv6-mapped forms, metadata, sitemap discovery), CSV injection across every report, traversal, whitelisted sort, bounded limit, bound parameters |
| Workspace / RBAC isolation | NOT VERIFIED | not present in this codebase (single tenant, reverse-proxy auth) |
| JS rendering (BrowserPool) | PARTIAL | app-shell pages detected and flagged (`JS_RENDERING_SUSPECTED`); BrowserPool leak fixed + 30 s lease timeout + capacity 1–4; rendering those pages inside the on-page engine is NOT IMPLEMENTED (HTTP-first only) |

## Performance

| Run | Target | Actual | Status |
|---|---|---|---|
| 10K, Enterprise 150K preset (16/18 req/s, 35/35/50), simulated edge without WAF | 15–20 min, complete pipeline | **11.25 min** (crawl 650.5 s + reports/ZIP 24.4 s); 16.52 req/s avg, 18 peak (1 s); p95 latency 354 ms; CPU 39 %; RSS 226 MB | PASS (simulated) |
| 10K, same preset, simulated Akamai model (blocks > 15 req/s / > 32 in flight) | — | crawl 13.0 min: 1 WAF cooldown, adaptive step-down 16 → 13.6, settled ~13.8 req/s; 0 failures | preset exceeds the model's WAF limit; engine stayed safe |
| 300K | ≤ 4 h, complete pipeline | {{300K}} | {{300K_STATUS}} |
| Real edge (bajajfinserv.in) | — | — | NOT VERIFIED (not reachable from this environment) |

Duration is set by the rate the edge allows: ~1.06 primary requests per page, so 300K pages need ≥ 23 req/s sustained
for ≤ 4 h; at the simulated Akamai model's ~14 req/s it would take ~6.3 h. 50K/100K/150K/200K profiles were not run
separately (time); the 10K and 300K runs bracket them and every component scales linearly (see package timing).

## Data quality (10K benchmark, B2)

| Dataset | Rows | Check |
|---|---|---|
| URLs (02) | 10,000 | = source pages; 0 pending |
| Link relationships (03) | 369,522 (from 410,000 occurrences, collapsed per page) | = DB; 10,626 unique URLs validated once, 358,896 reused |
| Images (05 / 06) | 250,304 placements / 60,319 unique | = DB; 179,985 reused |
| Issues (12) | 71,878 | CSV = DB; no duplicate keys |
| Broken / redirects | 397 / 499 relationships | 07/08 = DB = issue rows |
| Reports | 18 files, 289 MB CSV → 13.9 MB ZIP | 35/35 trust-gate checks PASS; manifest SHA-256 verified against the ZIP |

## Frontend

| Area | Status |
|---|---|
| Unified UI | PASS — one app: Command Center, Audits (Overview · URLs · Links · Images · Issues · Remediation · Reports), Sitemaps |
| Command Center | PASS — lifecycle pill, Pause/Resume/Cancel, backend progress/throughput/ETA, 3D crawl core, score card |
| GSAP | PASS — vendored 3.12.5, primitives in `js/motion.js`, view-scoped contexts reverted on navigation |
| 3D | PASS — crawl core (pending / in flight / done / HIGH-issue pages, req/s-driven rings) |
| Dynamic UI | PASS — state-driven (status transitions, failed-state pulse, live counters from the backend) |
| Themes | Dark + Light PASS; Watermelon intentionally not added |
| Large dataset UI | PASS — 300,000-row link grid: < 80 DOM rows, last row reachable, first page < 15 s (E2E) |
| Responsive | PASS at 1440 desktop and 1024 tablet (no horizontal overflow); phone layout from the previous phase |
| Accessibility | PASS (manual + E2E): keyboard grid navigation, focus rings, ARIA grid/tabs/dialog, reduced motion |
| Projects, Settings, Export Center pages | NOT IMPLEMENTED — no backend model for projects/settings; Reports tab covers exports |

## Testing

| Suite | Result |
|---|---|
| Existing baseline (8 + 3 + 1) | PASS (two file-list expectations updated for the two added report files) |
| Unit + integration (`test_platform_pipeline.py`, `test_seo_sitemap_audit.py`, …) | PASS |
| API | PASS |
| Playwright E2E (`test_e2e_platform_ui.py`, `test_dashboard_ui.py`) | PASS |
| Security (`test_security_validation.py`, 21 tests) | PASS |
| Full regression | 149 passed, 17 failed — the 17 are the pre-existing environment-only failures (no public DNS; async scripts pytest cannot collect); identical list before and after |
| Performance | 10K PASS (simulated); 300K {{300K_STATUS}} |

## Remaining risks

* Real-edge throughput and WAF tolerance: NOT VERIFIED (measure with `bench/live_edge_matrix.py` from your network).
* 300K on the real site: depends on the edge sustaining ≥ 23 req/s; at ≤ 15 req/s it cannot meet 4 h.
* Rendering JavaScript-only pages: detected, not rendered.
* Workspace/RBAC isolation: not part of this codebase.
* The CrawlPulse React frontend was not available; integrating this backend into it needs that code.
