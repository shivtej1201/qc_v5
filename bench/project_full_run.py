"""PROJECTION (not a measurement): full 124,474-source run time for baseline vs optimized, from
measured components: the sync_job_counters() cost curve (bench_db.py) and the fixed RPS budget.
Baseline writer runs flush+sync every 50 pages (Stage A) / 100 dests (Stage B); fetchers can run
ahead only by the 1,000-packet result queue, so sustained throughput is min(budget, writer)."""
import sys
PTS = [(0, 0.0), (250_042, 0.2141), (1_000_163, 1.9638), (2_000_313, 4.548), (3_000_482, 7.8302), (4_350_700, 11.7512)]
def sync_cost(r):
    for (r0, c0), (r1, c1) in zip(PTS, PTS[1:]):
        if r <= r1:
            return c0 + (c1 - c0) * (r - r0) / (r1 - r0)
    (r0, c0), (r1, c1) = PTS[-2], PTS[-1]
    return c1 + (c1 - c0) * (r - r1) / (r1 - r0)
def project(rps, sources=124_474, rels_per_page=34.8, extra_dests=5_918, flush_s=0.08):
    # baseline: Stage A in 50-page batches
    t, rels = 0.0, 0.0
    for _ in range(sources // 50):
        rels += 50 * rels_per_page
        t += max(50 / rps, sync_cost(rels) + flush_s)
    # Stage B: non-source destinations, sync every 100 dests at final table size
    for _ in range(extra_dests // 100):
        t += max(100 / rps, sync_cost(rels) + flush_s)
    base = t + 2 * sync_cost(rels)
    opt = (sources + extra_dests) / rps + 2 * sync_cost(rels)  # start-of-run + final reconciliation
    return base, opt
if __name__ == "__main__":
    for rps in [float(x) for x in (sys.argv[1:] or ["8", "10", "12"])]:
        b, o = project(rps)
        print(f"rps={rps:>4}: baseline ≈ {b/3600:5.2f} h | optimized ≈ {o/3600:5.2f} h | saving ≈ {(b-o)/3600:4.2f} h ({(b-o)/b*100:.0f}%)")
