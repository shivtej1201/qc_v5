"""
URL-status audit at 125K scale — acceptance tests for the user story
("process and properly validate up to 125,000 URLs without missing, duplicating or
misclassifying any").

All tests drive the REAL pipeline: CSV -> JobManager ingest (per-row ledger) -> BatchWorkerDaemon
worker loops -> URLChecker -> SQLite, against a local target server running in its own process
(bench/url_status_sim.py) that counts every request it receives (server-side truth).

Run the 125K test alone:  python -m pytest -q test_url_status_large_volume.py -k 125k -s
Skip it:                  QC_SKIP_125K=1 python -m pytest -q test_url_status_large_volume.py
"""
import asyncio
import csv
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import httpx
import psutil
import pytest

from browser import BrowserPool
from checker import URLChecker
from config import settings
from daemon import BatchWorkerDaemon
from db import Database
from job_manager import JobManager
from report_generator import ReportGenerator

HERE = os.path.dirname(os.path.abspath(__file__))
TERMINAL = ("COMPLETED", "PARTIAL", "FAILED", "CANCELLED")


# ----------------------------------------------------------------------------------- fixtures
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Target:
    def __init__(self):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "bench", "url_status_sim.py"),
                                      "--port", str(self.port), "--slow-sec", "5"])
        end = time.time() + 20
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.3).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("target server did not start")

    def hits(self):
        return json.loads(urllib.request.urlopen(f"{self.base}/__hits", timeout=10).read())

    def stop(self):
        self.proc.terminate()
        self.proc.wait()


@pytest.fixture(scope="module")
def target():
    t = Target()
    yield t
    t.stop()


@pytest.fixture()
def workdir():
    saved = {k: getattr(settings, k) for k in (
        "ALLOW_LOCAL_TESTING", "BROWSER_FALLBACK", "CONNECT_TIMEOUT", "READ_TIMEOUT", "MAX_RETRIES",
        "BATCH_SIZE", "HTTP_CONCURRENCY", "CACHE_ENABLED", "BATCH_LEASE_DURATION_SEC")}
    settings.ALLOW_LOCAL_TESTING = True      # target server is on 127.0.0.1
    settings.BROWSER_FALLBACK = False        # HTTP validation only (no Playwright in these tests)
    settings.CONNECT_TIMEOUT = 1.0
    settings.READ_TIMEOUT = 1.5
    settings.MAX_RETRIES = 1
    settings.CACHE_ENABLED = True
    cwd = os.getcwd()
    d = tempfile.mkdtemp(prefix="qc_urlstatus_")
    os.chdir(d)
    try:
        yield d
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
        for k, v in saved.items():
            setattr(settings, k, v)


# ----------------------------------------------------------------------------------- helpers
async def open_env():
    db = Database("audit.db")
    await db.connect()
    return db, JobManager(db)


async def submit(jm, rows, header="url", name="urls.csv"):
    with open(name, "w", encoding="utf-8", newline="") as f:
        if header:
            f.write(header + "\n")
        for r in rows:
            f.write(r + "\n")
    sheets = await jm.parse_sheets(name)
    upload_id = f"up_{random.randrange(10**9)}"
    await jm.create_upload(upload_id, name, "url_status")
    jobs = await jm.create_jobs_for_sheets(upload_id, name, "url_status", ["CSV Data"])
    assert len(jobs) == 1
    return jobs[0]["id"], sheets


def new_daemon(db, jm, workers=4):
    d = BatchWorkerDaemon(db=db, jm=jm, worker_count=workers)
    d.checker = URLChecker(BrowserPool())   # browser pool never started (fallback disabled)
    return d


async def job_row(db, job_id):
    async with db.conn.execute("SELECT status, total_records, processed_records FROM jobs WHERE id = ?", (job_id,)) as c:
        return await c.fetchone()


