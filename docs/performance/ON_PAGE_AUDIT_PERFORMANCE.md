# QC V5 — On-Page Audit Performance Enhancement

Scope: optimize the existing `OnPageAuditEngine` (no rebuild, no new infrastructure) for the ~125K
sitemap source-page audit while preserving URL/status accuracy, source → destination relationships,
SSRF, robots, Akamai/WAF protection, checkpoint recovery and final reconciliation.

> **Measurement environment.** All numbers below were measured in a 4-vCPU Linux container. The
> production edge (`www.bajajfinserv.in`) is **not reachable** from that container (egress policy), so
> end-to-end runs use a local simulated edge (`bench/sim_edge.py`) shaped after the real Bajaj Mall run
> (`docs/reports/bajaj_mall_on_page_final`: ~35 relationships/page, ~97% reuse, one shared external
> iframe, redirects and 5xx) with a 300 ms lognormal latency model and an *Akamai-like model* (blocks
> after >15 req/s sustained over 10 s). **No result here is a measurement of the real Akamai edge.**
> `bench/live_edge_matrix.py` runs the Section 29 matrix against the real edge from your network.

## 1. Bottlenecks found in the current implementation (measured before any change)

| # | Bottleneck | Evidence |
|---|---|---|
| 1 | `sync_job_counters()` runs **full-table `COUNT(*)` / `GROUP BY` / `COUNT(DISTINCT)` after every 50 source pages** (and every 100 Stage B destinations). Its cost grows linearly with the relationship table. | 0.21 s at 250K rows → **11.75 s per call at 4.35M rows** (`bench/bench_db.py`). At 8 RPS a 50-page batch takes 6.25 s to fetch, so beyond ~2.5M relationships the DB writer — not Akamai — sets the pace. End-to-end at 3.5M rows: baseline achieved only **4.76 of 8 RPS at 97% CPU** (table A). Consistent with the slow tail of the real run (~31K pages remaining at the 03:08 snapshot, final report at 10:17). |
| 2 | Pacemaker reserves a rate slot **before** taking the per-host semaphore. When slots are reserved while the semaphore is full, requests leave back-to-back as soon as it frees. | Baseline: **10 primary requests in one second at an 8 RPS target** (server-side, tables A/B). |
| 3 | Redirect hops after the first were **not charged** to the primary budget; robots.txt fetches were never charged. | Code review (`fetch_url_manual_redirects`). |
| 4 | After a cooldown, workers sleeping on already-reserved slots re-check the gate and **all fire at once** (thundering herd). | Code review (`_acquire_primary_pace` epoch-less). |
| 5 | Serial Stage A → Stage B: no destination is validated until all ~125K sources finish; external validations cannot overlap primary traffic. | Code review. |
| 6 | When `tag-1`/`tag-2` meta tags are missing, `Classifier.classify_from_html` **parses the same HTML a second time**. | Code review. |
| 7 | Destination checks (`need_body=False`) still downloaded **and gunzipped** the full body (`client.get` reads everything). | Code review. |
| 8 | Keep-alive pool smaller than the connection pool (20 vs 40) → primary connections can be evicted and re-handshaked. | Code review. |
| 9 | Source-page results seeded the destination cache even when the outcome was a **timeout / network error / WAF block** (violates Rule 17). | Code review (`self_cache_rec` was written for every terminal state). |

Not bottlenecks (measured, so left alone): HTML parsing (7.6 ms/page → ~120 pages/s on one core), SQLite
batched writes (≥30K relationship rows/s even at 100-page batches vs ≤700 rows/s needed at 20 RPS), and
indexes (dropping three relationship indexes gains only ~14% on a non-bottleneck path; all kept).

## 2. What changed

**Bounded pipeline (Sections 5, 23, 31, 32).**
`source producer → [fetch_q] → source fetchers → [parse_q] → parse pool → registry → [dest_q] → destination workers`,
with **all** results going to `[write_q] → one batched SQLite writer`. Every queue is bounded
(fetch 2×workers, parse 4×parse workers, dest 5,000, write 2,000 packets), so backpressure flows upstream
and memory stays flat. Destination validation starts as soon as a destination is discovered.

**One authoritative primary budget (Sections 6, 8).** Every request to a primary host — source fetches,
destination checks, every redirect hop, robots.txt — reserves a slot on one virtual clock with strict
1/target_rps spacing (no burst allowance). The slot is reserved *after* the concurrency semaphore, so a
request leaves at its slot. Lock-free (no await inside the reservation), one `sleep` per request, and a
cooldown **epoch** voids slots reserved before a cooldown so released workers leave one interval apart.
Additional primary hosts can be listed with `primary_hosts=[...]`.

