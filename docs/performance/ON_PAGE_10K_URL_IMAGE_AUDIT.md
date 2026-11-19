# QC V5 — 10K Source-Page URL + Image Audit: Implementation & Measured Results

> **Where this was measured.** A 4-vCPU Linux container that **cannot reach www.bajajfinserv.in** (egress
> policy). All runs use the local simulated edge in `bench/` (`sim_edge.py` + `sim_site.py`): PDP-shaped pages
> (~140 KB, ~37 links, 21 `<img>` incl. `<picture>`, srcset, lazy `data-src`, og/twitter image), 800 ms page
> latency with origin degradation past 24 in-flight requests, a separate image CDN host (80 ms), and an
> **Akamai model** that blocks on >15 req/s over 10 s **or** ≥32 concurrent primary requests. These are a model
> of the edge, not Bajaj's real limits. Run `bench/live_edge_matrix.py` from your network for the real RPS ceiling.

## Headline (measured)

```
10,000 source pages ............ 100% fetched and audited (10,000 COMPLETED, 0 pending)
Runtime ........................ 768.9 s crawl + 18.1 s CSV/ZIP = 13.1 min end-to-end   (target 15-20 min)
URL relationships .............. 369,522 persisted = 10,626 NEW_CHECK + 358,896 REUSED
Image relationships ............ 250,304 persisted (240,304 HTTP = 60,319 NEW + 179,985 REUSED; 10,000 data: URIs)
HTTP work actually done ........ 10,000 source fetches + 626 URL checks + 60,319 image checks (server-side counted)
Every unique URL/image ......... requested exactly once (27 repeats = 15 redirect-hop targets + 12 retried 503s)
Reconciliation ................. 24/24 PASS · DB integrity_check = ok · ZIP verified · final_qc_status = PASS
WAF / 429 / 403 / timeouts ..... 0 / 0 / 0 / 0 · cooldowns 0 · peak RSS 226 MB
```

**The 13.1 min holds only under two conditions, both explained in §C:** (1) the edge sustains 14 req/s on
the primary host, and (2) image URLs are served by a host with its **own** limits, not under the primary
Akamai budget.

## A. Files changed

| File | Change | Reason |
|---|---|---|
| `html_parser.py` | `parse_srcset()` (spec tokenizer, keeps commas inside URLs); `all_on_page_images` collected in the **same** parse pass: `img` via configurable attributes, `<picture><source srcset>`, og:image / twitter:image, with alt present/text, width, height, loading, attribute source. Existing `images` list untouched. | §6, §8, §9, §22 |
| `config.py` | `ON_PAGE_IMAGE_ATTRIBUTES`, `ON_PAGE_IMAGE_SOURCE_ATTRIBUTES`, `ON_PAGE_IMAGE_META_KEYS` | configurable image attributes (§8) |
| `on_page_audit_engine.py` | Image pipeline: `classify_image_url`, `extract_on_page_images` (same Parser instance); image relationship table and **separate** image validation cache; image registry with single-flight futures; image worker pool with its own concurrency and body-discard limit; content-type mismatch, not-an-image and empty-image detection; `INVALID` URL scope; `validation_detail` (IN_FLIGHT_REUSED) and `seeded_by` (SOURCE_FETCH) provenance; image counters in the exact writer mirror and in the full reconciliation; per-kind request accounting (source/url/image/robots/probe, total vs primary-budget); `live_metrics` row for realtime UI; resume of PENDING image work; additive schema (`ensure_on_page_image_schema`). | §8-§22, §35, §36 |
| `on_page_csv_packager.py` (new) | Streaming CSV writer (one SQL query + `fetchmany` per report, no per-row lookups) for files 01-11, ZIP (deflate) and post-write verification, 24-check trust gate; `final_qc_status = FAILED` on any mismatch. | §23-§34 |
| `run_on_page_audit.py` (new) | CLI runner: inventory → engine → CSV/ZIP; every limit is a flag; re-run with the same `--job-id` resumes. Raises the config concurrency caps only when explicitly requested (they silently clamped source>20 / dest>32). | §17, §35 |
| `on_page_report_packager.py` | Counts `INVALID_SKIPPED` in the relationship trust gate | new INVALID scope |
| `db.py` | Server applies the same image DDL; `/api/on-page-audits/{id}/stats` adds image progress (denominator = unique images), URL pending count, image reuse rate, parsed `live_metrics` (rates, ETA on source population, throttle, WAF, robots, errors) | §36 |
| `bench/sim_site.py`, `bench/sim_edge.py` | Image markup + CDN host + mislabelled/broken images; Akamai concurrency trigger; origin latency knee | benchmark realism |
| `bench/run_10k.py`, `bench/matrix_10k.sh`, `bench/summarize_matrix.py` (new) | 10K benchmark and Section 38 matrix | §37, §38 |
| `test_on_page_url_image_audit.py` (new) | 6 tests (below) | §44 |

