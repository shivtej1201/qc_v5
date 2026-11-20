# QC V5 — System Architecture

Audience: engineers, tech leads, SRE. Companion docs: [design system](../design/DESIGN_SYSTEM.md),
[10K performance report](../performance/ON_PAGE_10K_URL_IMAGE_AUDIT.md).

## 1. What the system does

QC V5 audits websites at scale:

| Capability | Entry point | Output |
|---|---|---|
| **On-page URL + image + SEO audit** (sitemap pages → every link, canonical, hreflang, iframe and image on them; SEO / technical-SEO facts per page; sitemap health) | Command Center (`/dashboard/`), `qcv5/cli/run_on_page_audit.py`, `/api/on-page/*` | 16 CSVs + ZIP, issue reports, live metrics |
| **Bulk URL status check** (up to 125K+ URLs from a sheet) | Classic console (`/`), `/api/upload` | one result row per submitted URL |
| Link / image / API-response audits (Playwright-based) | Classic console | Excel reports |

**Performance target:** 10,000 source pages audited, validated and reported in 15–20 min. Measured: 13.1 min at 14 req/s
on a simulated Akamai edge (see §7 for what governs this on the real site).

## 2. System context

```
           ┌──────────────┐      HTTPS (GET, manual redirects, SSRF-checked per hop)
 Analyst ─▶│  Browser UI  │                 ┌────────────────────────────┐
           │ /dashboard/  │                 │ Target site (Akamai edge)  │
           │ / (classic)  │                 │ www.<primary-domain>        │◀─┐
           └──────┬───────┘                 └────────────────────────────┘  │
                  │ JSON / CSV / ZIP                                         │ ONE shared
           ┌──────▼──────────────────────────────┐   spawn / stop-file      │ request budget
           │ FastAPI app (qcv5/api/server.py)             │────────────────┐         │ per primary domain
           │  · classic job APIs + batch daemon  │                ▼         │
           │  · /api/on-page/* (onpage_service)  │   ┌─────────────────────┴──┐   ┌──────────────┐
           └──────┬──────────────────────────────┘   │ Audit worker process   │──▶│ CDN / external│
                  │ aiosqlite (WAL)                  │ qcv5.cli.run_on_page_… │   │ hosts (own    │
           ┌──────▼────────────────────────────────┐ │ OnPageAuditEngine       │   │ budgets)      │
           │ SQLite (WAL) audit_cache.db           │◀┤ (sqlite3, single writer)│   └──────────────┘
           └───────────────────────────────────────┘ └─────────┬──────────────┘
                                                               ▼
                                          reports/onpage/<job>/QC_V5_ONPAGE_AUDIT{.zip,/}
```

Design decisions:

* **Audits run as isolated worker processes**, not inside the API. A crash or long crawl never blocks the API; the
  API restarts without killing running audits (they are re-attached by PID file).
* **One active crawl per primary domain** (HTTP 409 otherwise): two crawls would double the request rate the WAF sees.
* **SQLite in WAL mode** stays the only database: the worker is the single writer for its job; the API reads.
* **No new infrastructure** (no Redis/Celery/queues): bounded asyncio queues inside the worker give the pipeline.

## 3. Components

| Module | Responsibility |
|---|---|
| `qcv5/api/server.py` | FastAPI app: classic APIs, `/api/on-page/*` dashboard API, static UIs (`/` classic, `/dashboard/` new) |
| `qcv5/services/onpage_service.py` | Audit lifecycle (start/stop/resume), presets, duration model, issue browsing, smart insights, health score |
| `qcv5/cli/run_on_page_audit.py` | Worker CLI: inventory → engine → CSV/ZIP package; graceful stop (SIGTERM or stop-file); result JSON |
| `qcv5/crawler/on_page_audit_engine.py` | Crawl + validation pipeline, rate control, WAF handling, registries, exact counters, checkpointing |
| `qcv5/crawler/html_parser.py` | Single-pass HTML parser: links, canonical, hreflang, meta, area, iframe, images (srcset, picture, lazy, og/twitter) |
| `qcv5/audit/on_page_issue_rules.py` | ~100 issue types → severity, detail, recommended action (pure functions) |
| `qcv5/audit/seo_audit.py` | SEO page facts from the single parse (+ `X-Robots-Tag`), single-page SEO rules, hreflang reciprocity, lastmod rules |
| `qcv5/crawler/sitemap_config.py` | Sitemap discovery (sitemap / robots.txt / domain), import snapshots, selection filters, saved profiles; uses `qcv5/crawler/sitemap_extractor.py` (SSRF per hop, robots, gzip, nested indexes) |
| `qcv5/reports/on_page_csv_packager.py` | Streaming reports 00–17 (incl. cross-page SEO checks, per-sitemap health, input exceptions, manifest), ZIP with SHA-256 verification, trust gate |
| `qcv5/audit/issue_store.py` | Persisted issues (unique per page + rule + subject), project-level issue lifecycle, deterministic health score |
| `qcv5/audit/remediation_engine.py` | Remediation items from classic audits **and** on-page issues (grouped per destination / page / site-wide) |
| `qcv5/audit/on_page_schema.py` | Dependency-free image-audit DDL shared by API and worker |
| `qcv5/core/request_errors.py` | Type-based transport error classification shared by checker + engine |
| `qcv5/crawler/url_normalizer.py` | Normalization (cache key), syntax validation, SSRF checks (DNS/private-IP aware) |
| `qcv5/services/job_manager.py`, `qcv5/services/daemon.py`, `qcv5/crawler/checker.py`, `qcv5/core/db.py` | Classic bulk URL-status pipeline (per-URL ledger, resumable batches) |
| `frontend/dashboard/` | Command Center SPA (vanilla ES modules + vendored Three.js) |