async def run_daemon(db, d, job_id, timeout=600, stop_when=None, progress=None):
    """Run the daemon's real worker loops until the job is terminal (or stop_when(counts) is true,
    in which case the loops are killed abruptly, like a process crash)."""
    tasks = [asyncio.create_task(d._worker_loop(i)) for i in range(d.worker_count)]
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            await asyncio.sleep(0.25)
            counts = await db.get_url_status_counts(job_id)
            if progress is not None:
                progress.append((counts["processed_urls"], counts["pending_urls"], counts["total_urls"],
                                 (await job_row(db, job_id))[2]))
            if stop_when and stop_when(counts):
                return "KILLED"
            st = (await job_row(db, job_id))[0]
            if st in TERMINAL:
                return st
        raise AssertionError("job did not finish in time")
    finally:
        d.active = False
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def items(db, job_id):
    return [r async for r in db.iter_job_url_items(job_id)]


# ----------------------------------------------------------------------------------- tests
def test_every_outcome_class_recorded_with_reason(target, workdir):
    """AC3/AC6: 200, 3xx, 4xx, 5xx, blocked, timeout, connection, DNS, SSL, redirect loop, SSRF,
    invalid -> each row has a final outcome or an explicit reason; retries happen; nothing dropped."""
    b = target.base
    rows = [
        f"{b}/ok/1",                      # 1  OK
        f"{b}/r/7",                       # 2  REDIRECT -> 200
        f"{b}/rb/1",                      # 3  REDIRECT -> 404
        f"{b}/nf/1",                      # 4  CLIENT_ERROR 404
        f"{b}/fb/1",                      # 5  BLOCKED 403
        f"{b}/rl/1",                      # 6  BLOCKED 429 (retried)
        f"{b}/se/1",                      # 7  SERVER_ERROR 500
        f"{b}/flaky/1",                   # 8  503 then 200 -> OK after retry
        f"{b}/slow/1",                    # 9  TIMEOUT (retried)
        f"{b}/loop/1",                    # 10 REDIRECT_LOOP
        f"{b}/to-ssrf",                   # 11 redirect hop to metadata IP -> BLOCKED_SSRF
        "http://127.0.0.1:1/closed",      # 12 CONNECTION_ERROR
        f"https://127.0.0.1:{target.port}/ok/2",  # 13 SSL_ERROR (TLS to a plain-HTTP port)
        "http://does-not-exist.invalid/x",  # 14 DNS_ERROR
        "http://169.254.169.254/latest/meta-data",  # 15 BLOCKED_SSRF (never requested)
        "not a url",                      # 16 INVALID_URL
        "ftp://example.com/file.txt",     # 17 INVALID_URL
        f"{b}/ok/1",                      # 18 DUPLICATE of row 1
        f"{b}/ok/1#reviews",              # 19 DUPLICATE of row 1 (fragment-normalized)
        "NA",                             # 20 INVALID_URL (literal NA must not vanish)
    ]
    expected = {
        1: ("OK", 1, 200), 2: ("REDIRECT", 1, 200), 3: ("REDIRECT", 1, 404), 4: ("CLIENT_ERROR", 1, 404),
        5: ("BLOCKED", 1, 403), 6: ("BLOCKED", 1, 429), 7: ("SERVER_ERROR", 1, 500), 8: ("OK", 1, 200),
        9: ("TIMEOUT", 0, None), 10: ("REDIRECT_LOOP", 0, None), 11: ("BLOCKED_SSRF", 0, None),
        12: ("CONNECTION_ERROR", 0, None), 13: ("SSL_ERROR", 0, None), 14: ("DNS_ERROR", 0, None),
        15: ("BLOCKED_SSRF", 0, None), 16: ("INVALID_URL", 0, None), 17: ("INVALID_URL", 0, None),
        18: ("OK", 1, 200), 19: ("OK", 1, 200), 20: ("INVALID_URL", 0, None),
    }

    async def go():
        db, jm = await open_env()
        try:
            job_id, sheets = await submit(jm, rows)
            assert sheets == [{"name": "CSV Data", "count": len(rows)}]
            st = await run_daemon(db, new_daemon(db, jm), job_id, timeout=120)
            assert st == "COMPLETED"
            got = {r["row_no"]: r for r in await items(db, job_id)}
            assert sorted(got) == list(range(1, len(rows) + 1)), "every submitted row has exactly one record"
            for row_no, (oc, validated, code) in expected.items():
                r = got[row_no]
                assert r["item_state"] == "DONE", r
                assert (r["outcome"], r["validated"], r["final_status_code"]) == (oc, validated, code), (row_no, r)
                if not validated:
                    assert r["error_type"] and r["error_message"], f"row {row_no} lacks a reason: {r}"
                assert r["original_url"] == rows[row_no - 1].strip()
            assert got[18]["validation_source"] == "DUPLICATE" and got[18]["duplicate_of_row"] == 1
            assert got[19]["validation_source"] == "DUPLICATE" and got[19]["duplicate_of_row"] == 1
            assert got[8]["attempts"] == 2 and got[9]["attempts"] == 2, "transient failures are retried"
            cols = ReportGenerator.URL_STATUS_EXPORT_COLUMNS
            sev = lambda n: ReportGenerator.url_status_row(got[n], cols)[cols.index("severity")]
            act = lambda n: ReportGenerator.url_status_row(got[n], cols)[cols.index("recommended_action")]
            assert (sev(1), act(1)) == ("", "")                                   # healthy: no issue
            assert sev(2) == "LOW" and "final URL" in act(2)                       # 301 -> 200
            assert sev(3) == "HIGH" and sev(4) == "HIGH" and "404" in act(4)       # redirect to 404, 404
            assert sev(7) == "HIGH" and sev(9) == "MEDIUM" and sev(14) == "HIGH"   # 500, timeout, DNS
            assert sev(16) == "MEDIUM" and "valid http" in act(16)                 # invalid entry
            hits = target.hits()
            assert hits.get("/ok/1") == 1, "duplicates must not be re-validated"
            assert hits.get("/flaky/1") == 2 and hits.get("/rl/1") == 2 and hits.get("/se/1") == 1
            assert "/latest/meta-data" not in hits
            c = await db.get_url_status_counts(job_id)
            assert c["total_urls"] == 20 and c["processed_urls"] == 20 and c["pending_urls"] == 0
            assert c["duplicate_urls"] == 2 and c["invalid_urls"] == 3
            assert c["successfully_validated_urls"] + c["failed_validations"] == 20
            assert (await job_row(db, job_id))[1:] == (20, 20)
        finally:
            await db.close()

    asyncio.run(go())


