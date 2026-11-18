"""
LIVE Akamai edge RPS matrix (Section 29) — run from the network QC V5 normally runs from.

It drives the real optimized OnPageAuditEngine (same pacing, WAF detection, cooldown, robots,
SSRF) against a sample of real sitemap URLs at one fixed RPS per level, for a SUSTAINED window,
and STOPS climbing as soon as WAF / 429 / cooldown behaviour appears. Each level uses its own
throwaway SQLite DB; nothing touches the production audit DB.

    python bench/live_edge_matrix.py --urls sitemap_urls.txt --levels 6,8,10,12,15,18,20 \
        --window-min 10 --out live_matrix.json [--http2]

Run once with and once without --http2 at the chosen level to decide HTTP/2 on real edge data.
Safety: levels are tried in ascending order only; a level is "unstable" if it produced any
Akamai cooldown, any WAF-classified response, or 429s above --max-429-pct. The script stops at
the first unstable level and recommends the highest stable level minus one step of headroom.
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from on_page_audit_engine import OnPageAuditEngine  # noqa: E402
from url_normalizer import URLNormalizer  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls", required=True, help="file with one sitemap source URL per line")
    p.add_argument("--levels", default="6,8,10,12,15,18,20")
    p.add_argument("--window-min", type=float, default=10.0, help="sustained window per level (minutes)")
    p.add_argument("--primary-domain", default="bajajfinserv.in")
    p.add_argument("--source-concurrency", type=int, default=10)
    p.add_argument("--dest-concurrency", type=int, default=20)
    p.add_argument("--max-429-pct", type=float, default=0.5)
    p.add_argument("--http2", action="store_true")
    p.add_argument("--out", default="live_matrix.json")
    a = p.parse_args()

    urls = [u.strip() for u in open(a.urls, encoding="utf-8") if u.strip().startswith("http")]
    offset = 0
    results = []
    for rps in [float(x) for x in a.levels.split(",")]:
        n = int(rps * a.window_min * 60 * 0.9)  # ~90% of the window is source pages, rest destinations
        batch = urls[offset: offset + n]
        offset += n
        if len(batch) < n:
            print(f"[live] not enough unused URLs for a sustained {a.window_min} min window at {rps} rps; stopping")
            break
        work = tempfile.mkdtemp(prefix="qc_live_")
        eng = OnPageAuditEngine(
            db_path=os.path.join(work, "live.db"), job_id=f"live_{rps}", primary_domain=a.primary_domain,
            target_rps=rps, min_rps=min(3.0, rps), max_rps=rps,  # fixed level: measure, don't adapt
            source_concurrency=a.source_concurrency, dest_concurrency=a.dest_concurrency, http2=a.http2,
        )
        eng.initialize_inventory([{"original_url": u, "normalized_url": URLNormalizer.normalize(u)} for u in batch])
        t0 = time.time()
        st = asyncio.run(eng.run())
        eng.close()
        perf = st["performance"]
        req = max(1, perf["http_requests_total"])
        row = {
            "rps_target": rps, "http2": a.http2, "wall_sec": round(time.time() - t0, 1),
            "source_pages": len(batch), "source_pages_per_sec": round(len(batch) / (time.time() - t0), 2),
            "unique_destinations": st["unique_http_destinations"], "dest_validations_http": perf["dest_validations_http"],
            "reused_validations": st["reused_http_validations"],
            "achieved_primary_rps": perf["achieved_primary_rps"], "latency_avg_ms": perf["latency_avg_ms"],
            "latency_p95_ms": perf["latency_p95_ms"], "status_403": perf["status_403"], "status_429": perf["status_429"],
            "waf_events": perf["waf_events"], "cooldowns": perf["akamai_cooldowns"], "timeouts": perf["timeouts"],
            "network_errors": perf["network_errors"], "cpu_avg_pct": perf["cpu_avg_pct"], "rss_peak_mb": perf["rss_peak_mb"],
            "db_flush_p95_ms": perf["db_flush_p95_ms"], "write_q_peak": perf["write_q_peak"],
        }
        row["stable"] = row["cooldowns"] == 0 and row["waf_events"] == 0 and 100.0 * row["status_429"] / req <= a.max_429_pct
        results.append(row)
        print(json.dumps(row), flush=True)
        shutil.rmtree(work, ignore_errors=True)
        if not row["stable"]:
            print(f"[live] WAF/rate-limit behaviour at {rps} rps -> stop increasing", flush=True)
            break
        time.sleep(120)  # let the edge window reset between levels

    stable = [r["rps_target"] for r in results if r["stable"]]
    rec = None
    if stable:
        rec = stable[-2] if len(stable) >= 2 and not results[-1]["stable"] else stable[-1]
    out = {"levels": results, "highest_stable_rps": stable[-1] if stable else None,
           "recommended_target_rps": rec, "recommended_max_rps": stable[-1] if stable else None}
    json.dump(out, open(a.out, "w"), indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