## 4. The on-page audit pipeline (inside one worker)

```
 sitemap URLs ─▶ [fetch_q] ─▶ source fetchers ─▶ [parse_q] ─▶ parse pool (1 parse/page) ─▶ label + dedup
   (SQLite         (bounded)   GET + HTML,         (bounded)   links + images + meta          │
    inventory)                 SSRF/robots/WAF                                                ├─▶ [dest_q]  ─▶ URL validators   ─┐
                                                                                              ├─▶ [img_q]   ─▶ image validators ─┤
                                                                                              ▼                                  ▼
                                                                                         [write_q] ─▶ single batched SQLite writer
                                                                                                       (1 txn/flush + O(1) progress row)
```

* **Shared primary budget**: every request to the primary domain (pages, link checks, images, redirect hops,
  robots.txt) reserves a slot on one strict-spacing clock. `--host-budget` gives other hosts (e.g. image CDN) their own.
* **Adaptive control**: +0.25 req/s per 2,000 healthy responses up to `max_rps`; 5 consecutive WAF responses → 15%
  step-down + cooldown + probe; budgets are independent per host.
* **All URLs on the page** are extracted and typed: navigation (`<a>`, `<area>`, `<iframe>`, `<embed>`, meta refresh),
  SEO signals (canonical, hreflang, prev/next/amphtml, `og:url`, JSON-LD URLs), resources (`<script>`, stylesheets,
  icons/preload/manifest, CSS `url()`, `<video>`/`<audio>`/`<source>`/`<track>`) and forms (recorded, never requested:
  a GET on a form endpoint is not a valid check). Images also come from CSS backgrounds, video posters and JSON-LD.
* **Retries**: transient failures only (timeouts, connection errors, 429, 502–504), exponential backoff with jitter
  (0.5 s, 1 s, 2 s … ≤ 8 s), `Retry-After` honored up to 30 s; 4xx, robots and SSRF blocks are never retried.
* **Three-level dedup** (same page, across pages, in flight) for URLs and images; a sitemap page that is also a link
  target is satisfied by its own fetch (`SOURCE_PAGE_REUSED`). Only authoritative non-WAF HTTP results are reused.
* **Exactness**: counters are mirrored from what each transaction writes; full aggregation only at start,
  checkpoints and the end, where mirror == DB is verified.
* **Checkpoint/resume**: everything a page produces commits in one transaction with the page's state; on resume
  PENDING pages and validations continue, finished work is reused.

## 5. Data model (SQLite)

