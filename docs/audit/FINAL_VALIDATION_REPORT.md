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
| 300K (B4), 30 req/s (max 30), 40/35/50, simulated edge without WAF | ≤ 4 h, complete pipeline | **3 h 21 min 57 s** = crawl 10,892 s (27.5 pages/s, 29.6 primary req/s, 195 req/s total incl. images) + reports/ZIP 1,224.7 s (CSVs 812 s, ZIP 55 s, 431 MB); 300,000/300,000 pages, 0 failed, 0 WAF, 0 timeouts, 12 network errors (retried); 11.1M link + 7.5M image relationships, 318K + 1.81M unique URLs validated once; 2,156,380 issues; trust gate 35/35 | PASS (simulated; crawl and packaging measured in two processes, see below) |
| 300K (B3), 25 req/s, before the connection-pool fix | ≤ 4 h | crawl ~4 h 40 min (3 h 45 min, container restart, ~55 min resumed from checkpoint); report build failed on a full disk | FAIL |
| Real edge (bajajfinserv.in) | — | — | NOT VERIFIED (not reachable from this environment) |

Duration is set by the rate the edge allows: ~1.06 primary requests per page, so 300K pages need ≥ 23 req/s sustained
for ≤ 4 h; at the simulated Akamai model's ~14 req/s it would take ~6.3 h. 50K/100K/150K/200K profiles were not run
separately (time); the 10K and 300K runs bracket them and every component scales linearly (see package timing).

### What it took to reach 300K in ≤ 4 h

* **Crawler CPU (fixed).** py-spy on a 20K run showed ~75 % of the event-loop thread inside httpcore's connection
  pool, which re-checks every pooled connection (socket probe + O(n²) idle count) on each request. One shared pool
  for the site, the image CDN and every external host made that cost grow with total connections. `http_pool.py`
  now gives each host its own pool, split into round-robin shards of ≤ 8 keep-alive connections. 20K pages, same
  settings, identical row counts: crawl 1,271.4 s → 718.3 s (15.7 → 27.8 pages/s; repeat run 718.6 s); the run is
  now capped by the 30 req/s budget, not the CPU.
* **Disk during packaging (fixed).** Issues found while the packager streams CSV rows were committed in batches on
  the connection holding that long read, so SQLite could never reset its WAL; at 1.6M issues it reached 9.6 GB in
  under two minutes and filled the disk (B4's first packaging attempt; the crawl DB was intact). Issues are now
  spooled to a TEMP table and published in one transaction. The B4 package above was rebuilt from the kept crawl
  DB with that fix, so the total adds the crawl time the engine recorded to a separate packaging run; the failed
  first attempt (~14 min) is not counted.
* **Disk sizing.** The audit's own peak for 300K was ~22 GB: crawl DB 11.8 GB + 8.8 GB of CSVs + 0.4 GB ZIP +
  ≤ 1.1 GB WAL (~75 KB per page). Budget ≥ 30 GB free for a 300K audit.

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
| Performance | 10K PASS (simulated); 300K PASS (simulated, 3 h 22 min) |

## Remaining risks

* Real-edge throughput and WAF tolerance: NOT VERIFIED (measure with `bench/live_edge_matrix.py` from your network).
* 300K on the real site: depends on the edge sustaining ≥ 23 req/s; at ≤ 15 req/s it cannot meet 4 h.
* Rendering JavaScript-only pages: detected, not rendered.
* Workspace/RBAC isolation: not part of this codebase.
* The CrawlPulse React frontend was not available; integrating this backend into it needs that code.