def test_single_url_exception_is_isolated(target, workdir):
    """AC5: an unexpected exception for one URL is recorded for that URL; the job completes."""
    b = target.base
    rows = [f"{b}/ok/{100 + i}" for i in range(40)]

    async def go():
        db, jm = await open_env()
        try:
            job_id, _ = await submit(jm, rows)
            d = new_daemon(db, jm)
            real = d.checker.check

            async def flaky_check(url):
                if url.endswith("/ok/117"):
                    raise RuntimeError("parser exploded")
                return await real(url)

            d.checker.check = flaky_check
            assert await run_daemon(db, d, job_id, timeout=60) == "COMPLETED"
            got = {r["row_no"]: r for r in await items(db, job_id)}
            assert got[18]["outcome"] == "VALIDATION_ERROR" and "parser exploded" in got[18]["error_message"]
            assert all(r["outcome"] == "OK" for k, r in got.items() if k != 18)
        finally:
            await db.close()

    asyncio.run(go())


def test_persistence_failure_fails_batch_then_resume_completes(target, workdir):
    """AC5/AC10: a DB failure fails only that batch, leaves its rows PENDING (never falsely
    'processed'), the job is PARTIAL, and resuming completes it."""
    settings.BATCH_SIZE = 25
    b = target.base
    rows = [f"{b}/ok/{300 + i}" for i in range(100)]   # 4 batches

    async def go():
        db, jm = await open_env()
        try:
            job_id, _ = await submit(jm, rows)
            real = db.record_url_item_results
            state = {"failed": False}

            async def failing_once(job, its, cache):
                if not state["failed"]:
                    state["failed"] = True
                    raise RuntimeError("disk full (simulated)")
                return await real(job, its, cache)

            db.record_url_item_results = failing_once
            assert await run_daemon(db, new_daemon(db, jm), job_id, timeout=60) == "PARTIAL"
            c = await db.get_url_status_counts(job_id)
            assert c["pending_urls"] == 25 and c["processed_urls"] == 75
            assert (await job_row(db, job_id))[2] == 75, "progress must not count unsaved results"

            db.record_url_item_results = real
            await jm.update_job_status(job_id, "QUEUED")       # user presses Resume
            assert await run_daemon(db, new_daemon(db, jm), job_id, timeout=60) == "COMPLETED"
            c = await db.get_url_status_counts(job_id)
            assert c["processed_urls"] == 100 and c["pending_urls"] == 0
            hits = target.hits()
            assert sum(v for k, v in hits.items() if k.startswith("/ok/3")) <= 125  # only the failed batch re-run
        finally:
            await db.close()

    asyncio.run(go())


