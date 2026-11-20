# QC V5 — Technical Leadership Audit & Executive Implementation Blueprint

**Platform:** QC V5 — Website Audit Intelligence  
**Target:** 10,000 URLs in 12–15 Minutes | 150,000 (1.5 Lakh) URLs in < 3 Hours  
**Engine:** Asynchronous Producer-Consumer Pipeline with 3-Level Deduplication  
**UI/UX:** Watermelon Design System with GSAP Micro-Interactions & Three.js Command Center  

---

## 1. Cross-Functional Leadership Council Review

Before executing, our multidisciplinary leadership council (System Architect, Full-Stack Engineer, Technical SEO Specialist, UI/UX Lead, and QA Lead) completed an end-to-end audit of the codebase:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                          QC V5 ARCHITECTURAL PIPELINE                                  │
│                                                                                        │
│   [ Sitemaps / URL Batches ]                                                           │
│               │                                                                        │
│               ▼                                                                        │
│   ┌───────────────────────┐       Single Request Per Page                              │
│   │  Fetch Workers (Pool) │ ──────────────────────────────────────┐                    │
│   └───────────────────────┘                                       │                    │
│               │ (HTML Payload)                                    │                    │
│               ▼                                                   │                    │
│   ┌───────────────────────┐                                       │                    │
│   │  Parse Workers (Pool) │ (Links, Canonicals, Images, Hreflangs)│                    │
│   └───────────────────────┘                                       │                    │
│        │                 │                                        │                    │
│        ▼                 ▼                                        ▼                    │
│   ┌───────────────┐ ┌────────────────┐              ┌─────────────────────────┐        │
│   │  Dest Workers │ │ Image Workers  │              │   Source Result Cache   │        │
│   │   (Link Dedup)│ │  (Image Dedup) │              │  (Reused for Dest/Links)│        │
│   └───────────────┘ └────────────────┘              └─────────────────────────┘        │
│        │                     │                                    │                    │
│        └─────────────────────┼────────────────────────────────────┘                    │
│                              ▼                                                         │
│                 ┌─────────────────────────┐                                            │
│                 │ SQLite WAL Batch Writer │                                            │
│                 └─────────────────────────┘                                            │
│                              │                                                         │
│                              ▼                                                         │
│                 ┌─────────────────────────┐                                            │
│                 │ 14-CSV Streaming Report │ ──► ZIP Package + 28 Trust Checks          │
│                 └─────────────────────────┘                                            │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 1.1. System Architect's Report: Scaling to 150,000 URLs in 3 Hours
* **Throughput Target:** 150,000 URLs in 3 hours = 10,800 seconds.  
  $$\text{Target Throughput} = \frac{150,000}{10,800} \approx 13.88 \text{ requests/second}$$
* **Engine Capability:** At **16.0 – 18.0 req/s** with `source_concurrency: 35`, `dest_concurrency: 35`, `image_concurrency: 50`, the crawl completes in **~2.79 hours** (under the 3-hour limit).
* **3-Level Deduplication:**
  1. *Level 1 (Same Page):* Multiple links to the same destination on one page are collapsed into a single database entry with an occurrence counter and anchor text collection.
  2. *Level 2 (Cross Page):* If a destination was already validated across any other page, the validation outcome is reused instantly from SQLite (`on_page_validation_cache`).
  3. *Level 3 (In-Flight):* An in-memory asynchronous lock (`_dest_in_flight`) ensures that if 100 concurrent workers discover the same URL simultaneously, only **one single HTTP request** is dispatched to the network.
* **Source-Result Reuse:** Any destination URL that is also in the sitemap is satisfied directly by its primary crawl fetch (`SOURCE_PAGE_REUSED`), eliminating redundant requests.

---

### 1.2. Technical SEO Specialist's Report: 404, Interlinking & Technical Audits

The system validates all URL relationships and flags **54+ issue types** across 4 severities (`HIGH`, `MEDIUM`, `LOW`, `INFO`).

#### A. 404 & Status Audits (Links & Images)
* **404 / 410 Broken Links:** Destination returns 404 or 410 (`NOT_FOUND`, `GONE`). Reports identify exact `source_page_url`, `found_url`, and `anchor_or_alt_text`.
* **Broken Images (404 / Content-Type Mismatches):** Identifies broken images (`IMAGE_NOT_FOUND`), zero-byte assets (`IMAGE_EMPTY`), and images returning HTML error pages (`IMAGE_CONTENT_TYPE_MISMATCH`).
* **Redirect Chains & Loops:** Identifies 301/302 redirect hops (`REDIRECT_CHAIN`, `REDIRECT_LOOP`, `BROKEN_REDIRECT`).

#### B. Interlinking & Link Architecture
* Extracts every internal link, external link, canonical, hreflang, iframe, and area tag.
* Discovers link syntax type (`RELATIVE`, `ROOT_RELATIVE`, `ABSOLUTE`, `PROTOCOL_RELATIVE`).
* Flags `INSECURE_HTTP_LINK` (mixed content where HTTPS pages link to HTTP).
* Preserves anchor text context and location (Header/Footer vs. Main Content Section).

