# QC V5: Comprehensive System Optimization Audit & Baseline Report

**Date:** November 16, 2026  
**Auditor:** Antigravity AI (Google DeepMind)  
**Target:** Production-like URL Quality Control & Link Auditor V5 (`qc_v5-main`)  
**Scope:** Architecture, Execution Flow, Pipeline Analysis, Baseline Telemetry, and Bottleneck Identification  

---

## 1. Executive Summary

QC V5 is an enterprise-scale, background-processing URL and Image Auditor built with FastAPI, Uvicorn, SQLite3 (WAL mode), `httpx`, and Playwright. The platform is designed to process large workbooks (20,000+ URLs) across multiple execution modes:
- **Link Audit (`link_audit`):** Extracts links from source pages and concurrently validates unique destinations with deduplication.
- **Image Audit (`image_audit`):** Extracts and audits image assets, DAM references, and dimensions.
- **URL Status (`url_status`):** Validates raw URL status codes with redirect chain tracking.
- **Page & API Audit (`api_response_audit`):** Headless browser evaluation observing in-page AJAX/Fetch network calls.
- **Sitemap to Crawl (`sitemaps`):** Recursive sitemap index downloading, XML/gzip processing, robots.txt checking, and 403 diagnostics.

This audit establishes the performance baseline and profiles CPU, memory, I/O, network, database, parser, browser, concurrency, and frontend data flow to identify concrete, non-speculative optimization opportunities.

---

## 2. Complete Architecture & Execution Flow

