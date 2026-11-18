"""
10K-source URL + image audit benchmark on the simulated edge (sources on the primary host,
links mostly to other sources, images on a separate CDN host, external links on a 3rd host).

  python bench/run_10k.py --sources 10000 --rps 12 --sc 10 --dc 20 --ic 30 --out bench/results/10k.json
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def wait_port(host, port, t=30):
    end = time.time() + t
    while time.time() < end:
        try:
            socket.create_connection((host, port), 0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{host}:{port} did not start")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sources", type=int, default=10000)
    p.add_argument("--rps", type=float, default=12.0)
    p.add_argument("--max-rps", type=float, default=0.0)
    p.add_argument("--sc", type=int, default=10)
    p.add_argument("--dc", type=int, default=20)
    p.add_argument("--ic", type=int, default=30)
    p.add_argument("--per-host", type=int, default=30)
    p.add_argument("--latency-ms", type=float, default=300.0)
    p.add_argument("--cdn-latency-ms", type=float, default=80.0)
    p.add_argument("--waf-rps", type=float, default=15.0)
    p.add_argument("--waf-conc", type=int, default=32)
    p.add_argument("--origin-knee", type=int, default=24)
    p.add_argument("--waf-block", type=float, default=20.0)
    p.add_argument("--images-on-primary", action="store_true", help="charge the CDN host to the primary budget")
    p.add_argument("--port", type=int, default=8821)
    p.add_argument("--keep", default="", help="copy the ZIP + summary here")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    a.out = os.path.abspath(a.out)

    work = tempfile.mkdtemp(prefix="qc_10k_")
    sim = subprocess.Popen([sys.executable, os.path.join(HERE, "sim_edge.py"), "--sources", str(a.sources),
                            "--port", str(a.port), "--latency-ms", str(a.latency_ms), "--images",
                            "--cdn-latency-ms", str(a.cdn_latency_ms), "--waf-rps", str(a.waf_rps),
                            "--waf-conc", str(a.waf_conc), "--origin-knee", str(a.origin_knee),
                            "--waf-block", str(a.waf_block)])
    try:
        for h in ("127.0.0.1", "127.0.0.2", "127.0.0.3"):
            wait_port(h, a.port)
        base = f"http://127.0.0.1:{a.port}"
        urls = os.path.join(work, "sitemap_urls.txt")
        with open(urls, "w") as f:
            f.write("\n".join(f"{base}/p/{i}" for i in range(a.sources)))
        os.chdir(ROOT)
        import run_on_page_audit as cli
        args = cli.parse_args([
            "--urls", urls, "--db", os.path.join(work, "audit.db"), "--job-id", "BENCH10K",
            "--out-dir", os.path.join(work, "out"), "--package-name", "QC_V5_10K_AUDIT",
            "--primary-domain", "127.0.0.1", "--allow-local", "--probe-url", f"{base}/robots.txt",
            "--target-rps", str(a.rps), "--min-rps", str(min(3.0, a.rps)), "--max-rps", str(a.max_rps or a.rps),
            "--source-concurrency", str(a.sc), "--dest-concurrency", str(a.dc), "--image-concurrency", str(a.ic),
            "--per-host-concurrency", str(a.per_host), "--akamai-cooldown-sec", str(a.waf_block),
            "--read-timeout", "15", "--max-retries", "1",
        ] + (["--primary-hosts", "127.0.0.3"] if a.images_on_primary else []))
        t0 = time.time()
        res = cli.run(args)
        total = time.time() - t0
        sim_stats = json.loads(urllib.request.urlopen(f"{base}/__stats", timeout=10).read())
        out = {
            "config": vars(a), "total_seconds_incl_package": round(total, 1),
            "crawl_seconds": res["crawl_seconds"], "package_seconds": res.get("package_seconds"),
            "final_qc_status": res.get("final_qc_status"), "performance": res["performance"],
            "stats": res["stats"], "package": res.get("package"), "server": sim_stats,
        }
        with open(a.out, "w") as f:
            json.dump(out, f, indent=2, default=str)
        if a.keep and res.get("package"):
            os.makedirs(a.keep, exist_ok=True)
            shutil.copy(res["package"]["zip_path"], a.keep)
        perf = res["performance"]
        print(json.dumps({
            "final_qc_status": res.get("final_qc_status"), "crawl_seconds": res["crawl_seconds"],
            "package_seconds": res.get("package_seconds"), "pages_per_sec": perf.get("source_pages_per_sec"),
            "primary_rps": perf.get("achieved_primary_rps"), "total_rps": perf.get("total_requests_per_sec"),
            "url_val": perf.get("dest_validations_http"), "img_val": perf.get("image_validations_http"),
            "waf_events": perf.get("waf_events"), "403": perf.get("status_403"), "429": perf.get("status_429"),
            "timeouts": perf.get("timeouts"), "cooldowns": perf.get("akamai_cooldowns"),
            "rss_peak_mb": perf.get("rss_peak_mb"), "server_kind": sim_stats["kind"],
            "server_dups": sim_stats["duplicate_paths"], "max_inflight": sim_stats.get("max_primary_inflight"),
        }, indent=1, default=str))
    finally:
        sim.terminate()
        sim.wait()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