def test_interrupt_then_resume_does_not_revalidate(target, workdir):
    """AC8: kill the workers mid-job (crash), recover, resume: every URL that already has a
    persisted result is NOT requested again; no URL is lost; counts exact."""
    settings.BATCH_SIZE = 250
    b = target.base
    n = 3000
    rows = [f"{b}/ok/{10_000 + i}" for i in range(n)]

    async def go():
        db, jm = await open_env()
        try:
            job_id, _ = await submit(jm, rows)
            d1 = new_daemon(db, jm, workers=3)
            st = await run_daemon(db, d1, job_id, timeout=120, stop_when=lambda c: c["processed_urls"] >= 1200)
            assert st == "KILLED"
            done_before = {r["original_url"] for r in await items(db, job_id) if r["item_state"] == "DONE"}
            assert 1200 <= len(done_before) < n

            # process restart: leases of the dead worker are reclaimed exactly like daemon.start() does
            await db.reset_running_batches(d1.worker_id)
            d2 = new_daemon(db, jm, workers=3)
            assert await run_daemon(db, d2, job_id, timeout=120) == "COMPLETED"

            final = await items(db, job_id)
            assert len(final) == n and all(r["item_state"] == "DONE" and r["outcome"] == "OK" for r in final)
            hits = target.hits()
            for u in done_before:
                path = u[len(b):]
                assert hits.get(path) == 1, f"{path} was re-validated after resume ({hits.get(path)} requests)"
            extra = sum(hits.get(f"/ok/{10_000 + i}", 0) for i in range(n)) - n
            # only requests that were in flight (not yet persisted) at the kill may repeat
            assert 0 <= extra <= settings.HTTP_CONCURRENCY * 3
            c = await db.get_url_status_counts(job_id)
            assert (c["total_urls"], c["processed_urls"], c["pending_urls"]) == (n, n, 0)
        finally:
            await db.close()

    asyncio.run(go())


def test_headerless_csv_keeps_first_url(workdir):
    """AC10 at ingestion: a CSV without a header row must not swallow the first URL."""
    async def go():
        db, jm = await open_env()
        try:
            job_id, sheets = await submit(jm, ["https://a.example/1", "https://a.example/2", "NA"], header=None)
            assert sheets == [{"name": "CSV Data", "count": 3}]
            got = await items(db, job_id)
            assert [r["original_url"] for r in got] == ["https://a.example/1", "https://a.example/2", "NA"]
            assert got[2]["outcome"] == "INVALID_URL" and got[2]["item_state"] == "DONE"
        finally:
            await db.close()

    asyncio.run(go())