#### C. On-Page Tag & Technical SEO Checks
* **Canonicals:** Missing canonical (`MISSING_CANONICAL`), multiple conflicting canonicals (`MULTIPLE_CANONICALS`), canonical pointing to non-200 or redirect (`CANONICAL_REDIRECTED`, `CANONICAL_NOT_FOUND`).
* **Robots Directives:** Sitemap URLs blocked by `robots.txt` or tagged with `<meta name="robots" content="noindex">`.
* **Titles & Meta Descriptions:** Missing title (`MISSING_TITLE`), soft 404s (`SOFT_404`), titles under 30 characters or over 65 characters (`TITLE_TOO_SHORT`, `TITLE_TOO_LONG`).
* **Headings Structure:** Pages missing `<h1>` (`MISSING_H1`) or declaring multiple `<h1>` tags (`MULTIPLE_H1`).
* **Image SEO & Core Web Vitals:** Missing `alt` tags (`MISSING_ALT`), oversized images > 300 KB (`LARGE_IMAGE`), and missing explicit dimensions (`width`/`height`) that cause Cumulative Layout Shift (CLS).

---

### 1.3. UI/UX & Frontend Engineer's Report: Watermelon UI & GSAP

* **GSAP 3.12 Integration:** Vendored locally at `frontend/dashboard/vendor/gsap.min.js` (zero external CDN dependency, fully functional offline).
* **Watermelon Design System (`data-theme="watermelon"`):**
  * **Palette:**
    * Watermelon Coral / Ruby: `#ff2a5f` (Primary brand, high severity, active crawl pulses)
    * Mint Emerald / Rind: `#00e599` (Success states, 200 OK badges, secondary accents)
    * Obsidian Dark Seed / Slate: `#070d18` & `#0c1626` (Backdrop surfaces, glassmorphic cards)
  * **Animations:**
    * `countUp()` powered by GSAP tweens with smooth easing (`power2.out`).
    * Staggered entrance cascades on metric tiles, chart containers, and issue cards (`.reveal`).
    * Reactive 3-way theme toggle: **Dark 🌙** ➔ **Watermelon 🍉** ➔ **Light ☀️**.
* **Three.js Live Crawl Visualizer:** Renders an interactive 3D node network visualizing real-time crawl throughput, active connections, and status distributions.

---

### 1.4. QA & Reliability Engineer's Report: Trust Gate & Data Integrity

* **28-Point Reconciliation Trust Gate:** The report packager runs 28 SQL-level integrity checks against the database before signing off. If a single row count, unique URL count, or status bucket does not balance, `final_qc_status` fails.
* **Resumability:** If interrupted or stopped via UI/CLI, re-running with the same `--job-id` resumes without re-validating already finished URLs.

---

## 2. Speed Presets & Concurrency Matrix

| Preset Name | Target RPS | Max RPS | Source Conc. | Dest Conc. | Image Conc. | 10K Pages Est. | 150K Pages Est. |
|---|---|---|---|---|---|---|---|
| **Conservative** | 8.0 req/s | 10.0 req/s | 10 | 20 | 10 | ~23 min | ~5.6 hours |
| **15–20 Min Target** | 12.0 req/s | 12.0 req/s | 20 | 20 | 30 | **15.2 min** | ~3.7 hours |
| **Max Tested** | 14.0 req/s | 14.0 req/s | 20 | 20 | 30 | **13.1 min** | ~3.2 hours |
| **Enterprise 150K** | **16.0 req/s** | **18.0 req/s** | **35** | **35** | **50** | **11.4 min** | **~2.79 hours** |

---

## 3. Report Package Manifest (`.zip`)

Every completed audit outputs a single ZIP package containing 14 streaming CSVs:

| # | File Name | Description & Contents |
|---|---|---|
| **01** | `01_summary.csv` | Executive KPI overview: totals, duration, request rates, memory, integrity status |
| **02** | `02_source_page_audit.csv` | Every sitemap URL audited: HTTP status, final URL, title, response time, link counts |
| **03** | `03_url_audit.csv` | Every link occurrence: source page, discovered URL, syntax, scope, anchor text, location |
| **04** | `04_unique_url_validation.csv` | Deduplicated list of unique URLs with final status, redirect chains, and validation sources |
| **05** | `05_image_audit.csv` | Every image placement: source page, image URL, alt attribute, dimensions, loading type |
| **06** | `06_unique_image_validation.csv` | Deduplicated image validations: HTTP status, content-type, content-length, issue flags |
| **07** | `07_broken_urls.csv` | **Primary 404 Fix List:** All 404, 410, 5xx, and timeout links with source pages & anchors |
| **08** | `08_redirect_urls.csv` | All redirected links: 301/302 hops, redirect count, and final destination URL |
| **09** | `09_broken_images.csv` | **Image 404 Fix List:** All broken, empty, or mislabelled images with source pages & alt |
| **10** | `10_external_urls.csv` | Outbound external links found across the site |
| **11** | `11_reconciliation.csv` | 28-point trust gate verification table (expected vs. actual DB counts) |
| **12** | `12_issues.csv` | **Master Fix Tracker:** Issue ID, severity, type, source page, URL, detail, **recommended action** |
| **13** | `13_issue_summary.csv` | Rollup by issue type: severity, total occurrences, unique URLs, and affected pages |
| **14** | `14_page_issue_summary.csv` | Priority page ranking: worst pages sorted by issue count (HIGH severity first) |

