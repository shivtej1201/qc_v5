#!/usr/bin/env bash
# Section 38 concurrency matrix: 1,000 pages per cell, fixed 14 RPS primary budget,
# 800 ms page latency, Akamai model = 15 req/s over 10 s OR >=32 primary requests in flight.
R=bench/results/matrix; mkdir -p $R
for sc in 10 20 30 40; do for dc in 10 20 30 40 60; do
  echo "=== $(date -u +%H:%M:%S) sc=$sc dc=$dc"
  python bench/run_10k.py --sources 1000 --rps 14 --sc $sc --dc $dc --ic 30 --latency-ms 800 \
      --waf-rps 15 --waf-conc 32 --origin-knee 24 --port 8831 --out $R/sc${sc}_dc${dc}.json > /dev/null 2>$R/last.err \
      || { echo FAILED; tail -3 $R/last.err; }
done; done
echo "=== DONE $(date -u +%H:%M:%S)"