@pytest.mark.skipif(os.environ.get("QC_SKIP_125K") == "1", reason="QC_SKIP_125K=1")
def test_125k_urls_end_to_end(workdir):
    """
    AC1-AC10 at full scale: 125,000 submitted rows (incl. 12,000 duplicates, 300 invalid entries,
    404/301/500 mixes) -> accepted without truncation, every unique URL requested exactly once,
    every row has one result record, progress exact and monotonic, export = one row per URL.
    """
    settings.BATCH_SIZE = 1000
    settings.HTTP_CONCURRENCY = 200
    target = Target()   # dedicated server: server-side hit counts must not include other tests' requests
    b = target.base
    rnd = random.Random(42)
    unique = ([f"{b}/ok/{200_000 + i}" for i in range(108_200)]
              + [f"{b}/nf/{i}" for i in range(2_000)]
              + [f"{b}/r/{900_000 + i}" for i in range(1_500)]       # redirect targets /ok/900000+ are unique
              + [f"{b}/se/{i}" for i in range(1_000)])
    invalid = [f"not-a-url-{i}" for i in range(300)]
    dups = [rnd.choice(unique) for _ in range(12_000)]
    rows = unique + invalid + dups
    rnd.shuffle(rows)
    assert len(rows) == 125_000

    async def go():
        db, jm = await open_env()
        rss = {"peak": 0.0}
        stop = threading.Event()

        def sample():
            p = psutil.Process()
            while not stop.is_set():
                rss["peak"] = max(rss["peak"], p.memory_info().rss / 1048576)
                time.sleep(0.5)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        try:
            t0 = time.time()
            job_id, sheets = await submit(jm, rows)
            t_ingest = time.time() - t0
            assert sheets == [{"name": "CSV Data", "count": 125_000}]                       # AC1
            assert (await job_row(db, job_id))[:2] == ("QUEUED", 125_000)
            c0 = await db.get_url_status_counts(job_id)
            assert c0["total_urls"] == 125_000 and c0["duplicate_urls"] == 12_000             # AC4
            assert c0["unique_urls"] == 113_000 and c0["pending_urls"] == 125_000 - 300      # AC2

            progress = []
            t1 = time.time()
            st = await run_daemon(db, new_daemon(db, jm, workers=8), job_id, timeout=1800, progress=progress)
            t_run = time.time() - t1
            assert st == "COMPLETED"

            c = await db.get_url_status_counts(job_id)
            assert (c["total_urls"], c["processed_urls"], c["pending_urls"]) == (125_000, 125_000, 0)  # AC3/AC10
            assert c["duplicate_urls"] == 12_000 and c["invalid_urls"] == 300
            assert sum(c["by_outcome"].values()) == 125_000
            uniq_outcomes = {}
            all_items = await items(db, job_id)
            for r in all_items:
                if r["validation_source"] != "DUPLICATE":
                    uniq_outcomes[r["outcome"]] = uniq_outcomes.get(r["outcome"], 0) + 1
            assert uniq_outcomes == {"OK": 108_200, "CLIENT_ERROR": 2_000, "REDIRECT": 1_500,
                                     "SERVER_ERROR": 1_000, "INVALID_URL": 300}                 # AC6
            # AC7: progress never went backwards, processed+pending == total at every sample,
            # and the job row agrees with the ledger at the end
            procs = [p[0] for p in progress]
            assert procs == sorted(procs)
            bad = [p for p in progress if not (p[0] + p[1] == p[2] == 125_000)]
            assert not bad, f"{len(bad)}/{len(progress)} inconsistent progress samples, e.g. {bad[:5]}"
            assert (await job_row(db, job_id))[2] == 125_000

            hits = target.hits()                                                              # AC4 (server side)
            assert max(hits.values()) == 1, [k for k, v in hits.items() if v > 1][:5]
            assert sum(hits.values()) == 113_000 - 300 + 1_500   # unique HTTP URLs + one redirect hop each

            # AC9: export through the real API = one record per submitted URL, in order
            import server
            server.db = db
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t") as cl:
                r = await cl.get(f"/api/jobs/{job_id}/url-results.csv")
                assert r.status_code == 200
                exported = list(csv.reader(io.StringIO(r.text)))
                stats = (await cl.get(f"/api/jobs/{job_id}/stats")).json()
            assert len(exported) == 125_001
            hdr = exported[0]
            assert [row[hdr.index("original_url")] for row in exported[1:]] == [x.strip() for x in rows]
            assert stats["processed_records"] == stats["total_records"] == 125_000
            t2 = time.time()
            await ReportGenerator(db).generate_report(job_id, "url_status", "report.xlsx")
            t_report = time.time() - t2
            import openpyxl
            wb = openpyxl.load_workbook("report.xlsx", read_only=True)
            n_rows = sum(1 for _ in wb["URL Status"].iter_rows(values_only=True)) - 1
            wb.close()
            assert n_rows == 125_000
            print(f"\n[125K] ingest {t_ingest:.1f}s | validation {t_run:.1f}s ({113_000 / t_run:.0f} unique URL/s, "
                  f"{125_000 / t_run:.0f} rows/s) | xlsx report {t_report:.1f}s | peak RSS {rss['peak']:.0f} MB | "
                  f"requests {sum(hits.values()):,}")
            assert rss["peak"] < 1024, "memory must stay bounded at 125K"
        finally:
            stop.set()
            th.join()
            await db.close()

    try:
        asyncio.run(go())
    finally:
        target.stop()