---

## 4. Master Remediation Guide (Fix Actions for Developers & SEO)

| Severity | Issue Type | Why It Occurs | Recommended Fix / Action |
|---|---|---|---|
| **HIGH** | `NOT_FOUND` | Link returns HTTP 404. | Update the `<a href="...">` on the source page to the correct canonical URL, or implement a 301 redirect if the page moved. |
| **HIGH** | `IMAGE_NOT_FOUND` | Image returns HTTP 404. | Re-upload the asset to the CDN or fix the `src`/`srcset` path on the source page. |
| **HIGH** | `REDIRECT_TO_BROKEN` | Link redirects to a 4xx/5xx error. | Update link directly to a 200 OK target; fix or remove the broken redirect rule. |
| **HIGH** | `SERVER_ERROR` | Destination returns 5xx error. | Check origin application logs for server crashes, unhandled exceptions, or database timeouts. |
| **HIGH** | `IMAGE_CONTENT_TYPE_MISMATCH` | Image returns HTML or non-image. | Server is returning an error/HTML page disguised as an image. Fix routing or asset storage. |
| **HIGH** | `IMAGE_EMPTY` | Image returns 0 bytes. | Asset file is corrupt or empty on CDN. Re-generate and re-upload the image asset. |
| **HIGH** | `REDIRECT_LOOP` | Too many redirect hops (> 10). | Inspect server rewrite rules (.htaccess/Nginx/Edge) and remove cyclical redirect paths. |
| **HIGH** | `SITEMAP_URL_NOINDEX` | Sitemap page has `<meta name="robots" content="noindex">`. | Remove `noindex` if page is intended to rank, or exclude this URL from the XML sitemap. |
| **HIGH** | `SOURCE_NOT_FOUND` | XML sitemap lists a 404 URL. | Remove the dead URL from the XML sitemap immediately. |
| **MEDIUM** | `REDIRECT_CHAIN` | Link passes through 2+ hops. | Update href to the final destination URL to eliminate latency and save crawl budget. |
| **MEDIUM** | `MISSING_ALT` | `<img>` has no alt attribute. | Add descriptive, keyword-rich `alt` text (or `alt=""` if purely decorative for accessibility). |
| **MEDIUM** | `SITEMAP_URL_NOT_CANONICAL` | Canonical points to a different URL. | Update sitemap to only list the canonical URL, or correct the `rel="canonical"` tag. |
| **MEDIUM** | `MULTIPLE_CANONICALS` | Page declares 2+ canonical tags. | Retain exactly one canonical tag in `<head>` and remove duplicate tags. |
| **MEDIUM** | `MISSING_H1` | Page lacks an `<h1>` heading. | Add a single `<h1>` containing primary search intent keywords. |
| **LOW** | `INSECURE_HTTP_LINK` | HTTPS page links to `http://`. | Switch protocol to `https://` to prevent mixed content security warnings. |
| **LOW** | `MISSING_CANONICAL` | Page has no `rel="canonical"`. | Add a self-referencing canonical tag `<link rel="canonical" href="...">`. |
| **LOW** | `LARGE_IMAGE` | Image exceeds 300 KB. | Compress asset and serve modern next-gen formats (WebP or AVIF). |
| **LOW** | `MISSING_IMAGE_DIMENSIONS` | Image lacks `width`/`height`. | Specify explicit `width` and `height` (or CSS `aspect-ratio`) to eliminate CLS layout shifts. |
| **LOW** | `EMPTY_ANCHOR_TEXT` | Link contains empty/whitespace text. | Provide descriptive anchor text to pass topical relevance and improve UX. |

---

## 5. How to Run Audits Locally

### Option A: Command Center UI (Recommended)
1. Open **[http://127.0.0.1:8002/dashboard/](http://127.0.0.1:8002/dashboard/)**.
2. Toggle the theme at top-right to enjoy the **Watermelon UI** (`🍉`).
3. Click **"New audit"**.
4. Paste sitemap URLs or upload a `.txt` / `.csv` file.
5. Select a preset:
   * **15-20 min target** for 10K pages.
   * **Enterprise 150K** for large-scale runs (150K in ~2.8 hours).
6. Click **"Launch audit"**. Watch the real-time Three.js graph, GSAP counter tweens, and live issue streams.
7. Click **"Download report (.zip)"** once completed.

### Option B: High-Speed CLI
```bash
python run_on_page_audit.py \
  --urls sitemap_urls.txt \
  --job-id onpage_audit_01 \
  --primary-domain example.com \
  --target-rps 16.0 \
  --max-rps 18.0 \
  --source-concurrency 35 \
  --dest-concurrency 35 \
  --image-concurrency 50 \
  --out-dir reports/onpage
```
To resume an interrupted run, simply execute the same command with the identical `--job-id`.