(The URL-status 125K work in the same change set — `job_manager.py`, `daemon.py`, `checker.py`, `report_generator.py`,
`server.py`, `status_classifier.py`, `url_normalizer.py`, `models.py`, `test_url_status_large_volume.py` — is
described in the commit message.)

## B. Architecture

* **Source pipeline.** Unchanged design (previous change): bounded `fetch_q` → source fetchers (GET + HTML,
  SSRF on every hop, robots, WAF diagnostics, retries) → `parse_q` → 2-thread parse pool → registry → writer.
  Source pages are audited like any URL (status, redirects, robots, WAF); sitemap membership is not trusted.
* **One parse.** One `Parser` pass yields links, canonical, hreflang, link-meta, area, iframe, images, meta tags,
  data-pageType and title. URL extraction, image extraction and classification all read that parser. HTML is
  dropped right after.
* **URL pipeline.** Normalized with the existing `URLNormalizer` (the cache key); scopes INTERNAL / EXTERNAL /
  NON_HTTP / FRAGMENT / **INVALID** (recorded, never requested).
* **Image pipeline.** Normalized with the same normalizer. Types HTTP_IMAGE / HTTPS_IMAGE / NON_HTTP_IMAGE /
  INVALID_IMAGE; scope INTERNAL_IMAGE / EXTERNAL_IMAGE. **Relationship key** = (source page, normalized URL,
  alt present, alt text), so different alt texts stay separate SEO rows. **Validation key** = normalized URL
  only. Separate cache table, separate queue, separate worker pool.
* **Dedup (both).** Level 1: same-page occurrences collapse into one row with `occurrence_count` and every
  attribute source. Level 2: an in-memory registry preloaded from the DB returns REUSED for any known result
  without queueing. Level 3: the first discoverer creates the single future; anyone else who finds the URL
  while it is queued or in flight joins it and is labelled `IN_FLIGHT_REUSED`. Proven server-side by tests and
  by the 10K run.
* **Cache reuse.** A URL that is itself a pending sitemap page waits for its own source fetch. If that fetch is
  an authoritative non-WAF HTTP result, it seeds the cache (`SOURCE_PAGE_REUSED`); otherwise the URL gets its
  own validation. In the 10K run this resolved 10,000 of 10,626 unique URLs without a second request.
* **Rate control.** One primary budget (strict spacing, Akamai cooldown, 15% reduction, probe) for every
  primary-host request, whether source, URL, image, robots or redirect hop. Requests are counted per kind; in
  the 10K run, primary requests split as source 10,000 / url 627 / image 104 / robots 1. Other hosts are bounded
  by per-host concurrency.
* **DB.** Still a single batched writer and one transaction per flush, now also carrying image rows and
  results. Counters are an exact mirror; full aggregation runs only at start, at checkpoints and at the end
  (mirror == DB in every run). DB write latency in the 10K run: flush avg 117 ms / p95 358 ms (one transaction
  per ~1 s of work, ~1,020 rows/s), write-queue peak 189 of 2,000.
* **Reports.** Streamed CSV (`fetchmany(5000)`) → ZIP. Report `validation_source` refines the project enums:
  NEW_CHECK, REUSED, IN_FLIGHT_REUSED, SOURCE_PAGE_REUSED, *_SKIPPED. `base_validation_source` keeps the DB
  value, so NEW + REUSED reconciliation still uses the existing invariant. Building the 10K package took 18.1 s
  for 217 MB of CSV → 9.9 MB ZIP. No Excel in the main pipeline (the legacy Excel packager is untouched).

## C. Performance

### C1. Concurrency matrix (Section 38): 1,000 pages per cell, fixed 14 req/s primary budget