**Adaptive control reuses the existing controller (Section 7).** Same `consecutive_waf` threshold,
cooldown gate, Akamai probe and **15% reduction**; the gradual increase (+0.25 per 2,000 healthy responses,
capped by `max_rps`) now counts healthy primary responses from both sources and destinations (same edge,
same budget). Timeouts and 5xx no longer count toward an increase. Thresholds are constructor parameters.

**Destination registry (Sections 14–17, 33).** In-memory, preloaded once from `on_page_validation_cache`
at start (O(1) lookups, no per-URL DB query):
* known terminal state → `REUSED`, never queued;
* already queued / in flight → join the single owner (`asyncio.Future`); `validate_destination()` exposes the
  single-flight path;
* URL is itself a still-pending sitemap source → **AWAITING_SOURCE**: no destination request; the source
  fetch satisfies it;
* source results seed the cache **only** for authoritative origin HTTP outcomes (`VALIDATED`, real status,
  not WAF). Timeouts, DNS/network/SSL errors, WAF/rate-limit blocks, robots and SSRF outcomes are not
  seeded — the destination gets its own validation under the normal policy.

**Parse once, drop HTML (Sections 18–20).** One `Parser` pass per page feeds links, canonical, hreflang,
meta tags, data-pageType and title; `classify_from_html(..., parser=...)` reuses it. Parsing runs in a
dedicated 2-thread pool (measured lowest event-loop lag of the thread options; process pools were
faster but unnecessary at ≤20 pages/s). HTML is released right after extraction + classification.

**Destination bodies (Sections 11, 12).** Still `GET` with manual hop-by-hop redirects and SSRF checks at
every hop. With `need_body=False` small bodies are drained as raw bytes (connection stays reusable,
no gunzip); bodies over 256 KB are not downloaded (connection closed instead). 403/429 bodies are still
read for Diagnostics403.

**DB writer and counters (Sections 21–27).** One writer, one transaction per flush (source updates,
relationships, cache seeds, source-seeded results, destination results **and** the job progress row),
flushing at `batch_size` (default 250 pages) or every 1 s. Counters are an **exact mirror** maintained
from what each transaction writes (mirroring the SQL order and UPSERT semantics), applied only after commit.
The UI row is a single-row `UPDATE`. Full aggregation runs only at start, every `reconcile_interval_sec`
(default 15 min) and at the end, where mirror vs DB is compared (`counter_reconciliation`) — a mismatch
adopts DB values and is reported. `url_relationships_discovered` (extracted) and
`url_relationships_persisted` (committed) are now distinct populations.

**Robustness.** Graceful `request_stop()` (in-flight work finishes and is flushed; unstarted destinations
stay `PENDING` and are rescheduled on resume). A failing writer aborts the run with an error instead of
hanging producers on the bounded queue; nothing uncommitted is marked done.

**Connection pool (Section 9).** `max_keepalive_connections = max_connections`, keep-alive expiry
configurable (30 s default). **HTTP/2 stays off by default** — it could not be benchmarked against the
real Akamai edge from here; `http2=True` is available and `bench/live_edge_matrix.py --http2` compares it.

## 3. Measured results

### A. Real-run DB scale (3.5M relationships already persisted, 800 live sources, 8 RPS budget)

| Metric | Baseline | Optimized | Δ |
|---|---:|---:|---:|
| Wall-clock (s) | 227.7 | 135.2 | -41% |
| Source pages/sec (incl. start/finish) | 3.51 | 5.92 | +69% |
| Relationships persisted/sec | 127.8 | 215.2 | +68% |
| Achieved primary RPS (server-side) | 4.76 | 8.03 | +69% |
| Max primary requests in any 1 s (server-side) | 10 | 9 | -10% |
| Primary requests (server-side) | 853 | 854 | +0% |
| Server requests total | 879 | 880 | +0% |
| Unique HTTP destinations | 100,874 | 100,874 | +0% |
| NEW_CHECK / REUSED | 100,874 / 3,427,628 | 100,874 / 3,427,628 | |
| Paths requested >1× (server-side) | 3 | 3 | +0% |
| Peak RSS (MB) | 223.7 | 227.5 | +2% |
| CPU avg % (1 core = 100) | 97.3 | 28.4 | -71% |
| Job status | COMPLETED | COMPLETED |  |

### B. Fresh DB (3,000 sources, 8 RPS budget)

