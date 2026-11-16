# Link & Image Auditor - Architecture Document

## Overview
This platform is built as an enterprise-ready, background-processing URL and Image Auditor. Designed for massive scale (20,000+ URL workbooks), the platform decouples the initial file upload from the actual HTTP workload using a persistent Job Queue and Batching system. 

## 1. Core Technology Stack
- **Backend:** FastAPI (Python 3) for the API Layer, Uvicorn for ASGCI hosting.
- **Background Workers:** Asynchronous Python daemon (`daemon.py`) using `asyncio` running detached from the web server.
- **Frontend:** Vanilla JS and HTML/Tailwind CSS with real-time polling to a Dashboard.
- **Database:** SQLite3 operating in WAL (Write-Ahead Logging) mode with massive concurrency isolation.
- **Crawling/Validation Engine:** `httpx` for high-throughput HTTP networking, combined with a `Playwright` Headless Browser Pool for SPA/JavaScript rendering fallbacks.
- **Reporting:** `pandas` and `openpyxl` for high-performance memory-efficient DataFrame Excel exports.

## 2. Independent Extraction & Validation Pipelines
To handle massive throughput without artificial bottlenecks, the Daemon orchestrates two fully independent pipelines:
1. **Source-Page Extraction Pipeline:** The `daemon.py` worker continuously extracts from URLs using the headless BrowserPool, immediately pushing discovered unique destination URLs into an asynchronous global HTTP Queue.
2. **Destination Validation Pipeline:** A configurable pool of permanent HTTP workers (`HTTP_CONCURRENCY`) constantly consumes from the global HTTP Queue. 
This decoupling means that the engine validates links concurrently while it discovers them, rather than waiting for an entire batch to finish extracting.

## 3. Adaptive Concurrency Tuning
The HTTP validation engine features an intelligent `ConcurrencyTuner`. 
- **Backpressure Scaling:** If target domains begin throwing `429 Rate Limited`, `500 Server Error`, or if average latencies cross a dangerous threshold, the tuner actively steps down the global semaphore capacity.
- **Upward Scaling:** If success rates are near 100% and latency is low, the tuner seamlessly expands the concurrent connection pool up to a max cap.
This ensures we do not DDOS clients or get ourselves permanently blacklisted by cloud firewalls.

## 4. Strict Validation & Deduplication 
The platform enforces a strict separation between **Link Extraction Relationships** and **HTTP Validation**.
* `extracted_links`: Stores the relationship between a `source_page_url` and an `original_url`.
* `url_results` / `image_results`: Stores the network state of a unique domain/path.

If 10 pages link to `/contact`, the engine only queries `/contact` *once*, but the relationship is maintained in `extracted_links` so the final report accurately shows 10 rows. 
**Force Recheck:** Bypasses the cache for a run, executing live validation.

### Finalization Gate
A report cannot be generated if a Job is currently `QUEUED` or `RUNNING`. The final Excel compilation strictly checks that every unique `normalized_url` in `extracted_links` has a corresponding network check. If a job is manually aborted, the report filename is prefixed with `PARTIAL_`.

## 4. Database Schema
- **uploads:** Tracks physical file uploads and configurations.
- **jobs:** A group of batches. Maps to a specific Sheet in an Excel file.
- **batches:** A subset of work assigned to the background worker. 
- **extracted_links:** Tracks the exact location, anchor text, component, and `extracted_timestamp`.
- **url_results / image_results:** Dedicated cache tables tracking final destination resolution, precise error taxonomy (DNS, Timeout, Network), and `checked_at` (ISO 8601).

## 5. Precise Error Taxonomy
The `checker.py` handles exceptions meticulously. It no longer defaults to a 404.
If an `httpx` error occurs, it categorizes it as:
- **Timeout**: The server didn't respond in time.
- **DNS Error**: Domain name resolution failed (`name_not_resolved`).
- **Connection Error**: Refused or unreachable.
- **SSL Error**: Certificate verification failure.

## 6. Concurrency Policies
- The HTTP engine has a global `Semaphore` preventing massive bursts, coupled with a per-domain `Semaphore` to ensure we do not DDOS specific client servers.
- The daemon strictly processes one batch chunk at a time, protecting RAM while still moving at maximum network speed.
