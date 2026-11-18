"""Build docs/performance/ON_PAGE_AUDIT_PERFORMANCE.md tables from bench/results/*.json (measured data only)."""
import glob
import json
import os
import sys

R = sys.argv[1] if len(sys.argv) > 1 else "bench/results"


def load(n):
    p = os.path.join(R, n + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


def row(label, b, o, key, fmt="{:,}", better=None):
    bv = b.get(key) if b else None
    ov = o.get(key) if o else None
    f = lambda v: "—" if v is None else (fmt.format(v) if isinstance(v, (int, float)) else str(v))
    delta = ""
    if isinstance(bv, (int, float)) and isinstance(ov, (int, float)) and bv:
        delta = f"{(ov - bv) / bv * 100:+.0f}%"
    return f"| {label} | {f(bv)} | {f(ov)} | {delta} |"


def compare(title, b, o):
    out = [f"### {title}", "", "| Metric | Baseline | Optimized | Δ |", "|---|---:|---:|---:|"]
    for label, key, fmt in [
        ("Wall-clock (s)", "wall_sec", "{:,.1f}"),
        ("Source pages/sec (incl. start/finish)", "source_pages_per_sec", "{:,.2f}"),
        ("Relationships persisted/sec", "relationships_per_sec", "{:,.1f}"),
        ("Achieved primary RPS (server-side)", "achieved_primary_rps", "{:,.2f}"),
        ("Max primary requests in any 1 s (server-side)", "max_primary_req_in_any_1s", "{:,}"),
        ("Primary requests (server-side)", "primary_requests", "{:,}"),
        ("Server requests total", "server_total_requests", "{:,}"),
        ("Unique HTTP destinations", "unique_http_destinations", "{:,}"),
        ("NEW_CHECK / REUSED", None, None),
        ("Paths requested >1× (server-side)", "duplicate_paths", "{:,}"),
        ("Peak RSS (MB)", "rss_peak_mb", "{:,.1f}"),
        ("CPU avg % (1 core = 100)", "cpu_avg_pct", "{:,.1f}"),
        ("Job status", "status", "{}"),
    ]:
        if key is None:
            fb = f"{b['new_http_validations']:,} / {b['reused_http_validations']:,}" if b else "—"
            fo = f"{o['new_http_validations']:,} / {o['reused_http_validations']:,}" if o else "—"
            out.append(f"| {label} | {fb} | {fo} | |")
        else:
            out.append(row(label, b, o, key, fmt))
    return "\n".join(out)


def main():
    parts = []
    a_b, a_o = load("A_scale_baseline_final") or load("A_scale_baseline"), load("A_scale_optimized_final") or load("A_scale_optimized")
    parts.append(compare("A. Real-run DB scale (3.5M relationships already persisted, 800 live sources, 8 RPS budget)", a_b, a_o))
    b_b, b_o = load("B_fresh_baseline"), load("B_fresh_optimized")
    parts.append(compare("B. Fresh DB (3,000 sources, 8 RPS budget)", b_b, b_o))
    for name, r in (("A optimized", a_o), ("B optimized", b_o)):
        if r and r.get("engine_performance"):
            p = r["engine_performance"]
            parts.append(
                f"**{name} engine telemetry:** source-seeded validations {p['source_seeded_validations']:,}, "
                f"destination HTTP validations {p['dest_validations_http']:,}, DB flush avg {p['db_flush_avg_ms']} ms / "
                f"p95 {p['db_flush_p95_ms']} ms, DB rows/s {p['db_writes_per_sec']}, write-queue peak {p['write_q_peak']}, "
                f"dest-queue peak {p['dest_q_peak']}, parse-queue peak {p['parse_q_peak']}, request latency avg "
                f"{p['latency_avg_ms']} ms / p95 {p['latency_p95_ms']} ms, timeouts {p['timeouts']}, 403 {p['status_403']}, "
                f"429 {p['status_429']}, cooldowns {p['akamai_cooldowns']}, counter reconciliation "
                f"{'PASS' if (r.get('counter_reconciliation') or {}).get('passed') else 'FAIL'}."
            )
    mats = sorted((json.load(open(f)) for f in glob.glob(os.path.join(R, "C_matrix_*.json"))), key=lambda x: x["rps_target"])
    if mats:
        t = ["### C. Fixed-RPS matrix vs simulated Akamai model (blocks when >15 req/s sustained over 10 s)", "",
             "| Target RPS | Achieved primary RPS | Source pg/s | Dest HTTP validations | Wall (s) | 403 served | WAF events | Cooldowns | 429 | Timeouts | Lat avg/p95 (ms) | DB flush p95 (ms) | CPU % | RSS MB | Result |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for m in mats:
            p = m["engine_performance"]
            ok = m["waf_blocks"] == 0
            t.append(f"| {m['rps_target']:g} | {m['achieved_primary_rps']} | {m['source_pages_per_sec']} | {p['dest_validations_http']:,} | "
                     f"{m['wall_sec']} | {m['waf_403_served']} | {p['waf_events']} | {p['akamai_cooldowns']} | {p['status_429']} | "
                     f"{p['timeouts']} | {p['latency_avg_ms']}/{p['latency_p95_ms']} | {p['db_flush_p95_ms']} | {m['cpu_avg_pct']} | "
                     f"{m['rss_peak_mb']} | {'stable' if ok else 'WAF → stop'} |")
        parts.append("\n".join(t))
    d = load("D_adaptive")
    if d:
        p = d["engine_performance"]
        hist = "; ".join(f"t={h[0]}s {h[1]}→{h[2]} ({h[3]})" for h in p["rps_history"][-12:])
        parts.append(
            "### D. Adaptive controller (start 8 RPS, ceiling 20, +0.25 per 300 healthy responses, same Akamai model)\n\n"
            f"Wall {d['wall_sec']} s for 3,000 sources ({d['source_pages_per_sec']} pg/s), achieved primary RPS "
            f"{d['achieved_primary_rps']}, final target {d['target_rps_final']}, cooldowns {d['cooldowns']}, 403 served "
            f"{d['waf_403_served']}, WAF blocks {d['waf_blocks']}, status {d['status']}, counter reconciliation "
            f"{'PASS' if (d.get('counter_reconciliation') or {}).get('passed') else 'FAIL'}.\n\nRate history (last events): {hist}"
        )
    print("\n\n".join(parts))


if __name__ == "__main__":
    main()