| Source conc | Dest conc | Crawl (s) | Pages/s | URL val/s | Image val/s | Total req/s | Primary req/s | Max in-flight (server) | CPU % | Peak RSS MB | DB flush p95 ms | Write-q peak | Dest-q peak | WAF | 429 | 403 | Timeouts | Conn errors | QC |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 10 | 10 | 100.1 | 9.99 | 0.86 | 60.39 | 71.29 | 10.85 | 10 | 25.9 | 96.5 | 74.88 | 37 | 27 | 0 | 0 | 0 | 0 | 0 | PASS |
| 10 | 20 | 100.2 | 9.98 | 0.86 | 60.36 | 71.26 | 10.78 | 10 | 27.7 | 96.2 | 98.77 | 25 | 5 | 0 | 0 | 0 | 0 | 0 | PASS |
| 10 | 30 | 97.2 | 10.28 | 0.88 | 62.17 | 73.39 | 11.14 | 10 | 27.8 | 97.0 | 120.7 | 30 | 4 | 0 | 0 | 0 | 0 | 0 | PASS |
| 10 | 40 | 100.2 | 9.98 | 0.86 | 60.34 | 71.23 | 10.79 | 10 | 28.5 | 96.3 | 108.43 | 25 | 4 | 0 | 0 | 0 | 0 | 0 | PASS |
| 10 | 60 | 99.1 | 10.09 | 0.87 | 60.99 | 71.99 | 10.9 | 10 | 27.6 | 97.5 | 128.3 | 40 | 4 | 0 | 0 | 0 | 0 | 0 | PASS |
| 20 | 10 | 79.2 | 12.63 | 1.09 | 76.33 | 90.11 | 13.74 | 16 | 36.7 | 97.7 | 275.64 | 85 | 43 | 0 | 0 | 0 | 0 | 0 | PASS |
| 20 | 20 | 78.2 | 12.79 | 1.1 | 77.35 | 91.31 | 13.8 | 18 | 36.4 | 97.8 | 170.73 | 115 | 21 | 0 | 0 | 0 | 0 | 0 | PASS |
| 20 | 30 | 80.2 | 12.47 | 1.07 | 75.41 | 89.02 | 13.6 | 17 | 37.4 | 98.4 | 225.75 | 71 | 9 | 0 | 0 | 0 | 0 | 0 | PASS |
| 20 | 40 | 78.2 | 12.78 | 1.1 | 77.27 | 91.22 | 13.79 | 16 | 38.4 | 98.6 | 197.41 | 59 | 4 | 0 | 0 | 0 | 0 | 0 | PASS |
| 20 | 60 | 79.2 | 12.63 | 1.09 | 76.35 | 90.13 | 13.6 | 17 | 37.8 | 98.5 | 299.17 | 86 | 5 | 0 | 0 | 0 | 0 | 0 | PASS |
| 30 | 10 | 79.2 | 12.62 | 1.09 | 76.32 | 90.09 | 13.73 | 17 | 36.8 | 97.8 | 197.54 | 53 | 50 | 0 | 0 | 0 | 0 | 0 | PASS |
| 30 | 20 | 78.3 | 12.78 | 1.1 | 77.25 | 91.18 | 13.81 | 18 | 40.0 | 97.5 | 308.7 | 71 | 30 | 0 | 0 | 0 | 0 | 0 | PASS |
| 30 | 30 | 79.2 | 12.63 | 1.09 | 76.36 | 90.14 | 13.74 | 17 | 38.6 | 97.9 | 167.6 | 62 | 17 | 0 | 0 | 0 | 0 | 0 | PASS |
| 30 | 40 | 79.2 | 12.62 | 1.09 | 76.31 | 90.08 | 13.74 | 17 | 41.1 | 97.6 | 307.68 | 72 | 6 | 0 | 0 | 0 | 0 | 0 | PASS |
| 30 | 60 | 79.2 | 12.62 | 1.09 | 76.32 | 90.09 | 13.73 | 17 | 38.7 | 97.9 | 178.03 | 87 | 4 | 0 | 0 | 0 | 0 | 0 | PASS |
| 40 | 10 | 80.1 | 12.48 | 1.07 | 75.44 | 89.06 | 13.61 | 17 | 34.8 | 98.6 | 151.01 | 35 | 55 | 0 | 0 | 0 | 0 | 0 | PASS |
| 40 | 20 | 79.2 | 12.63 | 1.09 | 76.37 | 90.15 | 13.74 | 17 | 35.8 | 97.7 | 122.88 | 31 | 38 | 0 | 0 | 0 | 0 | 0 | PASS |
| 40 | 30 | 79.1 | 12.64 | 1.09 | 76.43 | 90.22 | 13.73 | 17 | 35.0 | 97.5 | 176.18 | 36 | 24 | 0 | 0 | 0 | 0 | 0 | PASS |
| 40 | 40 | 78.1 | 12.8 | 1.1 | 77.37 | 91.33 | 13.85 | 17 | 35.9 | 97.1 | 129.92 | 53 | 10 | 0 | 0 | 0 | 0 | 0 | PASS |
| 40 | 60 | 78.2 | 12.78 | 1.1 | 77.29 | 91.23 | 13.82 | 17 | 36.6 | 97.8 | 207.23 | 34 | 4 | 0 | 0 | 0 | 0 | 0 | PASS |