| Table | Grain | Key |
|---|---|---|
| `on_page_audit_jobs` | one row per audit; live counters + `live_metrics` JSON | `job_id` |
| `on_page_source_pages` | one row per sitemap page (status, redirects, title, bytes, truncation, classification) | `(job_id, source_page_url)` |
| `on_page_link_relationships` | one row per page → URL (collapsed per page, `occurrence_count`) | `(job_id, source_page_url, normalized_found_url)` |
| `on_page_validation_cache` | one row per unique URL per job (validation result, `seeded_by`) | `(job_id, normalized_url)` |
| `on_page_image_relationships` | one row per page → image → alt text | `(job_id, source_page_url, normalized_image_url, alt_present, alt_text)` |
| `on_page_image_validation_cache` | one row per unique image per job (status, content-type/length, image issue) | `(job_id, normalized_url)` |
| `on_page_seo_pages` | one row per parsed 2xx HTML sitemap page: SEO facts (written in the page's transaction) | `(job_id, source_page_url)` |
| `on_page_sitemap_entries` | sitemap provenance per page URL (sitemap, lastmod, changefreq, priority) | `(job_id, normalized_url)` |
| `on_page_sitemap_sources` | every sitemap file fetched for the audit (kind, status, URLs, duplicates, size, selected) | `(job_id, sitemap_url)` |
| `on_page_input_exceptions` | input lines that are not pages (invalid, exact or normalized duplicate) | `(job_id, line_no)` |
| `on_page_issues` | one row per issue: rule, severity, category, score dimension, page, subject URL, evidence | `(job_id, issue_key)` UNIQUE |
| `on_page_issue_status` | lifecycle per project (primary domain): OPEN / IN_PROGRESS / VERIFIED / RESOLVED / DISMISSED | `(project_id, issue_key)` |
| `on_page_scores` | health score per audit + the five dimension scores, `formula_version`, weights | `job_id` |
| `job_url_items` | bulk URL-status: one row per submitted URL | `(job_id, row_no)` |

## 6. Dashboard API (`/api/on-page/…`)

| Method & path | Purpose |
|---|---|
| `GET presets` · `GET estimate?pages=&rps=&images_on_primary=` | Speed presets; predicted duration from the measured model |
| `GET audits` · `POST audits` (JSON urls / sitemap import) · `POST audits/upload` (.txt/.csv/.xlsx) | List / start |
| `GET audits/{id}` | State, live stats + `live_metrics`, health score, worker log tail |
| `POST audits/{id}/stop` · `POST audits/{id}/resume` | Graceful stop (resumable) / resume from checkpoint |
| `GET audits/{id}/insights` | Smart insights (projection vs target, budget share, WAF, fix-first, biggest wins, reconciliation) |
| `GET audits/{id}/issues/overview` · `GET audits/{id}/issues?severity=&issue_type=&category=&q=&offset=&limit=` | Issue totals + filtered issue rows |
| `GET audits/{id}/pages` · `GET audits/{id}/package` · `GET audits/{id}/files/{NN_name.csv}` | Worst pages, ZIP, single CSV |
| `GET audits/{id}/seo` | SEO score, coverage per check, SEO issues by area, sitemap health rows |
| `POST sitemaps/discover` `{inputs, max_urls}` | Resolve sitemaps (direct, robots.txt `Sitemap:` lines, or `/sitemap.xml`), fetch + parse the tree → import snapshot (`smi_…`) |
| `GET sitemaps/{import_id}` · `POST sitemaps/{import_id}/preview` | Snapshot summary · selection preview (count, what was left out and why, sample, duration estimates) |
| `POST audits` with `sitemap: {import_id, selected_sitemaps, include, exclude, max_pages}` | Start an audit from a sitemap selection (sitemap provenance flows into the reports) |
| `GET/POST sitemap-profiles` · `PUT/DELETE sitemap-profiles/{id}` · `POST sitemap-profiles/{id}/run` | Saved sitemap configurations; *run* re-discovers and launches |

| `POST audits/{id}/pause` · `POST audits/{id}/cancel` · `GET audits/{id}/progress` | Lifecycle control; progress (total, processed, remaining, success, failed, blocked, links, images, issues, current + average throughput, elapsed, ETA) — all from the backend |
| `GET audits/{id}/score` | Health score, five dimensions, `formula_version`, weights, `calculated_at` |
| `PATCH audits/{id}/issues/{issue_key}/status` `{status, note}` | Issue lifecycle (persists per project across audits) |
| `GET audits/{id}/urls` · `links` · `images` | Server-side explorers: filters, whitelisted sort, `limit` ≤ 500 + `offset`, total `matched` |
| `GET audits/{id}/issues?...&status=&page_url=&sort=&order=` | Issues from the database (indexed) with lifecycle status |
| `POST /api/jobs/{id}/remediations/generate` (existing) | Also turns on-page issues into remediation items |

Errors: 400 invalid input/ids/sort/limit, 404 unknown audit or report not built, 409 domain busy / already running /
cancelled audit resumed.

## 6b. Job lifecycle

```
CREATED -> QUEUED -> RUNNING -> PAUSING -> PAUSED -> (resume) RESUMED/RUNNING -> COMPLETED | PARTIAL | FAILED
                        \-> CANCELLING -> CANCELLED (not resumable; completed work stays in the DB)
            worker crash -> INTERRUPTED (resume continues from the checkpoint)
```

The state is derived from the worker process, its `result.json` and the audit metadata, and every transition is
appended to an event history (`events` in `GET audits/{id}`). Pause and cancel are graceful: in-flight requests finish
and are committed. Recovery: completed pages and validations are never re-requested; PENDING pages and validations
continue on resume (verified with a SIGKILL in `tests/test_platform_pipeline.py`).

## 6c. Issues, lifecycle and score

* Issues are written to `on_page_issues` with `INSERT OR IGNORE` on `issue_key = hash(page | rule | subject)`, then
  12/13/14 are streamed from the table, so CSV and DB always agree and rebuilding a package never duplicates issues.
* Lifecycle is per project (primary domain): a status survives re-audits. After a complete audit, an OPEN or
  IN_PROGRESS issue whose page was audited and that is no longer detected becomes VERIFIED; a VERIFIED or RESOLVED
  issue that is detected again is re-opened with a "Regression" note. Only an audit other than the one the status was
  set in can move it.
* Health score `qcv5-health-1.0`: each audited page gets a penalty per dimension = min(1, Σ severity weights)
  (HIGH 1.0, MEDIUM 0.5, LOW 0.2, INFO 0); dimension score = 100 × (1 − mean penalty); overall = 35 % technical +
  25 % links + 20 % content + 10 % performance + 10 % security. Same rows → same score.

## 6d. Observability

`<out-dir>/events.jsonl`: one JSON object per stage (`inventory`, `sitemap`, `crawl`, `export`) with job id, project
id, counts, rates and durations; failed trust-gate checks are listed in the `export` event. Per-URL errors are stored
on the URL's row (`error_type`, `error_message`, `status_class`) and appear in 03/04/05/06.

## 6a. SEO and sitemap audit

* **Collected during the single parse**: the parser records title(s), meta description(s), robots meta, viewport,
  charset, `lang`, meta refresh, headings (H1 text, skipped levels), JSON-LD blocks, Open Graph/Twitter tags,
  canonical placement, `http://` scripts/styles/iframes, visible word count and a normalized content hash. The fetch
  records `X-Robots-Tag`. No extra HTTP requests. Measured parser cost: +1.0 ms per 140 KB page (6.4 → 7.4 ms), far
  below the ~83 ms per page the 12 req/s rate budget allows, so audit duration is unchanged.
* **Single-page rules** (`seo_audit.page_issues`): length/presence/multiplicity checks, noindex/nofollow on a sitemap
  page, canonical outside `<head>`, mixed content, invalid/missing structured data, thin content, hreflang code validity,
  self-reference, x-default and conflicts, URL hygiene.
* **Cross-page rules** (packager, report time): duplicate titles / descriptions / H1 / visible content among
  indexable self-canonical pages, hreflang reciprocity between audited pages, trailing-slash duplicates, orphan pages
  (no `<a>` from another audited page), internal 200 HTML pages linked from the site but missing from the sitemap.
* **Sitemap rules**: failed sitemap files, >50,000 URLs / >50 MB, duplicate entries (within and across files),
  `<lastmod>` missing / not W3C Datetime / in the future, plus per-sitemap roll-ups of page status, redirects,
  noindex and non-canonical pages (16_sitemap_audit.csv).
* **Trust gate additions**: SEO CSV rows = SEO DB rows; every parsed 2xx HTML page has an SEO row; sitemap report
  pages = source pages.

## 7. Performance model (why 15–20 min is a rate question)

Measured on 10K pages: primary-domain work = 1.073 requests/page (page + non-sitemap links + robots); the engine
holds the configured rate exactly (13.99 of 14.0 req/s). So:

`minutes ≈ (pages × 1.073 [+ pages × images/page if images share the budget]) / rps / 60 + ~0.5`

| Primary rate the real edge sustains | 10K pages |
|---|---|
| 8 req/s | ~22.7 min |
| 10 req/s | ~18.2 min |
| 12 req/s | ~15.2 min |
| 14 req/s | 13.1 min (measured) |

Images on a separate CDN host with its own `--host-budget` overlap the crawl; if they share the primary budget,
~60K image checks add ~70 min at 14 req/s. The dashboard wizard shows this estimate before launch, and live
insights explain any gap during the run. Measure the real ceiling with `bench/live_edge_matrix.py`.

## 8. Security

SSRF validation on the first URL and every redirect hop (private/link-local/metadata addresses blocked, DNS-aware);
robots.txt honored; WAF responses never reported as origin status; one crawl per domain; strict input validation
on job ids and report file names (no path traversal); crawled text is rendered as text only in the UI (no
`innerHTML`); CSV/Excel formula-injection guard on all exports; the API keeps the existing reverse-proxy auth middleware.

## 9. Operations

* Run: `./start.sh` (or `python -m qcv5`), open `/dashboard/`. Workers are spawned on demand.
* Config: `DB_FILE`, `QC_ONPAGE_DATA_DIR`, `QC_ONPAGE_REPORTS_DIR`, `ALLOW_LOCAL_TESTING` (tests only), plus every
  `qcv5/cli/run_on_page_audit.py` flag (rates, concurrency, host budgets, byte limits).
* Recovery: API restart is safe (workers continue); worker crash → `INTERRUPTED` → Resume.
* Tests: `python -m pytest` (see README for the suite map and expected environment-dependent skips/failures).