```
                                  [ Client Browser / Frontend UI ]
                                                 │
                                     (HTTP / SSE / REST Polling)
                                                 ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ FastAPI Web Server (`server.py`)                                                       │
│  - ReverseProxyAuthMiddleware (`X-Proxy-Secret`, `X-Remote-User`)                      │
│  - Upload Streaming (1 MB chunks, 20K/1M record boundaries)                           │
│  - REST Endpoints (/api/jobs, /api/sitemaps, /api/remediations, /api/diagnostics/403) │
└──────────────────────────┬─────────────────────────────────┬───────────────────────────┘
                           │                                 │
                 (Enqueue Work/Batches)              (Live Stats/Reports)
                           ▼                                 │
┌──────────────────────────────────────────────┐             │
│ Job & Batch Lifecycle (`job_manager.py`)      │             │
│  - Chunking (settings.BATCH_SIZE = 1,000)    │             │
│  - Atomic Batch Leasing (`lease_expires_at`) │             │
│  - State Transitions (QUEUED->RUNNING->      │             │
│    VALIDATING->FINALIZING->COMPLETED)        │             │
└──────────────────────┬───────────────────────┘             │
                       │                                     │
           (Claimed Batches / Advanced Jobs)                 │
                       ▼                                     │
┌──────────────────────────────────────────────────────────┐ │
│ Background Worker Daemon (`daemon.py`)                   │ │
│  - Multi-Worker Loops (`WORKER_COUNT` = 8 concurrent)   │ │
│  - Independent Pipelines:                                │ │
│    1. Source Extraction Pipeline                         │ │
│       (`extractor.py`, `html_parser.py`, `browser.py`)   │ │
│    2. Destination Validation Pipeline                    │ │
│       (`link_validator.py`, `checker.py`, `tuner.py`)    │ │
│    3. Asynchronous DB Writer Queue (`_db_writer`)        │ │
└──────────────────────┬───────────────────────────────────┘ │
                       │                                     │
                 (Transactions)                              │
                       ▼                                     ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ SQLite Database Layer (`db.py`)                                                        │
│  - Journal Mode: WAL (Write-Ahead Logging)                                             │
│  - Synchronous: NORMAL, busy_timeout: 5000ms                                           │
│  - Core Tables: `uploads`, `jobs`, `batches`, `extracted_links`, `job_destinations`,   │
│    `url_results`, `image_results`, `page_audit_results`, `source_classifications`,      │
│    `remediations`, `sitemap_imports`, `sitemap_sources`, `request_diagnostics`         │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Primary Crawl Pipeline
1. **Source Page Ingestion & Extraction:**
   - Source workbooks (.xlsx or .csv) uploaded via `/api/upload` and parsed by `JobManager._iter_url_chunks`.
   - Work partitioned into discrete batches of 1,000 URLs (`batches/{batch_id}_input.json`).
   - `BatchWorkerDaemon` claims batches via atomic SQL updates (`UPDATE batches SET status = 'RUNNING', worker_id = ...`).
   - For each source URL, `LinkExtractor.extract_links` fetches HTML via `httpx`.
   - On blocked status (403, 429, 503) or zero links on large pages (>500 bytes), falls back to Playwright `BrowserPool`.
   - `Parser` extracts links and images, filtering out header/footer/nav/modals and isolating main content.
   - Discovered links normalized via `URLNormalizer.normalize(href)` and validated via `URLNormalizer.is_safe_url(href)`.
   - Relationships persisted in `extracted_links` with source page classifications in `source_classifications`.

2. **Destination Validation Pipeline:**
   - Once extraction batches complete, job moves to `VALIDATING`.
   - `LinkValidator.validate_job_destinations` retrieves unique destination URLs (`SELECT DISTINCT normalized_url FROM extracted_links WHERE job_id = ?`).
   - **Deduplication:** Repeated destinations across hundreds of source pages are collapsed so each unique destination is verified only once.
   - Cache lookup against `url_results` (TTL = 24h) avoids network calls unless `force_recheck = True`.
   - Uncached unique destinations queued into a bounded async queue (`LINK_AUDIT_MAX_QUEUE_SIZE = 1000`).
   - Worker pool (bounded concurrency 1–50, default 32) validates URLs using a long-lived connection-pooled `httpx.AsyncClient`.
   - Manual hop-by-hop redirects (up to 10 hops) with SSRF validation on every target address.
   - Per-host rate limiting via per-host semaphores (default 10).
   - Transient failures (429, 502, 503, 504, timeouts) retry with exponential backoff and jitter; respects `Retry-After`.
   - Results buffered and batch-flushed into `job_destinations` and `url_results`.
   - Job moves to `FINALIZING` -> `COMPLETED`.

---

## 3. Deep-Dive Component Inspections

### 3.1 Database Read/Write Paths (`db.py`)
- **Connection Isolation:** Uses `aiosqlite` with `isolation_level="IMMEDIATE"` and `PRAGMA synchronous=NORMAL;`.
- **Indexing:** Excellent index coverage on foreign keys (`job_id`, `normalized_url`, `source_url`, `status`, `checked_at`).
- **Read Bottleneck Discovered (N+1 Query Pattern):**
  - In `db.get_job_non_200_destinations`:
    ```python
    for row in rows:
        src_list = (row.get("source_urls_concat") or "").split(",")
        for s in src_list[:10]:
            if s:
                sc = await self.get_source_classification(s)
    ```
    For each non-200 destination, the code executes up to 10 separate round-trip SQL queries to fetch source classifications. For 100 non-200 URLs, this issues up to 1,000 queries.
  - In `ReportGenerator._do_generate_link_audit_report`:
    ```python
    source_urls = list(set([l.get("source_url") for l in extracted if l.get("source_url") and l.get("source_url") != "N/A"]))
    source_class_map = {}
    for s in source_urls:
        sc = await self.db.get_source_classification(s)
        if sc:
            source_class_map[s] = sc
    ```
    For every unique source URL in the workbook, an individual `SELECT` query is executed sequentially.
- **Write Path Discovered (High Commit Frequency):**
  - In `link_validator.py`, `flush_results_buffer` flushes when `len(results_buffer) >= 15`. At 200 URLs/s validation, this causes 13+ SQLite commits per second, generating needless lock contention.

### 3.2 HTML Parsing (`html_parser.py`)
- Subclasses standard library `html.parser.HTMLParser`.
- **Redundant Traversal Bottleneck:**
  In `handle_starttag` for `a` and `img`:
  `_identify_component()` is called directly, AND `_identify_container_type()` is called which immediately calls `_identify_component()` a second time.
  Each call iterates `reversed(self.path)` and executes `.get('class', '').lower()` and `.get('id', '').lower()` for all ancestors. For pages with 1,000+ links, this results in tens of thousands of duplicate string manipulations and list scans.

### 3.3 HTTP Connection Handling & Concurrency (`link_validator.py`, `checker.py`)
- Connection pool limits configured via `httpx.Limits(max_connections=32, max_keepalive_connections=20, keepalive_expiry=30.0)`.
- Explicit timeouts on connect (5s), read (15s), total (20s).
- **Lock Contention:**
  `_get_host_semaphore` acquires an `async with self._sem_lock` on every single request even when the host semaphore already exists in `self._host_semaphores`.

### 3.4 Security & SSRF Protection (`url_normalizer.py`)
- Strict defense against SSRF:
  - Disallows loopback (`127.0.0.1`, `::1`, `localhost`).
  - Disallows private RFC 1918 subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
  - Disallows link-local cloud metadata endpoints (`169.254.169.254`).
  - Resolves octal, decimal, and hexadecimal IP formats (`0177.0.0.1`, `0x7f000001`, `2130706433`).
  - Evaluates every hop in redirect chains.
- **Blocking DNS Bottleneck:**
  `_resolve_and_check_hostname` performs synchronous `socket.getaddrinfo(h_clean, None)` inside an async function. When not cached, this blocks the event loop. LRU cache size is 2048, which churns on large jobs.

### 3.5 Browser Fallback (`browser.py`)
- Headless Chromium managed through `BrowserPool` with `settings.BROWSER_CONCURRENCY` (default 2).
- Route interception in `LinkExtractor._route_interceptor` blocks images, media, and fonts to conserve network bandwidth and memory.
- `ApiResponseObserver` monitors in-page Fetch/XHR and captures viewport screenshots for diagnostic evidence.

### 3.6 Frontend Data Flow (`frontend/index.html`)
- Single-page application using Tailwind CSS and vanilla JavaScript.
- Communicates with backend via standard REST APIs (`/api/jobs`, `/api/overview`, `/api/sitemaps`, `/api/diagnostics/403`).
- Polling intervals are 1500ms–3000ms.
- Table rendering uses template strings (`tbody.innerHTML = ...`).

---

## 4. Baseline Telemetry & Benchmark Records

### 4.1 Benchmark 1: Phase 10.8 (Deduplication & DB Writes)
- **Dataset:** 10 source pages, 100 relationships, 20 unique destinations (80% deduplication).
- **100 Relationships DB Write:** 0.70 ms
- **20 Destinations Batch Upsert:** 0.65 ms
- **Stats Query Execution:** 1.16 ms
- **Non-200 Query Execution:** 10.21 ms
- **3-Sheet Excel Generation:** 257.82 ms (0.258 s)
- **Excel File Size:** 26.40 KB

### 4.2 Benchmark 2: Phase 10.9 (Concurrent Link Validation Across Concurrency Levels)
- **Dataset:** 100 unique destination URLs against controlled 30ms latency server.
- **Concurrency 1:** 4.65s (1,291.7 URLs/min), P50: 46.7ms, P95: 55.1ms, Speedup: 1.00x
- **Concurrency 5:** 1.19s (5,050.3 URLs/min), P50: 58.5ms, P95: 67.9ms, Speedup: 3.91x
- **Concurrency 10:** 0.60s (9,964.6 URLs/min), P50: 54.6ms, P95: 74.1ms, Speedup: 7.75x
- **Concurrency 20:** 0.59s (10,150.8 URLs/min), P50: 73.7ms, P95: 118.7ms, Speedup: 7.88x

### 4.3 Benchmark 3: Phase 10.9.2 (Bounded Concurrency & Memory Profiling)
- **Concurrency 1:** 3.78s, Throughput: 26.46 URLs/s, Memory Delta: +5.23 MB
- **Concurrency 5:** 0.92s, Throughput: 108.70 URLs/s, Memory Delta: +1.27 MB
- **Concurrency 10:** 0.62s, Throughput: 161.08 URLs/s, Memory Delta: -0.36 MB
- **Concurrency 20:** 0.48s, Throughput: 206.31 URLs/s, Memory Delta: +1.11 MB
- **Concurrency 50:** 0.61s, Throughput: 164.63 URLs/s, Memory Delta: +2.71 MB

### 4.4 Benchmark 4: Phase 10.9.3 (3-Pass Production Stability & Latency)
- **Concurrency 5 (3-Pass Avg):** 1.047s, Throughput: 95.56 URLs/s, Avg Latency: 49.6ms, P95: 67.6ms, DB Locks: 0
- **Concurrency 10 (3-Pass Avg):** 0.780s, Throughput: 129.09 URLs/s, Avg Latency: 69.8ms, P95: 122.6ms, DB Locks: 0
- **Concurrency 20 (3-Pass Avg):** 0.773s, Throughput: 129.43 URLs/s, Avg Latency: 124.8ms, P95: 208.6ms, DB Locks: 0
- **Concurrency 50 (3-Pass Avg):** 0.900s, Throughput: 118.45 URLs/s, Avg Latency: 227.7ms, P95: 553.4ms, DB Locks: 0

### 4.5 Benchmark 5: Phase 11.1 (100 URLs with Browser & Live API Observation)
- **Total Pages Processed:** 100
- **Total Runtime:** 77.64 seconds
- **Throughput:** 77.28 pages/minute
- **Browser Concurrency:** 2 workers
- **Peak Memory Usage:** 3.13 MB (tracemalloc)
- **API Requests Observed:** 70 requests
- **Outcome Accuracy:** 100.0% (100/100 ground-truth matches)
- **Avg DB Write Latency:** 4.18 ms
- **DB Lock Errors:** 0
- **Report Generation Duration:** 0.387 seconds

### 4.6 Benchmark 6: Phase 11.1.1 (High-Fidelity Telemetry & Accuracy Matrix)
- **Total Execution Time:** 73.16 seconds
- **Measured Throughput:** 82.01 pages/min
- **Overall Accuracy:** 100.0% (Precision/Recall/F1: 100.0% across FOUND, NOT_FOUND, API_ERROR, PAGE_ERROR, SKIPPED)
- **Median DB Write Latency:** 1.04 ms
- **P95 DB Write Latency:** 5.25 ms
- **DB Lock Errors:** 0
- **Combined RSS Footprint:** 501.64 MB (Python: 58.24 MB, Chromium Tree: 443.40 MB)

---

## 5. Summary of Bottlenecks Identified

| # | Bottleneck Domain | Root Cause | Impact | Complexity / Risk |
|---|-------------------|------------|--------|-------------------|
| **B1** | Database (Read) | N+1 individual queries in `get_job_non_200_destinations` (up to 1,000 queries per job) | High | Low |
| **B2** | Database (Read/Report) | Serial individual queries for source classifications and cached destinations in `ReportGenerator` | High | Low |
| **B3** | Database (Config) | Default SQLite cache size (2 MB) and disabled memory-mapped I/O (`mmap_size=0`) | High | Extremely Low |
| **B4** | Network / DB Write | Flush threshold in `LinkValidator` set to 15 items, causing 13+ SQLite commits/sec | Medium-High | Low |
| **B5** | Parser (CPU) | Redundant double-traversal of ancestor tree and repetitive lowercase calls on every tag | Medium | Extremely Low |
| **B6** | Concurrency / Async | Per-host semaphore lock acquired unconditionally for existing hosts | Low-Medium | Extremely Low |
| **B7** | Network / SSRF | Small LRU cache (2048) in `_resolve_and_check_hostname` causing repeated DNS lookups | Medium | Extremely Low |

---

## 6. Audit Conclusion
The core system is well-architected with robust abstractions and strict validation. However, concrete, measurable bottlenecks exist in:
1. Database query efficiency (N+1 lookups).
2. SQLite engine configuration (cache and memory mapping).
3. Database batch commit sizing in high-concurrency validation.
4. Redundant DOM ancestor tree evaluations in HTML parsing.
5. Unnecessary async lock acquisition on per-host semaphore lookups.
6. LRU cache sizing for hostname validation.

All identified optimizations can be implemented with small, reversible, surgical improvements without modifying API contracts, reducing crawl accuracy, altering database schemas, or weakening security.
