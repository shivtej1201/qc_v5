"""Section 18 parser benchmark: inline vs asyncio.to_thread vs bounded pools.
Measures parse throughput AND event-loop lag (what the rate pacer / writer feel)."""
import asyncio, os, sys, time, statistics, concurrent.futures as cf
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sim_site import page_html
from qcv5.crawler.on_page_audit_engine import extract_on_page_urls

BASE = "http://www.qcbench.test"
PAGES = [page_html(i, 20000, BASE, "http://ext.example.org") for i in range(200)]

def parse(i):
    h = PAGES[i % len(PAGES)]
    raw, rels, p = extract_on_page_urls(BASE + f"/p/{i}", BASE + f"/p/{i}", h)
    return len(rels)

async def lag_probe(stop, lags):
    while not stop.is_set():
        t = time.perf_counter(); await asyncio.sleep(0.005)
        lags.append((time.perf_counter() - t - 0.005) * 1000)

async def run(mode, n=400, conc=10):
    loop = asyncio.get_running_loop()
    ex = None
    if mode.startswith("pool"):
        ex = cf.ThreadPoolExecutor(int(mode[4:]))
    if mode.startswith("proc"):
        ex = cf.ProcessPoolExecutor(int(mode[4:]))
        await asyncio.gather(*[loop.run_in_executor(ex, parse, i) for i in range(8)])
    q = asyncio.Queue()
    for i in range(n): q.put_nowait(i)
    stop = asyncio.Event(); lags = []
    probe = asyncio.create_task(lag_probe(stop, lags))
    async def worker():
        while not q.empty():
            i = q.get_nowait()
            if mode == "inline": parse(i)
            elif mode == "to_thread": await asyncio.to_thread(parse, i)
            else: await loop.run_in_executor(ex, parse, i)
            await asyncio.sleep(0)
    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(conc)])
    dt = time.perf_counter() - t0
    stop.set(); await probe
    if ex: ex.shutdown()
    lags.sort()
    return n / dt, statistics.mean(lags) if lags else 0, lags[int(len(lags)*0.99)-1] if lags else 0, max(lags) if lags else 0

if __name__ == "__main__":
    t = time.perf_counter(); [parse(i) for i in range(100)]
    print(f"single-thread parse cost: {(time.perf_counter()-t)*10:.1f} ms/page (page={len(PAGES[0])//1024} KB)")
    for mode in ["inline", "to_thread", "pool1", "pool2", "proc2", "proc3"]:
        pps, mean_lag, p99, mx = asyncio.run(run(mode))
        print(f"{mode:10s} {pps:7.1f} pages/s | loop lag mean {mean_lag:6.2f} ms p99 {p99:7.2f} ms max {mx:7.2f} ms")