| Metric | Baseline | Optimized | Δ |
|---|---:|---:|---:|
| Wall-clock (s) | 399.8 | 399.2 | -0% |
| Source pages/sec (incl. start/finish) | 7.50 | 7.52 | +0% |
| Relationships persisted/sec | 276.5 | 276.9 | +0% |
| Achieved primary RPS (server-side) | 8.00 | 8.01 | +0% |
| Max primary requests in any 1 s (server-side) | 10 | 9 | -10% |
| Primary requests (server-side) | 3,191 | 3,192 | +0% |
| Server requests total | 3,217 | 3,218 | +0% |
| Unique HTTP destinations | 3,206 | 3,206 | +0% |
| NEW_CHECK / REUSED | 3,206 / 107,321 | 3,206 / 107,321 | |
| Paths requested >1× (server-side) | 9 | 9 | +0% |
| Peak RSS (MB) | 128.7 | 126.9 | -1% |
| CPU avg % (1 core = 100) | 10.5 | 9.8 | -7% |
| Job status | COMPLETED | COMPLETED |  |

**A optimized engine telemetry:** source-seeded validations 800, destination HTTP validations 74, DB flush avg 10.42 ms / p95 22.76 ms, DB rows/s 259.6, write-queue peak 3, dest-queue peak 4, parse-queue peak 1, request latency avg 319.5 ms / p95 531.7 ms, timeouts 0, 403 0, 429 0, cooldowns 0, counter reconciliation PASS.

**B optimized engine telemetry:** source-seeded validations 3,000, destination HTTP validations 206, DB flush avg 13.51 ms / p95 30.13 ms, DB rows/s 308.6, write-queue peak 3, dest-queue peak 4, parse-queue peak 1, request latency avg 320.4 ms / p95 535.6 ms, timeouts 0, 403 0, 429 0, cooldowns 0, counter reconciliation PASS.

### C. Fixed-RPS matrix vs simulated Akamai model (blocks when >15 req/s sustained over 10 s)

| Target RPS | Achieved primary RPS | Source pg/s | Dest HTTP validations | Wall (s) | 403 served | WAF events | Cooldowns | 429 | Timeouts | Lat avg/p95 (ms) | DB flush p95 (ms) | CPU % | RSS MB | Result |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 6 | 6.01 | 5.63 | 98 | 213.1 | 0 | 0 | 0 | 0 | 0 | 319.3/532.3 | 18.97 | 7.1 | 83.9 | stable |
| 8 | 8.02 | 7.5 | 98 | 160.0 | 0 | 0 | 0 | 0 | 0 | 319.4/535.3 | 27.63 | 9.5 | 84.4 | stable |
| 10 | 10.02 | 9.37 | 98 | 128.0 | 0 | 0 | 0 | 0 | 0 | 319.5/533.1 | 24.45 | 11.9 | 84.7 | stable |
| 12 | 12.03 | 11.24 | 98 | 106.8 | 0 | 0 | 0 | 0 | 0 | 319.9/534.5 | 37.16 | 14.2 | 85.9 | stable |
| 15 | 10.83 | 10.02 | 100 | 119.8 | 13 | 13 | 1 | 0 | 0 | 316.6/532.3 | 33.17 | 12.5 | 84.8 | WAF → stop |

### D. Adaptive controller (start 8 RPS, ceiling 20, +0.25 per 300 healthy responses, same Akamai model)

Wall 350.0 s for 3,000 sources (8.57 pg/s), achieved primary RPS 9.13, final target 10.5, cooldowns 0, 403 served 0, WAF blocks 0, status COMPLETED, counter reconciliation PASS.

Rate history (last events): t=39.0s 8.0→8.25 (increase); t=75.6s 8.25→8.5 (increase); t=111.3s 8.5→8.75 (increase); t=145.5s 8.75→9.0 (increase); t=178.9s 9.0→9.25 (increase); t=211.4s 9.25→9.5 (increase); t=243.0s 9.5→9.75 (increase); t=273.7s 9.75→10.0 (increase); t=303.7s 10.0→10.25 (increase); t=333.3s 10.25→10.5 (increase)


### Full 124,474-source run — projection from measured components (not a measurement)

`bench/project_full_run.py` integrates the measured `sync_job_counters()` cost curve over the real run's
shape (124,474 sources, ~35 relationships/page, ~5.9K non-source destinations). Calibration check: the
model predicts the baseline at 5.5 pages/s at 3.5M rows; measured was ~4.4, so the model is *generous to
the baseline*.

```
rps= 8.0: baseline ≈  5.36 h | optimized ≈  4.53 h | saving ≈ 0.83 h (15%)
rps=10.0: baseline ≈  4.89 h | optimized ≈  3.63 h | saving ≈ 1.26 h (26%)
rps=12.0: baseline ≈  4.63 h | optimized ≈  3.02 h | saving ≈ 1.60 h (35%)
```

At a fixed 8 RPS the remaining wall-clock is the rate budget itself (~130K requests ÷ 8 ≈ 4.5 h). The
baseline's writer-bound floor barely moves when RPS rises; the optimized engine scales linearly with the
safe RPS — which is why the live matrix (below) is the next lever.

