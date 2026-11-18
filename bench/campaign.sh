#!/usr/bin/env bash
# Full benchmark campaign. Usage: bash bench/campaign.sh <baseline_code_dir> <template_db_path>
set -u
BASE_DIR="$1"; TPL="$2"; R=bench/results; mkdir -p $R
run() { echo "=== $(date -u +%H:%M:%S) $*"; python bench/run_e2e.py "$@" > /dev/null 2>"$R/last.err" || { echo FAILED; tail -5 "$R/last.err"; }; }
# A) real-run DB scale: 3.5M relationships already persisted (~75% of the real 4.33M run)
run --engine baseline  --code-dir "$BASE_DIR" --sources 800 --rps 8 --prefill-rels 3500000 --template-db "$TPL" --out $R/A_scale_baseline.json
run --engine optimized --code-dir .           --sources 800 --rps 8 --prefill-rels 3500000 --template-db "$TPL" --out $R/A_scale_optimized.json
# B) fresh DB, same fixed budget
run --engine baseline  --code-dir "$BASE_DIR" --sources 3000 --rps 8 --out $R/B_fresh_baseline.json
run --engine optimized --code-dir .           --sources 3000 --rps 8 --out $R/B_fresh_optimized.json
# C) RPS matrix vs simulated Akamai model (blocks when >15 req/s sustained over 10s); stop after first WAF level
for rps in 6 8 10 12 15 18 20; do
  run --engine optimized --code-dir . --sources 1200 --rps $rps --waf-rps 15 --waf-block 20 --out $R/C_matrix_${rps}.json
  blocks=$(python -c "import json;print(json.load(open('$R/C_matrix_${rps}.json'))['waf_blocks'])")
  if [ "$blocks" != "0" ]; then echo "WAF observed at $rps rps -> stop increasing"; break; fi
done
# D) adaptive ramp: start 8, ceiling 20, +0.25 per 300 healthy requests, same Akamai model
run --engine optimized --code-dir . --sources 3000 --rps 8 --max-rps 20 --rps-window 300 --waf-rps 15 --waf-block 20 --out $R/D_adaptive.json
echo "=== DONE $(date -u +%H:%M:%S)"