**Reading the matrix.** At 800 ms page latency, **10 source workers cannot fill a 14 req/s budget** (≈10.9 req/s:
10 in flight ÷ 0.8 s). **20 saturates it.** 30 and 40 change nothing, because the rate budget, not the worker
count, bounds in-flight requests (server max 16-19, far below the 32-connection trigger). Destination concurrency
is irrelevant: only ~6% of unique URL destinations are not themselves source pages. Every cell had 0
WAF / 429 / 403 / timeouts and a PASS trust gate. **Selected: source 20, destination 20, image 30** — the lowest
setting that saturates the budget, not the highest that survived.

### C2. 10,000-page run (measured)

| Metric | Value |
|---|---|
| Source pages | 10,000 (completed 10,000) |
| Runtime | 768.9 s crawl + 18.1 s package = **786.9 s (13.1 min)** |
| Pages/sec | 13.01 |
| Unique URLs / URL HTTP validations / URL reuse | 10,626 / 626 HTTP + 10,000 source-seeded / 358,896 reused relationships |
| Unique images / image validations / image reuse | 60,319 / 60,319 / 179,985 reused relationships |
| Total relationships | 369,522 URL + 250,304 image = 619,826 |
| Total req/s · primary req/s | 92.3 · 13.99 (budget 14.0) |
| Image validations/sec | 78.5 (CDN host, 30 workers) |
| Peak RAM · CPU | 225.8 MB · 44.8% of one core |
| DB latency | flush avg 116.9 ms, p95 357.7 ms |
| WAF · 429 · 403 · timeouts · cooldowns | 0 · 0 · 0 · 0 · 0 |
| Request latency | avg 221 ms, p95 944 ms |

### C3. What the 15-20 min target depends on (projection from the measured request counts)

The primary-budget work for 10K pages is fixed by the site: 10,732 requests (10,000 pages + 627 URL checks +
104 primary images + robots). The engine held 13.99 of 14.0 req/s, so runtime ≈ 10,732 / RPS + ~20 s overhead:

| Primary RPS that the real edge sustains | Projected 10K runtime | Target 15-20 min |
|---|---|---|
| 8 (current default) | ≈ 22.7 min | **missed**: rate budget, not code |
| 10 | ≈ 18.2 min | met |
| 12 | ≈ 15.2 min | met |
| 14 (measured on the model) | **13.1 min (measured)** | met |

**If images live under the same Akamai budget** (e.g. `cms-assets.bajajfinserv.in` matched by
`--primary-domain bajajfinserv.in`), the 60,215 CDN image checks join that budget: 70,947 requests ≈ **84 min at
14 req/s, 148 min at 8 req/s**. No code change can meet 15-20 min that way without exceeding a safe rate.
Measure whether the image host shares the Akamai limit (`live_edge_matrix.py`). If it does not, scope the
primary budget to `--primary-domain www.bajajfinserv.in`. That is an operator decision, not a default I changed.

## D. Accuracy / reconciliation (10K run, from `11_reconciliation.csv`)

```
source pages = expected ............ 10,000 = 10,000                  PASS
URL relationships persisted ........ 369,522 = NEW 10,626 + REUSED 358,896   PASS
unique URL cache = expected ........ 10,626 = NEW_CHECK 10,626 = 04 CSV rows    PASS
image relationships (HTTP) ......... 240,304 = NEW 60,319 + REUSED 179,985    PASS
unique image cache = expected ...... 60,319 = NEW_CHECK 60,319 = 06 CSV rows    PASS
CSV rows = DB rows ................. 03: 369,522 · 05: 250,304 · 02: 10,000 · 07/08/09/10 = DB queries   PASS
orphans / missing endpoints ........ 0 / 0 (URL and image)              PASS
job progress row = DB recount ...... PASS (mirror == full aggregation)
DB integrity (PRAGMA integrity_check) ok                                 PASS
final_qc_status .................... PASS
```

