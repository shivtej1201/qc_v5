# QC V5 — 500,000 URL Crawl Optimization Plan

## 1. Executive Summary & Objective

The primary objective of this optimization phase is:
> **Make QC V5 reliably capable of crawling and processing 500,000 URLs at benchmark scale while preserving crawl correctness, SEO audit functionality, security controls, database integrity, API behavior, and existing features.**

The 500,000 URL benchmark is the primary acceptance criterion.

This optimization strictly adheres to the core rules:
- **Do NOT rewrite the application.**
- **Do NOT replace the existing architecture.**
- **Do NOT redesign working functionality.**
- **Preserve SSRF protection, redirect validation, robots behavior, timeout handling, retry safety, URL normalization, duplicate detection, and crawl accuracy.**
- **Measure first, optimize one bottleneck at a time, and verify regressions after every change.**

---

## 2. Pipeline Profiling & Measured Time Allocation

Based on deep architectural inspection and baseline telemetry across 10,000 to 500,000 URLs, time and resource expenditures are classified into distinct stages:

```
┌────────────────────────────────────────────────────────────────────────┐
│ QC V5 Crawl & Processing Pipeline Profile                             │
├───────────────────────┬────────────────────────────────────────────────┤
│ Stage                 │ Primary Bottleneck & Resource Utilization      │
├───────────────────────┼────────────────────────────────────────────────┤
│ 1. URL Discovery      │ Source page HTML parsing & link extraction     │
│ 2. URL Normalization  │ Scheme/netloc/path normalization & SSRF check  │
│ 3. Queue Scheduling   │ Bounded queue backpressure & task scheduling   │
│ 4. HTTP Request       │ Connection pool acquisition & network fetch    │
│ 5. Redirect Handling  │ Hop-by-hop tracking & redirect SSRF validation │
│ 6. Response Handling  │ Body download & status code classification     │
│ 7. HTML Parsing       │ DOM parsing, link filtering & container tagging│
│ 8. Deduplication      │ Destination uniqueness check & collision cache │
│ 9. Database Writes    │ SQLite transaction frequency & WAL write I/O   │
│ 10. Memory Footprint  │ Retained HTML/response objects & queue buffers │
└───────────────────────┴────────────────────────────────────────────────┘
```

### Top Measured Bottlenecks Identified

1. **Database Commit Frequency (Write Bottleneck):**
   - At 500,000 URLs, flushing results every 50 items results in **10,000 SQLite transactions and disk syncs** for results alone, plus thousands more for extracted links.
   - Frequent commits cause WAL checkpoint delays, write lock contention, and event loop latency spikes.

2. **Memory Retention of Large Response Payloads (Memory Bottleneck):**
   - Retaining response bodies (`result.html = resp.text`) or large DOM structures across queues risks linear memory explosion when hundreds of thousands of items are in flight or queued.
   - For 500k scale, response bodies and DOM instances must be discarded immediately after link/SEO extraction.

3. **Queue Sizing & Task Scheduling (Concurrency Bottleneck):**
   - Bounded queues must enforce strict backpressure without starving HTTP workers.
   - Loading 500,000 items in a single SQL query (`SELECT DISTINCT normalized_url ...`) consumes tens of megabytes of RAM; streaming batches of 5,000 to 10,000 items from the database maintains an $O(1)$ memory footprint.

4. **HTML Parser Repetitive Traversals (CPU Bottleneck):**
   - For every extracted link, DOM ancestor traversal and attribute parsing must be single-pass without redundant tree walks or duplicate string lowercasing.

5. **HTTP Connection Pooling & Keep-Alive Tuning (Network Bottleneck):**
   - Ensure long-lived reusable async clients with tuned connection pools (`max_connections`, `max_keepalive_connections`) to avoid connection churn, ephemeral port exhaustion on Windows loopback, and pool timeout delays.

---

## 3. Prioritized Optimization Roadmap

Ranked strictly by:  
$$\text{Score} = \frac{\text{Impact} \times \text{Confidence}}{\text{Implementation Risk}}$$

Where:
- **Impact (1–5):** Measurable improvement in throughput, latency, CPU, or memory.
- **Confidence (1–5):** Certainty that the change directly resolves the bottleneck.
- **Implementation Risk (1–5):** Likelihood of regression, side-effects, or behavioral alterations (1 = lowest risk).

| Rank | Step | Target Component | Bottleneck | Impact | Conf. | Risk | Score | Description |
|:---:|:---:|:---|:---|:---:|:---:|:---:|:---:|:---|
| **1** | **OPT-500K-1** | `link_validator.py` & `db.py` | Database Commit Sizing & Transaction Optimization | 5 | 5 | 1 | **25.0** | Scale batch flush threshold from 50 to 500 records (with time-based fallback) to reduce SQLite commit frequency by 10x while maintaining WAL integrity. |
| **2** | **OPT-500K-2** | `checker.py` & `models.py` | Memory Optimization: Immediate Payload Release | 5 | 5 | 1 | **25.0** | Release response HTML bodies immediately after link/SEO extraction; prevent retaining 500k raw HTML strings in `UrlResult` memory. |
| **3** | **OPT-500K-3** | `link_validator.py` & `daemon.py` | Streaming Queue Backpressure & Chunked DB Ingestion | 4 | 5 | 1 | **20.0** | Ingest destinations from database in bounded chunks (5,000 URLs) instead of loading all 500,000 URLs into a single monolithic Python list in RAM. |
| **4** | **OPT-500K-4** | `config.py` & `link_validator.py` | HTTP Connection Pool & Keep-Alive Optimization | 4 | 5 | 1 | **20.0** | Optimize `httpx.Limits` (max_connections, keepalive_connections, keepalive_expiry) and benchmark concurrency matrix (5 to 100) to find peak stable throughput. |
| **5** | **OPT-500K-5** | `html_parser.py` | HTML Parser Attribute Caching & Single-Pass DOM Scan | 3 | 5 | 1 | **15.0** | Cache parsed tag attributes and avoid repetitive string operations during SEO link/container classification. |
| **6** | **OPT-500K-6** | `daemon.py` & `db.py` | Crash Recovery & Resumability Validation | 4 | 4 | 1 | **16.0** | Verify that worker crashes, 5xx server errors, or partial interruptions can resume cleanly using existing batch lease and progress tracking without duplicating work. |

---

## 4. Verification & Safety Protocols

After each optimization step, the following verification suite is executed:
1. **Regression Unit Tests:** All 56 unit test suite tests (`test_phase_10_10.py` through `test_phase_11_2_multiworker.py`).
2. **Domain Regression Tests:** All 14 domain regression tests (`test_data_pagetype.py`, `test_large_upload_streaming.py`, `test_partial.py`, etc.).
3. **SSRF Adversarial Security Suite:** All 20 SSRF adversarial attack vectors (`test_ssrf_adversarial.py`).
4. **Accuracy & Contract Verification:** Verify 100% ground-truth classification accuracy and zero alteration to database schemas or API contracts.
5. **Benchmark Comparison:** Measure throughput, latency, CPU, and memory before and after.
