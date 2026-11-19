# QC V5 — System Architecture

Audience: engineers, tech leads, SRE. Companion docs: [design system](../design/DESIGN_SYSTEM.md),
[10K performance report](../performance/ON_PAGE_10K_URL_IMAGE_AUDIT.md).

## 1. What the system does

QC V5 audits websites at scale:

| Capability | Entry point | Output |
|---|---|---|
| **On-page URL + image audit** (sitemap pages → every link, canonical, hreflang, iframe and image on them) | Command Center (`/dashboard/`), `run_on_page_audit.py`, `/api/on-page/*` | 14 CSVs + ZIP, issue reports, live metrics |
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
           │ FastAPI app (server.py)             │────────────────┐         │ per primary domain
           │  · classic job APIs + batch daemon  │                ▼         │
           │  · /api/on-page/* (onpage_service)  │   ┌─────────────────────┴──┐   ┌──────────────┐
           └──────┬──────────────────────────────┘   │ Audit worker process   │──▶│ CDN / external│
                  │ aiosqlite (WAL)                  │ run_on_page_audit.py    │   │ hosts (own    │
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
| `server.py` | FastAPI app: classic APIs, `/api/on-page/*` dashboard API, static UIs (`/` classic, `/dashboard/` new) |
| `onpage_service.py` | Audit lifecycle (start/stop/resume), presets, duration model, issue browsing, smart insights, health score |
| `run_on_page_audit.py` | Worker CLI: inventory → engine → CSV/ZIP package; graceful stop (SIGTERM or stop-file); result JSON |
| `on_page_audit_engine.py` | Crawl + validation pipeline, rate control, WAF handling, registries, exact counters, checkpointing |
| `html_parser.py` | Single-pass HTML parser: links, canonical, hreflang, meta, area, iframe, images (srcset, picture, lazy, og/twitter) |
| `on_page_issue_rules.py` | 54 issue types → severity, detail, recommended action (pure functions) |
| `on_page_csv_packager.py` | Streaming reports 01–14, ZIP, 28-check trust gate |
| `on_page_schema.py` | Dependency-free image-audit DDL shared by API and worker |
| `request_errors.py` | Type-based transport error classification shared by checker + engine |
| `url_normalizer.py` | Normalization (cache key), syntax validation, SSRF checks (DNS/private-IP aware) |
| `job_manager.py`, `daemon.py`, `checker.py`, `db.py` | Classic bulk URL-status pipeline (per-URL ledger, resumable batches) |
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

Errors: 400 invalid input/ids, 404 unknown audit or report not built, 409 domain busy / already running.

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

* Run: `./start.sh` (or `uvicorn server:app`), open `/dashboard/`. Workers are spawned on demand.
* Config: `DB_FILE`, `QC_ONPAGE_DATA_DIR`, `QC_ONPAGE_REPORTS_DIR`, `ALLOW_LOCAL_TESTING` (tests only), plus every
  `run_on_page_audit.py` flag (rates, concurrency, host budgets, byte limits).
* Recovery: API restart is safe (workers continue); worker crash → `INTERRUPTED` → Resume.
* Tests: `python -m pytest` (see README for the suite map and expected environment-dependent skips/failures).