`test_trust_gate_fails_on_discrepancy` proves the gate is not decorative: deleting one image cache row flips
`final_qc_status` to FAILED with the failing check named.

## E. Tests (exact counts, this container)

| Suite | Result |
|---|---|
| Full pytest run (all collectable tests) | **106 passed, 17 failed, 0 skipped**. The 17 failures are byte-identical to the untouched baseline (no DNS/egress in the container; async scripts pytest cannot run). Baseline was 85 passed / 17 failed. |
| New: `test_on_page_url_image_audit.py` | 6/6: URL forms (relative, absolute, root-relative, protocol-relative, fragment, duplicate, external, canonical, hreflang, invalid); image forms (img src, srcset with comma-in-URL, picture/source, data-src, data-lazy-src, data-original, data-image, og:image, twitter:image, duplicates, alt variants, data: URI, invalid); end-to-end dedup L1/L2/L3 + source reuse + 200/301/302/404/410/500/timeout/robots/WAF/invalid/non-HTTP + all 11 CSVs + ZIP; image single-flight; hard-kill restart (in-flight recovery, completed work never redone, duplicate resume = zero requests); trust gate failure |
| `test_on_page_audit.py` + `test_on_page_audit_performance.py` | 10/10 (existing contract unchanged) · stable over 3 repeats |
| `test_url_status_large_volume.py` | 6/6 incl. the 125,000-URL end-to-end run |
| `test_crash_recovery.py` · `test_job_realtime_status.py` · `test_ssrf_adversarial.py` (DNS stubbed for its 3 public names) | PASS · 8/8 · PASS |
| `test_large_upload_streaming.py` (script) | PASS (expectation updated to one report row per submitted URL) |

**Not tested here:** the real Bajaj edge (no network access), HTTP/2 on Akamai, and frontend UI rendering of the
new image metrics. The frontend has no on-page audit screen; the metrics are served by the existing stats API.

## F. Follow-up: review fixes and per-host rate budgets

### Review fixes (each pinned by a regression test)

| Issue | Fix |
|---|---|
| API server could not start without `psutil` (db.py imported the crawler for the image DDL) | DDL moved to dependency-free `on_page_schema.py`; `psutil` added to `requirements.txt` |
| URLs with spaces in path/query (e.g. AEM `/content/dam/My Image.png`) were marked INVALID and never checked | Only whitespace in scheme/host/port is invalid; spaces in path/query are percent-encoded and validated |
| CSV upload could silently drop every row after a chunk whose first cells were not URLs | URL column detected once and reused for all chunks |
| Crash between canonical and duplicate writes could strand DUPLICATE rows PENDING forever | `heal_pending_duplicates()` runs at batch start and before a job can be judged PARTIAL |
| Lock retry called `rollback()` on the connection shared with the daemon/API, discarding their writes | No rollback; the statements are idempotent and simply re-run |
| SSRF redirect hook did blocking DNS on the event loop | Resolution moved to a worker thread |
| DNS failure cache unbounded | Capped at 10,000 entries with expiry pruning |

### Per-host rate budgets (`--host-budget HOST=RPS[:MAX]`, engine `host_budgets=`)

Non-primary hosts used to be limited by concurrency only. A host such as an image CDN can now get its own
budget: strict spacing, its own WAF counter, 15% step-down + cooldown, and gradual increase. Its WAF never
touches the primary budget. Measured on 1,000 pages with a CDN that has its own Akamai model (blocks above
40 req/s):

| | No CDN budget (old behavior) | `--cdn-budget 35` |
|---|---|---|
| Peak CDN req/s (server) | 126 | 37 |
| CDN WAF blocks / 403s served | 4 / 5,635 | 0 / 0 |
| Images with a real status | **411 / 6,046 (7%)** | **6,046 / 6,046** |
| Crawl time | 79 s | 174 s |
| Trust gate | PASS | PASS |

The unbudgeted run "passes" reconciliation while 93% of image results are WAF blocks, so speed without a host
budget is meaningless when the CDN is protected. With a budget the CDN becomes the long pole: 10K pages ≈ 60K
images ÷ CDN RPS (≈29 min at 35 req/s). Measure the CDN's real safe rate and set it with `--host-budget`.

Tests after this change: **111 passed, 17 failed** (the same 17 pre-existing environment failures), plus
crash-recovery, realtime 8/8, SSRF, and large-upload script suites PASS.
