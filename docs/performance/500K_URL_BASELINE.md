# QC V5 — 500,000 URL Crawl Baseline Benchmark Report

## 1. Executive Summary & Benchmark Scope

This document establishes the official unoptimized baseline performance of **QC V5** across five benchmark scales:
- **Benchmark A:** 10,000 URLs
- **Benchmark B:** 50,000 URLs
- **Benchmark C:** 100,000 URLs
- **Benchmark D:** 250,000 URLs
- **Benchmark E:** 500,000 URLs

Every benchmark evaluates the complete production crawl pipeline:
1. **URL Discovery & Scheduling:** Ingestion of source pages, link extraction via standard library `HTMLParser` (`Parser` in `html_parser.py`), anchor text and component attribution, URL normalization, and bounded queue scheduling.
2. **Security & SSRF Validation:** `URLNormalizer.is_safe_url` verifying every URL and redirect hop against private IPv4/IPv6 subnets, link-local addresses, and decimal/hexadecimal representations.
3. **HTTP Transport & Redirect Handling:** Bounded asynchronous HTTP client (`httpx.AsyncClient`) with connection pooling, manual hop-by-hop redirect tracking, status classification (`StatusClassifier`), and transient error classification.
4. **Duplicate Detection:** Automatic destination URL deduplication across pages.
5. **Database Persistence:** Real-time persistence of extracted link relationships and URL check results into SQLite in WAL mode (`extracted_links`, `job_destinations`, `url_results`).
6. **Telemetry & Profiling:** 26 core operational and resource metrics collected per run.

---

## 2. Summary Baseline Performance Matrix

| Benchmark Scale | Processed URLs | Total Duration (s) | Throughput (URLs/sec) | Throughput (URLs/min) | P50 Latency (ms) | P95 Latency (ms) | P99 Latency (ms) | Peak Memory (MB) | DB Size (MB) | DB Write Time (s) | HTML Parse Time (s) | Network Wait Time (s) |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **10,000 (A)** | 10,000 | 7.99 s | 1,251.50 u/s | 75,090.0 u/m | 0.38 ms | 0.91 ms | 1.43 ms | 66.68 MB | 13.91 MB | 7.22 s | 1.27 s | 4.74 s |
| **50,000 (B)** | 50,000 | 42.95 s | 1,164.28 u/s | 69,856.8 u/m | 0.40 ms | 1.03 ms | 1.57 ms | 134.81 MB | 69.66 MB | 41.68 s | 6.78 s | 25.62 s |
| **100,000 (C)** | 100,000 | 99.27 s | 1,007.35 u/s | 60,441.0 u/m | 0.48 ms | 1.19 ms | 1.75 ms | 147.65 MB | 140.21 MB | 97.27 s | 15.73 s | 59.52 s |
| **250,000 (D)** | 250,000 | 272.71 s | 916.74 u/s | 55,004.4 u/m | 0.52 ms | 1.29 ms | 2.01 ms | 173.27 MB | 356.66 MB | 268.14 s | 42.83 s | 162.93 s |
| **500,000 (E)** | 500,000 | 571.15 s | 875.42 u/s | 52,525.2 u/m | 0.53 ms | 1.38 ms | 2.21 ms | 227.14 MB | 717.21 MB | 563.31 s | 88.92 s | 342.03 s |

---

## 3. Comprehensive 26-Point Telemetry Breakdown by Scale

### Benchmark A: 10,000 URLs
- **Total URLs:** 10,000
- **Discovered URLs:** 27,639
- **Scheduled URLs:** 10,000
- **Processed URLs:** 10,000
- **Successful URLs (200 OK):** 9,213
- **Failed URLs (4xx/5xx):** 787
- **Timeouts:** 0
- **Redirects (3xx):** 1,000
- **Duplicates Detected:** 27,637
- **Throughput (URLs/sec):** 1,251.50
- **Throughput (URLs/min):** 75,090.00
- **Total Duration:** 7.99 s
- **Average Request Latency:** 0.47 ms
- **P50 Latency:** 0.38 ms
- **P95 Latency:** 0.91 ms
- **P99 Latency:** 1.43 ms
- **CPU Usage (Avg):** 20.8%
- **Peak Memory:** 66.68 MB
- **Average Memory:** 57.86 MB
- **Database Size:** 13.91 MB
- **WAL Size:** 0.00 MB
- **Database Write Time:** 7.223 s
- **HTML Parse Time:** 1.266 s
- **Network Wait Time:** 4.739 s
- **Average Queue Depth:** 951.7
- **Worker Utilization:** 100.0%

### Benchmark B: 50,000 URLs
- **Total URLs:** 50,000
- **Discovered URLs:** 138,186
- **Scheduled URLs:** 50,000
- **Processed URLs:** 50,000
- **Successful URLs (200 OK):** 46,062
- **Failed URLs (4xx/5xx):** 3,938
- **Timeouts:** 0
- **Redirects (3xx):** 5,000
- **Duplicates Detected:** 138,184
- **Throughput (URLs/sec):** 1,164.28
- **Throughput (URLs/min):** 69,856.80
- **Total Duration:** 42.95 s
- **Average Request Latency:** 0.51 ms
- **P50 Latency:** 0.40 ms
- **P95 Latency:** 1.03 ms
- **P99 Latency:** 1.57 ms
- **CPU Usage (Avg):** 20.7%
- **Peak Memory:** 134.81 MB
- **Average Memory:** 98.03 MB
- **Database Size:** 69.66 MB
- **WAL Size:** 0.00 MB
- **Database Write Time:** 41.681 s
- **HTML Parse Time:** 6.779 s
- **Network Wait Time:** 25.620 s
- **Average Queue Depth:** 990.8
- **Worker Utilization:** 100.0%

