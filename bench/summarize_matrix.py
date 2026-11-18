"""Render the Section 38 concurrency matrix (bench/results/matrix/*.json) as a markdown table."""
import glob
import json
import os
import re
import sys

R = sys.argv[1] if len(sys.argv) > 1 else "bench/results/matrix"
rows = []
for f in glob.glob(os.path.join(R, "sc*_dc*.json")):
    m = re.search(r"sc(\d+)_dc(\d+)", f)
    d = json.load(open(f))
    p = d["performance"]
    s = d["server"]
    rows.append((int(m.group(1)), int(m.group(2)), d, p, s))
rows.sort(key=lambda r: (r[0], r[1]))
print("| Source conc | Dest conc | Crawl (s) | Pages/s | URL val/s | Image val/s | Total req/s | Primary req/s | "
      "Max in-flight (server) | CPU % | Peak RSS MB | DB flush p95 ms | Write-q peak | Dest-q peak | WAF | 429 | 403 | "
      "Timeouts | Conn errors | QC |")
print("|" + "---:|" * 19 + "---|")
for sc, dc, d, p, s in rows:
    print(f"| {sc} | {dc} | {d['crawl_seconds']} | {p.get('source_pages_per_sec')} | {p.get('url_validations_per_sec')} | "
          f"{p.get('image_validations_per_sec')} | {p.get('total_requests_per_sec')} | {p.get('achieved_primary_rps')} | "
          f"{s.get('max_primary_inflight')} | {p.get('cpu_avg_pct')} | {p.get('rss_peak_mb')} | {p.get('db_flush_p95_ms')} | "
          f"{p.get('write_q_peak')} | {p.get('dest_q_peak')} | {p.get('waf_events')} | {p.get('status_429')} | "
          f"{p.get('status_403')} | {p.get('timeouts')} | {p.get('network_errors')} | {d.get('final_qc_status')} |")