## 4. Section 37 summary (run A: real-run DB scale, simulated edge, fixed 8 RPS budget)

```text
QC V5 ON-PAGE AUDIT PERFORMANCE                 baseline -> optimized

Source pages:                                   800 live (on top of 100,000 completed pages / 3.5M relationships)
Source pages/sec:                               3.51 -> 5.92 (incl. start/end reconciliation; steady state = budget, 8.0)
URL relationships discovered:                   29,099 -> 29,099 (identical)
Unique destinations (job):                      100,874 -> 100,874 (identical)
HTTP validations (destination requests):        74 network validations + 800 source-seeded (same server-side request count as baseline: 854 vs 853 incl. 1 readiness probe)
Reused validations (job):                       3,427,628 -> 3,427,628
Reuse rate:                                     97.1%
End-to-end throughput:                          127.8 -> 215.2 relationships/s ; 4.76 -> 8.03 primary req/s
Peak RAM:                                       223.7 -> 227.5 MB
CPU:                                            97.3% -> 28.4% (one core = 100%)
SQLite write latency:                           flush avg 10.4 ms / p95 22.8 ms (one txn per flush)
DB queue depth:                                 write-queue peak 3 (bound 2,000); dest-queue peak 4
Akamai cooldowns:                               0 (model threshold not reached at 8 RPS); 1 at 15 RPS in matrix C
403/WAF:                                        0 (13 served + handled at 15 RPS in matrix C)
429:                                            0
Timeouts:                                       0
Existing regression:                            85/102 pass — identical 17 pre-existing failures before and after
                                                (no DNS/egress in this container; async scripts pytest can't run)
                                                + test_crash_recovery PASS, test_job_realtime_status 8/8,
                                                  test_ssrf_adversarial PASS (DNS stubbed for its 3 public hostnames)
On-page tests:                                  10/10 (1 existing + 9 new)
Reconciliation:                                 PASS (mirror == full aggregation in every run; trust gate PASS on 700-source package test)
Accuracy:                                       PASS (identical relationships / destinations / NEW_CHECK / REUSED vs baseline)
Security:                                       PASS (SSRF every hop, robots never fetched, WAF never converted to origin status)
```

## 5. Tests added (`test_on_page_audit_performance.py`, all against a real recording HTTP server)

| Test | Proves |
|---|---|
| duplicate destination | 10 sources → same slow destination → **1 request**, 10 relationships (1 NEW_CHECK, 9 REUSED) |
| in-flight dedup | 8 concurrent `validate_destination()` → **1 request**, all receive the same result object; later call = cached, no request |
| source = destination | 5 sources cross-linking + canonical self-links → **each fetched exactly once**, cache rows VALIDATED/200/referenced |
| Rule 17 | timed-out source → destination validated separately (→ 200); WAF-blocked source → **not reused**, destination gets its own WAF classification |
| Akamai | simulated Akamai 403 burst → cooldown, **15% RPS reduction**, requeue policy, all work completes; server-side: no 1 s window above budget, no herd |
| restart | graceful interrupt after ~20 pages → restart → **every URL requested exactly once across both runs**, relationships identical to an uninterrupted run |
| reconciliation | 700 sources / 20K+ relationships → **CSV rows == DB rows == counters**, trust gate PASS, mid-run checkpoint reconciliations with 0 mismatches |
| security | SSRF redirect hop to 169.254.169.254 → Blocked (SSRF); robots-disallowed URL never requested |
| writer failure | DB writer error → run aborts with an error (no hang); every source still PENDING (resumable) |

## 6. Next step that needs your network: live Akamai matrix + HTTP/2 decision

```bash
python bench/live_edge_matrix.py --urls sitemap_urls.txt --levels 6,8,10,12,15,18,20 --window-min 10 --out live_matrix.json
python bench/live_edge_matrix.py --urls sitemap_urls.txt --levels <chosen> --window-min 10 --http2 --out live_matrix_h2.json
```

Ascending levels only, a sustained window each, a 2-minute pause between levels, and it stops at the first
level with any cooldown, WAF response or 429s above 0.5%. Use the recommended `target_rps`/`max_rps` it
prints as the engine defaults for production; keep `http2=False` unless the `--http2` run is at least as
clean and faster.

## 7. Reproduce

```bash
python bench/bench_parser.py                 # Section 18
python bench/bench_db.py counters|batches|indexes   # Sections 21, 22, 25
bash bench/campaign.sh <baseline_code_dir> <template_db>   # tables A–D
python bench/make_report.py                  # regenerate tables from bench/results/*.json
```