def test_csv_chunk_with_leading_non_urls_loses_no_rows(workdir):
    """Regression: URL column must be detected once; a chunk that starts with non-URL cells used to
    end the whole file silently."""
    settings.BATCH_SIZE = 100
    rows = [f"https://a.example/{i}" for i in range(100)] + ["N/A"] * 6 + [f"https://a.example/x{i}" for i in range(250)]

    async def go():
        db, jm = await open_env()
        try:
            job_id, sheets = await submit(jm, rows, header=None)
            assert sheets == [{"name": "CSV Data", "count": len(rows)}]
            c = await db.get_url_status_counts(job_id)
            assert c["total_urls"] == len(rows) and c["invalid_urls"] == 6
        finally:
            await db.close()

    asyncio.run(go())


def test_stranded_duplicates_are_healed(workdir):
    """Regression: a crash between the canonical write and the duplicate copy (separate statements on
    a shared connection) must not leave duplicates PENDING forever."""
    async def go():
        db, jm = await open_env()
        try:
            job_id, _ = await submit(jm, ["https://a.example/1", "https://a.example/1", "https://a.example/1#x"])
            # simulate the crash window: canonical written, duplicates not
            await db.conn.execute(
                "UPDATE job_url_items SET item_state='DONE', outcome='OK', validated=1, final_status_code=200, "
                "status='Working', validation_source='NEW_CHECK' WHERE job_id=? AND row_no=1", (job_id,))
            await db.conn.commit()
            assert await db.heal_pending_duplicates(job_id) == 2
            got = await items(db, job_id)
            assert [(r["item_state"], r["outcome"], r["final_status_code"], r["validation_source"]) for r in got] == [
                ("DONE", "OK", 200, "NEW_CHECK"), ("DONE", "OK", 200, "DUPLICATE"), ("DONE", "OK", 200, "DUPLICATE")]
            assert await db.heal_pending_duplicates(job_id) == 0
        finally:
            await db.close()

    asyncio.run(go())


def test_spaces_in_url_path_are_validated_not_invalid(target, workdir):
    """Regression: spaces in the path/query are percent-encoded and validated; only a malformed host is INVALID."""
    b = target.base

    async def go():
        db, jm = await open_env()
        try:
            job_id, _ = await submit(jm, [f"{b}/ok/space test", "https://bad host.example/x"])
            assert await run_daemon(db, new_daemon(db, jm), job_id, timeout=60) == "COMPLETED"
            got = await items(db, job_id)
            assert (got[0]["outcome"], got[0]["final_status_code"]) == ("OK", 200)
            assert got[1]["outcome"] == "INVALID_URL"
        finally:
            await db.close()

    asyncio.run(go())


def test_database_connect_does_not_need_crawler_dependencies(workdir):
    """Regression: the API server's Database.connect() must not import psutil/httpx crawler modules."""
    import subprocess
    code = (
        "import builtins,asyncio,sys\n"
        "real=builtins.__import__\n"
        "def imp(n,*a,**k):\n"
        "    if n=='psutil': raise ModuleNotFoundError('psutil blocked')\n"
        "    return real(n,*a,**k)\n"
        "builtins.__import__=imp\n"
        "import db\n"
        "async def go():\n"
        "    d=db.Database('x.db'); await d.connect(); await d.close()\n"
        "asyncio.run(go()); print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=workdir, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": HERE})
    assert r.stdout.strip().endswith("OK"), r.stderr[-800:]
