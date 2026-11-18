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