### Benchmark C: 100,000 URLs
- **Total URLs:** 100,000
- **Discovered URLs:** 276,363
- **Scheduled URLs:** 100,000
- **Processed URLs:** 100,000
- **Successful URLs (200 OK):** 92,121
- **Failed URLs (4xx/5xx):** 7,879
- **Timeouts:** 0
- **Redirects (3xx):** 10,000
- **Duplicates Detected:** 276,361
- **Throughput (URLs/sec):** 1,007.35
- **Throughput (URLs/min):** 60,441.00
- **Total Duration:** 99.27 s
- **Average Request Latency:** 0.60 ms
- **P50 Latency:** 0.48 ms
- **P95 Latency:** 1.19 ms
- **P99 Latency:** 1.75 ms
- **CPU Usage (Avg):** 22.6%
- **Peak Memory:** 147.65 MB
- **Average Memory:** 122.56 MB
- **Database Size:** 140.21 MB
- **WAL Size:** 0.00 MB
- **Database Write Time:** 97.266 s
- **HTML Parse Time:** 15.733 s
- **Network Wait Time:** 59.518 s
- **Average Queue Depth:** 995.5
- **Worker Utilization:** 100.0%

### Benchmark D: 250,000 URLs
- **Total URLs:** 250,000
- **Discovered URLs:** 690,912
- **Scheduled URLs:** 250,000
- **Processed URLs:** 250,000
- **Successful URLs (200 OK):** 230,304
- **Failed URLs (4xx/5xx):** 19,696
- **Timeouts:** 0
- **Redirects (3xx):** 25,000
- **Duplicates Detected:** 690,910
- **Throughput (URLs/sec):** 916.74
- **Throughput (URLs/min):** 55,004.40
- **Total Duration:** 272.71 s
- **Average Request Latency:** 0.65 ms
- **P50 Latency:** 0.52 ms
- **P95 Latency:** 1.29 ms
- **P99 Latency:** 2.01 ms
- **CPU Usage (Avg):** 23.2%
- **Peak Memory:** 173.27 MB
- **Average Memory:** 145.61 MB
- **Database Size:** 356.66 MB
- **WAL Size:** 0.00 MB
- **Database Write Time:** 268.138 s
- **HTML Parse Time:** 42.831 s
- **Network Wait Time:** 162.929 s
- **Average Queue Depth:** 998.0
- **Worker Utilization:** 100.0%

### Benchmark E: 500,000 URLs (Primary Benchmark Criterion)
- **Total URLs:** 500,000
- **Discovered URLs:** 1,381,821
- **Scheduled URLs:** 500,000
- **Processed URLs:** 500,000
- **Successful URLs (200 OK):** 460,607
- **Failed URLs (4xx/5xx):** 39,393
- **Timeouts:** 0
- **Redirects (3xx):** 50,000
- **Duplicates Detected:** 1,381,819
- **Throughput (URLs/sec):** 875.42
- **Throughput (URLs/min):** 52,525.20
- **Total Duration:** 571.15 s (9 min 31 s)
- **Average Request Latency:** 0.68 ms
- **P50 Latency:** 0.53 ms
- **P95 Latency:** 1.38 ms
- **P99 Latency:** 2.21 ms
- **CPU Usage (Avg):** 25.5%
- **Peak Memory:** 227.14 MB
- **Average Memory:** 182.69 MB
- **Database Size:** 717.21 MB
- **WAL Size:** 0.00 MB
- **Database Write Time:** 563.309 s
- **HTML Parse Time:** 88.925 s
- **Network Wait Time:** 342.027 s
- **Average Queue Depth:** 998.8
- **Worker Utilization:** 100.0%

---

## 4. Root Cause Bottleneck Analysis (Measured Data)

1. **Database Write Serialization (Primary Bottleneck):**
   - At 500,000 URLs, total execution time was **571.15 seconds**, and cumulative database write time was **563.31 seconds** (98.6% of elapsed time).
   - The root cause is the write batch threshold of 50 items in `results_buffer`, triggering **over 30,000 separate transaction commits** across URL results and extracted links.
   - Each commit requires acquiring SQLite's single writer lock, coordinating with the WAL file, and executing lock handoffs.

2. **Memory Footprint Scaling:**
   - Memory scaled gracefully from 66.68 MB at 10k to 227.14 MB at 500k URLs without exceeding available RAM.
   - However, memory is consumed primarily by string sets for deduplication and queued objects in memory.

3. **Throughput Degradation Curve:**
   - Throughput decreased steadily from **1,251.5 URLs/s at 10k** down to **875.4 URLs/s at 500k** (-30.0% drop).
   - This drop directly correlates with database size growth from 13.9 MB to 717.2 MB, where b-tree indexing, page eviction, and write commits take longer on a 700 MB database file.
