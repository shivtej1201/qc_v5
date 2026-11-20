"""
QC V5 — On-page URL + image audit service (used by the API / dashboard).

Each audit runs as an isolated worker process (run_on_page_audit.py) so a crash can never take the
API down and every run is checkpointed/resumable. This module owns:

  * presets + duration estimates (measured performance model, see docs/architecture)
  * start / stop (graceful, cross-platform stop-file) / resume
  * ONE active run per primary domain (two crawls would double the request rate the WAF sees)
  * reading the finished package for the dashboard (issue summary, issue rows, worst pages)
  * "smart insights": plain-language findings computed from live metrics + issue summaries

Dependency-light on purpose (no psutil/httpx imports) so the API process stays lean.
"""
import csv
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import issue_store
from on_page_csv_packager import DATASETS
from seo_audit import SEO_CATEGORIES
from sitemap_config import SitemapConfigStore

ROOT = os.path.dirname(os.path.abspath(__file__))
JOB_ID_RE = re.compile(r"^onpage_[a-f0-9]{12}$")

# Measured on the 10K benchmark (docs/performance/ON_PAGE_10K_URL_IMAGE_AUDIT.md):
#   primary-budget requests per source page = 10,732 / 10,000 (page + non-source links + robots)
#   unique images per page on the simulated PDP model = 6.03; fixed overhead ~ 20 s + 1.9 ms/page packaging
PRIMARY_REQ_PER_PAGE = 1.0732
IMAGES_PER_PAGE = 6.03
FIXED_OVERHEAD_SEC = 20.0
PACKAGE_SEC_PER_PAGE = 0.0019

PRESETS: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "label": "Conservative", "description": "8 req/s on the primary domain. Safest for a WAF you have not measured.",
        "target_rps": 8.0, "max_rps": 10.0, "source_concurrency": 10, "dest_concurrency": 20, "image_concurrency": 10,
    },
    "target": {
        "label": "15-20 min target", "description": "12 req/s: 10K pages in ~15 min IF the edge sustains 12 req/s.",
        "target_rps": 12.0, "max_rps": 12.0, "source_concurrency": 20, "dest_concurrency": 20, "image_concurrency": 30,
    },
    "max_tested": {
        "label": "Max tested", "description": "14 req/s: fastest stable rate on the simulated Akamai model (~13 min / 10K).",
        "target_rps": 14.0, "max_rps": 14.0, "source_concurrency": 20, "dest_concurrency": 20, "image_concurrency": 30,
    },
    "enterprise_150k": {
        "label": "Enterprise 150K", "description": "16 req/s (adaptive up to 18), concurrency 35/35/50: ~150K pages in "
                       "~3 h IF the edge sustains 16 req/s. Measured on the simulated edge only; verify the live edge first.",
        "target_rps": 16.0, "max_rps": 18.0, "source_concurrency": 35, "dest_concurrency": 35, "image_concurrency": 50,
    },
}
TUNABLE_KEYS = {"target_rps": float, "max_rps": float, "min_rps": float, "source_concurrency": int,
                "dest_concurrency": int, "image_concurrency": int, "per_host_concurrency": int,
                "primary_domain": str, "read_timeout": float, "max_retries": int}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def estimate_duration(pages: int, rps: float, images_on_primary: bool = False,
                      images_per_page: float = IMAGES_PER_PAGE) -> Dict[str, Any]:
    """Predicted wall-clock for an audit, from the measured request model. The primary rate budget is
    the binding constraint, so time = primary requests / rps + overhead."""
    pages = max(0, int(pages))
    rps = max(0.1, float(rps))
    primary_req = pages * PRIMARY_REQ_PER_PAGE + (pages * images_per_page if images_on_primary else 0)
    crawl = primary_req / rps + FIXED_OVERHEAD_SEC
    total = crawl + pages * PACKAGE_SEC_PER_PAGE
    minutes = total / 60.0
    needed_for_20 = primary_req / max(1.0, 20 * 60 - FIXED_OVERHEAD_SEC - pages * PACKAGE_SEC_PER_PAGE)
    return {
        "pages": pages, "rps": rps, "images_on_primary": images_on_primary,
        "primary_requests": round(primary_req), "estimated_seconds": round(total), "estimated_minutes": round(minutes, 1),
        "meets_20_min_target": minutes <= 20.0,
        "rps_needed_for_20_min": round(needed_for_20, 1),
        "basis": "measured 10K benchmark request model; real edge latency/WAF limits can only lower throughput",
    }


def _domain_of(url: str) -> str:
    """Registrable-ish domain for the shared rate budget; IP hosts are used as-is."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class AuditConflict(Exception):
    pass


class OnPageAuditService:
    def __init__(self, db_path: str, base_dir: Optional[str] = None, reports_dir: Optional[str] = None,
                 python: str = sys.executable, allow_local: bool = False):
        self.db_path = os.path.abspath(db_path)
        self.base_dir = os.path.abspath(base_dir or os.path.join(ROOT, "data", "onpage"))
        self.reports_dir = os.path.abspath(reports_dir or os.path.join(ROOT, "reports", "onpage"))
        self.python = python
        self.allow_local = allow_local
        self.procs: Dict[str, subprocess.Popen] = {}
        self._issue_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)
        self.sitemaps = SitemapConfigStore(self.base_dir, allow_local=allow_local)

    # ------------------------------------------------------------------ paths / metadata
    def _dir(self, job_id: str) -> str:
        if not JOB_ID_RE.match(job_id or ""):
            raise ValueError("invalid job id")
        return os.path.join(self.base_dir, job_id)

    def _meta(self, job_id: str) -> Dict[str, Any]:
        p = os.path.join(self._dir(job_id), "config.json")
        if not os.path.exists(p):
            raise KeyError(job_id)
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def _save_meta(self, job_id: str, meta: Dict[str, Any]):
        p = os.path.join(self._dir(job_id), "config.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        os.replace(p + ".tmp", p)

    def package_dir(self, job_id: str) -> str:
        return os.path.join(self.reports_dir, job_id, "QC_V5_ONPAGE_AUDIT")

    def package_zip(self, job_id: str) -> str:
        return os.path.join(self.reports_dir, job_id, "QC_V5_ONPAGE_AUDIT.zip")

    def list_jobs(self) -> List[Dict[str, Any]]:
        out = []
        for name in sorted(os.listdir(self.base_dir)):
            if JOB_ID_RE.match(name):
                try:
                    out.append(self.describe(name))
                except (KeyError, ValueError, OSError):
                    continue
        return sorted(out, key=lambda j: j.get("created_at", ""), reverse=True)

    # ------------------------------------------------------------------ process control
    def _alive(self, job_id: str) -> bool:
        p = self.procs.get(job_id)
        if p is not None:
            return p.poll() is None
        pid_file = os.path.join(self._dir(job_id), "worker.pid")
        if not os.path.exists(pid_file):
            return False
        try:
            pid = int(open(pid_file).read().strip())
            if os.name == "nt":
                return False          # cannot probe safely without psutil; resume is always possible
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            return False

    def run_state(self, job_id: str) -> str:
        d = self._dir(job_id)
        if self._alive(job_id):
            return "STOPPING" if os.path.exists(os.path.join(d, "STOP")) else "RUNNING"
        res = self.result(job_id)
        if res:
            return res.get("status") or "FINISHED"
        return "INTERRUPTED" if os.path.exists(os.path.join(d, "worker.pid")) else "QUEUED"

    def result(self, job_id: str) -> Optional[Dict[str, Any]]:
        p = os.path.join(self._dir(job_id), "result.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def describe(self, job_id: str) -> Dict[str, Any]:
        state = self.lifecycle(job_id)       # may append a terminal event: read the metadata after it
        meta = self._meta(job_id)
        res = self.result(job_id) or {}
        return {
            **{k: meta.get(k) for k in ("job_id", "name", "created_at", "preset", "config", "url_count", "primary_domain",
                                        "source")},
            "run_state": self.run_state(job_id),
            "lifecycle": state,
            "events": meta.get("events", [])[-20:],
            "final_qc_status": res.get("final_qc_status"),
            "crawl_seconds": res.get("crawl_seconds"),
            "package_available": os.path.exists(self.package_zip(job_id)),
            "estimate": estimate_duration(meta.get("url_count", 0), (meta.get("config") or {}).get("target_rps", 8.0)),
        }

    # ------------------------------------------------------------------ lifecycle
    # CREATED -> QUEUED -> RUNNING -> (PAUSING -> PAUSED -> RESUMED) -> COMPLETED | PARTIAL | FAILED
    #                         \-> CANCELLING -> CANCELLED        crash -> INTERRUPTED (resumable)
    LIFECYCLE_STATES = ("CREATED", "QUEUED", "RUNNING", "PAUSING", "PAUSED", "RESUMED", "CANCELLING", "CANCELLED",
                        "COMPLETED", "PARTIAL", "FAILED", "INTERRUPTED")

    def _event(self, job_id: str, state: str, note: str = "", meta: Optional[Dict[str, Any]] = None):
        meta = meta or self._meta(job_id)
        ev = meta.setdefault("events", [])
        if not ev or ev[-1]["state"] != state:
            ev.append({"state": state, "at": _utc(), "note": note})
            self._save_meta(job_id, meta)
        return meta

    def lifecycle(self, job_id: str) -> str:
        """Single source of truth for the job state, derived from the worker process, its result and metadata;
        terminal states observed here are appended to the event history once."""
        meta = self._meta(job_id)
        rs = self.run_state(job_id)
        last = (meta.get("events") or [{}])[-1].get("state")
        if rs == "STOPPING":
            st = "CANCELLING" if meta.get("cancel_requested") else "PAUSING"
        elif rs == "RUNNING":
            st = "RESUMED" if last == "RESUMED" else "RUNNING"
        elif meta.get("cancel_requested"):
            st = "CANCELLED"
        elif rs == "STOPPED":
            st = "PAUSED"
        elif rs == "QUEUED":
            st = "QUEUED" if meta.get("events") else "CREATED"
        else:
            st = rs     # COMPLETED / PARTIAL / FAILED / INTERRUPTED
        if st in ("CANCELLED", "PAUSED", "COMPLETED", "PARTIAL", "FAILED", "INTERRUPTED"):
            self._event(job_id, st, meta=meta)
        return st

    def pause(self, job_id: str) -> Dict[str, Any]:
        """Graceful pause: in-flight work is committed; resume continues from the checkpoint."""
        if self._alive(job_id):
            self._event(job_id, "PAUSING")
        return self.stop(job_id)

    def cancel(self, job_id: str) -> Dict[str, Any]:
        """Stop for good. Completed work stays in the database (explorers keep working); the audit can no longer
        be resumed and no report package is built for it."""
        meta = self._meta(job_id)
        if not meta.get("cancel_requested"):
            meta["cancel_requested"] = True
            self._save_meta(job_id, meta)
            if self._alive(job_id):
                self._event(job_id, "CANCELLING")
                open(os.path.join(self._dir(job_id), "STOP"), "w").close()
            else:
                self._event(job_id, "CANCELLED")
        return self.describe(job_id)

    def progress(self, job_id: str, stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Command Center numbers, all from the backend (DB counters + worker live metrics); no client timers."""
        st = stats or {}
        live = st.get("live_metrics") or {}
        total = int(st.get("source_pages_total") or 0)
        done = int(st.get("source_pages_processed") or 0)
        running = self.run_state(job_id) in ("RUNNING", "STOPPING")
        res = self.result(job_id) or {}
        issues = None
        try:
            issues = int(self.issue_overview(job_id)["total"])
        except (FileNotFoundError, KeyError):
            pass
        return {
            "total": total, "processed": done, "remaining": max(0, total - done), "queued": max(0, total - done),
            "success": int(st.get("source_pages_completed") or 0),
            "failed": int(st.get("source_pages_failed") or 0) + int(st.get("source_pages_timed_out") or 0),
            "blocked": int(st.get("source_pages_blocked") or 0),
            "skipped": int(st.get("source_pages_explicitly_excluded") or 0),
            "percent": round(100.0 * done / total, 2) if total else 0.0,
            "links": int(st.get("url_relationships_persisted") or 0),
            "images": int(st.get("image_relationships_persisted") or 0),
            "unique_urls": int(st.get("unique_http_destinations") or 0),
            "unique_images": int(st.get("unique_image_urls") or 0),
            "issues": issues,
            "throughput_current_pps": live.get("source_pages_per_sec_recent") if running else 0.0,
            "throughput_average_pps": live.get("source_pages_per_sec"),
            "requests_per_sec_primary": live.get("primary_requests_per_sec"),
            "elapsed_sec": live.get("elapsed_sec") if running else res.get("crawl_seconds", live.get("elapsed_sec")),
            "eta_sec": (live.get("eta_recent_sec") or live.get("eta_sources_sec")) if running else 0,
            "source": "backend",
        }

    def _active_for_domain(self, domain: str) -> Optional[str]:
        for name in os.listdir(self.base_dir):
            if JOB_ID_RE.match(name) and self._alive(name):
                try:
                    if self._meta(name).get("primary_domain") == domain:
                        return name
                except (KeyError, ValueError, OSError):
                    continue
        return None

    def start(self, name: str, urls: Iterable[str], preset: str = "conservative",
              overrides: Optional[Dict[str, Any]] = None, primary_domain: Optional[str] = None,
              extra_args: Optional[List[str]] = None, sitemap_meta: Optional[Dict[str, Any]] = None,
              source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}; choose one of {sorted(PRESETS)}")
        # keep every non-empty line: the worker records invalid / duplicate lines in 17_input_exceptions.csv
        clean = [u.strip() for u in urls if u and u.strip()]
        if not clean:
            raise ValueError("no URLs supplied")
        config = {k: v for k, v in PRESETS[preset].items() if k in TUNABLE_KEYS}
        for k, v in (overrides or {}).items():
            if k in TUNABLE_KEYS and v not in (None, ""):
                config[k] = TUNABLE_KEYS[k](v)
        if config.get("max_rps", 0) < config["target_rps"]:
            config["max_rps"] = config["target_rps"]
        domain = (primary_domain or config.get("primary_domain") or "").strip().lower()
        if not domain:
            domain = _domain_of(clean[0])
        config["primary_domain"] = domain
        busy = self._active_for_domain(domain)
        if busy:
            raise AuditConflict(f"audit {busy} is already crawling {domain}; one active crawl per domain "
                                f"(a second would double the request rate the WAF sees)")
        job_id = f"onpage_{uuid.uuid4().hex[:12]}"
        d = self._dir(job_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "urls.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(clean))
        if sitemap_meta:
            SitemapConfigStore._write_json(os.path.join(d, "sitemap_meta.json"), sitemap_meta)
        meta = {"job_id": job_id, "name": (name or "On-page audit").strip()[:120], "created_at": _utc(),
                "preset": preset, "config": config, "url_count": len(set(clean)), "input_lines": len(clean),
                "primary_domain": domain,
                "extra_args": list(extra_args or []), "source": source or {"type": "url_list"}}
        self._save_meta(job_id, meta)
        self._event(job_id, "CREATED", meta=meta)
        self._spawn(job_id, meta)
        return self.describe(job_id)

    # ------------------------------------------------------------------ sitemap-driven audits
    async def discover_sitemaps(self, inputs: List[str], max_urls: int = 50000) -> Dict[str, Any]:
        return await self.sitemaps.discover(inputs, max_urls=max_urls)

    def sitemap_selection(self, import_id: str, **sel) -> Dict[str, Any]:
        snap = self.sitemaps.load_import(import_id)
        res = self.sitemaps.select(snap, **sel)
        res.pop("urls")
        pages = res["selected_count"]
        res["estimate"] = {p: estimate_duration(pages, PRESETS[p]["target_rps"]) for p in PRESETS}
        return res

    def start_from_sitemap(self, import_id: str, name: str = "", preset: str = "conservative",
                           overrides: Optional[Dict[str, Any]] = None, primary_domain: Optional[str] = None,
                           profile_id: Optional[str] = None, **sel) -> Dict[str, Any]:
        snap = self.sitemaps.load_import(import_id)
        chosen = self.sitemaps.select(snap, **sel)
        if not chosen["urls"]:
            raise ValueError("the sitemap selection is empty: widen the sitemaps or patterns")
        meta = self.sitemaps.audit_meta(snap, chosen)
        source = {"type": "sitemap", "import_id": import_id, "inputs": snap["inputs"], "profile_id": profile_id,
                  "selected_sitemaps": len(chosen["selected_sitemaps"]), "include": chosen["include"],
                  "exclude": chosen["exclude"], "max_pages": chosen["max_pages"], "excluded": chosen["excluded"]}
        return self.start(name or f"Sitemap audit · {urllib.parse.urlsplit(snap['inputs'][0]).hostname}",
                          [r["u"] for r in chosen["urls"]], preset=preset, overrides=overrides,
                          primary_domain=primary_domain, sitemap_meta=meta, source=source)

    async def run_profile(self, profile_id: str) -> Dict[str, Any]:
        """Re-discover the profile's sitemaps (fresh URLs/lastmod) and start an audit with its saved selection."""
        prof = self.sitemaps.get_profile(profile_id)
        snap = await self.discover_sitemaps(prof["inputs"])
        known = {s["sitemap_url"] for s in snap["sources"]}
        selected = [u for u in prof.get("selected_sitemaps") or [] if u in known]
        job = self.start_from_sitemap(snap["import_id"], name=prof["name"], preset=prof.get("preset", "conservative"),
                                      overrides=prof.get("overrides"), profile_id=profile_id,
                                      primary_domain=prof.get("primary_domain") or None,
                                      selected_sitemaps=selected, include=prof.get("include"),
                                      exclude=prof.get("exclude"), max_pages=prof.get("max_pages", 0))
        self.sitemaps.touch_profile(profile_id, {"job_id": job["job_id"], "at": _utc(), "import_id": snap["import_id"],
                                                 "pages": job.get("url_count")})
        return {"import": snap, "audit": job}

    def _spawn(self, job_id: str, meta: Dict[str, Any]):
        d = self._dir(job_id)
        c = meta["config"]
        for f in ("STOP", "result.json"):
            try:
                os.remove(os.path.join(d, f))
            except FileNotFoundError:
                pass
        args = [self.python, os.path.join(ROOT, "run_on_page_audit.py"),
                "--urls", os.path.join(d, "urls.txt"), "--db", self.db_path, "--job-id", job_id,
                "--job-name", meta["name"], "--out-dir", os.path.join(self.reports_dir, job_id),
                "--package-name", "QC_V5_ONPAGE_AUDIT", "--primary-domain", c["primary_domain"],
                "--target-rps", str(c["target_rps"]), "--max-rps", str(c.get("max_rps", c["target_rps"])),
                "--source-concurrency", str(c["source_concurrency"]), "--dest-concurrency", str(c["dest_concurrency"]),
                "--image-concurrency", str(c["image_concurrency"]),
                "--stop-file", os.path.join(d, "STOP"), "--result-json", os.path.join(d, "result.json")]
        for k, flag in (("min_rps", "--min-rps"), ("per_host_concurrency", "--per-host-concurrency"),
                        ("read_timeout", "--read-timeout"), ("max_retries", "--max-retries")):
            if k in c:
                args += [flag, str(c[k])]
        if self.allow_local:
            args.append("--allow-local")
        if os.path.exists(os.path.join(d, "sitemap_meta.json")):
            args += ["--sitemap-meta", os.path.join(d, "sitemap_meta.json")]
        args += meta.get("extra_args") or []
        log = open(os.path.join(d, "worker.log"), "a", encoding="utf-8")
        log.write(f"\n=== {_utc()} start {' '.join(args[1:4])} ...\n")
        log.flush()
        creation = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {}
        self._event(job_id, "QUEUED")
        proc = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, **creation)
        self.procs[job_id] = proc
        with open(os.path.join(d, "worker.pid"), "w") as f:
            f.write(str(proc.pid))
        self._event(job_id, "RUNNING")

    def stop(self, job_id: str) -> Dict[str, Any]:
        """Graceful: in-flight requests finish and are committed; the job stays resumable."""
        d = self._dir(job_id)
        self._meta(job_id)
        if self._alive(job_id):
            open(os.path.join(d, "STOP"), "w").close()
        return self.describe(job_id)

    def resume(self, job_id: str) -> Dict[str, Any]:
        meta = self._meta(job_id)
        if meta.get("cancel_requested"):
            raise AuditConflict(f"audit {job_id} was cancelled and cannot be resumed; start a new audit")
        if self._alive(job_id):
            raise AuditConflict(f"audit {job_id} is already running")
        busy = self._active_for_domain(meta["primary_domain"])
        if busy:
            raise AuditConflict(f"audit {busy} is already crawling {meta['primary_domain']}")
        meta["resumes"] = int(meta.get("resumes") or 0) + 1
        self._save_meta(job_id, meta)
        self._spawn(job_id, meta)
        self._event(job_id, "RESUMED")
        return self.describe(job_id)

    def wait(self, job_id: str, timeout: float = 600.0) -> str:
        end = time.time() + timeout
        while time.time() < end and self._alive(job_id):
            time.sleep(0.3)
        return self.run_state(job_id)

    def log_tail(self, job_id: str, lines: int = 40) -> List[str]:
        p = os.path.join(self._dir(job_id), "worker.log")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8", errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-lines:]]

    # ------------------------------------------------------------------ finished-report access
    def _csv(self, job_id: str, name: str) -> str:
        p = os.path.join(self.package_dir(job_id), name)
        if not os.path.exists(p):
            raise FileNotFoundError(name)
        return p

    def manifest(self, job_id: str) -> Dict[str, Any]:
        """The report package manifest (00_manifest.csv): every file with dataset, rows, columns, size, SHA-256 and
        status. Before the package is built, the expected files are listed as PENDING."""
        self._meta(job_id)
        try:
            with open(self._csv(job_id, "00_manifest.csv"), newline="", encoding="utf-8") as f:
                entries = list(csv.DictReader(f))
            ready = True
        except FileNotFoundError:
            entries = [{"filename": n, "dataset": d, "status": "PENDING"} for n, d in DATASETS.items()]
            ready = False
        return {"job_id": job_id, "ready": ready, "total_files": len(entries), "manifest": entries}

    def issue_overview(self, job_id: str) -> Dict[str, Any]:
        """Issue summary + severity totals + top offending URLs (cached per package mtime)."""
        p12 = self._csv(job_id, "12_issues.csv")
        mtime = os.path.getmtime(p12)
        cached = self._issue_cache.get(job_id)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(self._csv(job_id, "13_issue_summary.csv"), newline="", encoding="utf-8") as f:
            types = [dict(r, relationships=int(r["relationships"]), unique_urls=int(r["unique_urls"]),
                          source_pages_affected=int(r["source_pages_affected"])) for r in csv.DictReader(f)]
        sev = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
        conn = self._db()
        try:
            in_db = issue_store.has_issues_table_rows(conn, job_id)
            if in_db:      # indexed aggregation instead of re-reading a multi-million-row CSV
                for k, v in conn.execute("SELECT severity, COUNT(*) FROM on_page_issues WHERE job_id = ? GROUP BY severity",
                                         (job_id,)):
                    sev[k] = v
                top_urls = [((u, t), n) for u, t, n in conn.execute(
                    """SELECT subject_url, rule_code, COUNT(*) AS n FROM on_page_issues
                        WHERE job_id = ? AND severity = 'HIGH' AND category != 'SOURCE_PAGE'
                        GROUP BY subject_url, rule_code ORDER BY n DESC, subject_url LIMIT 10""", (job_id,))]
        finally:
            conn.close()
        if not in_db:
            url_counts: Dict[Tuple[str, str], int] = {}
            with open(p12, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sev[r["severity"]] = sev.get(r["severity"], 0) + 1
                    if r["severity"] == "HIGH" and r["category"] != "SOURCE_PAGE":
                        k = (r["normalized_url"], r["issue_type"])
                        url_counts[k] = url_counts.get(k, 0) + 1
            top_urls = sorted(url_counts.items(), key=lambda kv: -kv[1])[:10]
        with open(self._csv(job_id, "01_summary.csv"), newline="", encoding="utf-8") as f:
            summary = {r["metric"]: r["value"] for r in csv.DictReader(f)}
        data = {"by_severity": sev, "types": types, "total": sum(sev.values()), "summary": summary,
                "top_high_urls": [{"url": u, "issue_type": t, "relationships": n} for (u, t), n in top_urls]}
        self._issue_cache[job_id] = (mtime, data)
        return data

    def seo_overview(self, job_id: str) -> Dict[str, Any]:
        """SEO coverage (from 15), SEO issue categories (from 13) and sitemap health (from 16)."""
        p15 = self._csv(job_id, "15_seo_page_audit.csv")
        n = 0
        c = {k: 0 for k in ("indexable", "noindex", "title_ok", "desc_ok", "desc_present", "single_h1", "viewport",
                            "lang", "schema", "schema_invalid", "og_complete", "hreflang", "mixed_content", "thin")}
        words = 0
        with open(p15, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                n += 1
                gi = lambda k: int(r.get(k) or 0)
                c["indexable"] += gi("indexable")
                c["noindex"] += gi("noindex")
                c["title_ok"] += 30 <= gi("title_length") <= 60
                c["desc_present"] += gi("meta_description_length") > 0
                c["desc_ok"] += 70 <= gi("meta_description_length") <= 160
                c["single_h1"] += gi("h1_count") == 1
                c["viewport"] += gi("has_viewport")
                c["lang"] += bool((r.get("html_lang") or "").strip())
                c["schema"] += gi("jsonld_blocks") > 0 and gi("jsonld_invalid") == 0
                c["schema_invalid"] += gi("jsonld_invalid") > 0
                c["og_complete"] += bool(r.get("og_title") and r.get("og_description") and r.get("og_image"))
                c["hreflang"] += gi("hreflang_count") > 0
                c["mixed_content"] += gi("insecure_resources") > 0
                c["thin"] += gi("word_count") < 200
                words += gi("word_count")
        pct = lambda k: round(100.0 * c[k] / n, 1) if n else None
        # score: share of pages passing each core check, equally weighted
        core = [pct("indexable"), pct("title_ok"), pct("desc_present"), pct("single_h1"), pct("viewport"),
                pct("lang"), (100 - pct("mixed_content")) if n else None, (100 - pct("schema_invalid")) if n else None,
                (100 - pct("thin")) if n else None]
        core = [v for v in core if v is not None]
        score = round(sum(core) / len(core)) if core else None
        ov = self.issue_overview(job_id)
        cats: Dict[str, Dict[str, Any]] = {}
        for t in ov["types"]:
            if t["category"] not in SEO_CATEGORIES:
                continue
            cat = cats.setdefault(t["category"], {"category": t["category"], "issues": 0, "pages": 0, "types": []})
            cat["issues"] += t["relationships"]
            cat["pages"] = max(cat["pages"], t["source_pages_affected"])
            cat["types"].append({k: t[k] for k in ("severity", "issue_type", "relationships", "source_pages_affected",
                                                   "recommended_action")})
        sitemaps = []
        try:
            with open(self._csv(job_id, "16_sitemap_audit.csv"), newline="", encoding="utf-8") as f:
                sitemaps = list(csv.DictReader(f))
        except FileNotFoundError:
            pass
        return {"pages": n, "score": score, "counts": c, "avg_words": round(words / n) if n else 0,
                "coverage": {k: pct(k) for k in c},
                "categories": sorted(cats.values(), key=lambda x: -x["issues"]), "sitemaps": sitemaps}

    # ------------------------------------------------------------------ database-backed explorers (server-side paging)
    def _db(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    @staticmethod
    def _page(conn, select: str, base: str, where: List[str], args: List[Any], sort: str, sort_cols: Dict[str, str],
              order: str, offset: int, limit: int) -> Dict[str, Any]:
        """One bounded page: whitelisted sort column, LIMIT <= 500, total via COUNT on the same filter."""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        if sort and sort not in sort_cols:
            raise ValueError(f"sort must be one of {', '.join(sort_cols)}")
        col = sort_cols.get(sort or "", next(iter(sort_cols.values())))
        direction = "DESC" if str(order).lower() == "desc" else "ASC"
        w = " AND ".join(where)
        order_by = ", ".join(f"{c.strip()} {direction}" for c in col.split(","))   # direction on EVERY key
        total = conn.execute(f"SELECT COUNT(*) {base} WHERE {w}", args).fetchone()[0]
        cur = conn.execute(f"SELECT {select} {base} WHERE {w} ORDER BY {order_by} LIMIT ? OFFSET ?",
                           args + [limit, offset])
        cols = [d[0] for d in cur.description]
        return {"items": [dict(zip(cols, r)) for r in cur.fetchall()], "matched": total, "offset": offset,
                "limit": limit, "sort": sort or "", "order": direction.lower()}

    def _project(self, job_id: str) -> str:
        return (self._meta(job_id).get("primary_domain") or "").lower()

    def issues_db(self, job_id: str, severity: Optional[str] = None, rule: Optional[str] = None,
                  category: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None,
                  page_url: Optional[str] = None, sort: str = "", order: str = "asc", offset: int = 0,
                  limit: int = 100) -> Dict[str, Any]:
        self._meta(job_id)
        project = self._project(job_id)
        conn = self._db()
        try:
            issue_store.ensure_issue_schema(conn)
            where, args = ["i.job_id = ?"], [job_id]
            for col, val in (("i.severity", severity), ("i.rule_code", rule), ("i.category", category),
                             ("i.page_url", page_url)):
                if val:
                    where.append(f"{col} = ?")
                    args.append(val)
            if status:
                where.append("COALESCE(st.status, 'OPEN') = ?")
                args.append(status.upper())
            if search:
                where.append("(i.page_url LIKE ? OR i.subject_url LIKE ? OR i.found_url LIKE ?)")
                args += [f"%{search}%"] * 3
            sev = "CASE i.severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 WHEN 'LOW' THEN 2 ELSE 3 END, i.id"
            res = self._page(conn,
                """i.issue_key, i.severity, i.category, i.rule_code AS issue_type, i.dimension, i.page_url AS source_page_url,
                   i.element_type, i.found_url, i.subject_url AS normalized_url, i.anchor_or_alt_text, i.location,
                   i.http_status, i.initial_status, i.final_url, i.hop_count, i.detail, i.validation_source,
                   COALESCE(st.status, 'OPEN') AS status, COALESCE(st.note, '') AS status_note""",
                "FROM on_page_issues i LEFT JOIN on_page_issue_status st ON st.project_id = ? AND st.issue_key = i.issue_key",
                where, [project] + args, sort,
                {"severity": sev, "rule": "i.rule_code, i.id", "page": "i.page_url, i.id", "status": "status, i.id",
                 "id": "i.id"}, order, offset, limit)
        finally:
            conn.close()
        from on_page_issue_rules import action_for
        for it in res["items"]:
            it["recommended_action"] = action_for(it["issue_type"], it["category"])
            it["status_bucket"] = None
        res["source"] = "database"
        return res

    def set_issue_status(self, job_id: str, key: str, status: str, note: str = "") -> Dict[str, Any]:
        if not re.match(r"^[a-f0-9]{20}$", key or ""):
            raise ValueError("invalid issue key")
        conn = self._db()
        try:
            return issue_store.set_status(conn, job_id, self._project(job_id), key, status, note)
        finally:
            conn.close()

    def score(self, job_id: str) -> Dict[str, Any]:
        self._meta(job_id)
        conn = self._db()
        try:
            sc = issue_store.get_score(conn, job_id)
        finally:
            conn.close()
        if sc is None:
            raise KeyError(job_id)
        return sc

    def urls(self, job_id: str, status_bucket: Optional[str] = None, crawl_state: Optional[str] = None,
             search: Optional[str] = None, sort: str = "", order: str = "asc", offset: int = 0, limit: int = 100):
        """URL audit explorer: one row per source page (server-side filter / sort / paging)."""
        self._meta(job_id)
        conn = self._db()
        try:
            where, args = ["s.job_id = ?"], [job_id]
            if crawl_state:
                where.append("s.crawl_state = ?"); args.append(crawl_state)
            if status_bucket:
                lo = {"2xx": 200, "3xx": 300, "4xx": 400, "5xx": 500}.get(status_bucket)
                if lo is None:
                    raise ValueError("status_bucket must be 2xx, 3xx, 4xx or 5xx")
                where.append("s.http_status BETWEEN ? AND ?"); args += [lo, lo + 99]
            if search:
                where.append("s.source_page_url LIKE ?"); args.append(f"%{search}%")
            return self._page(conn,
                """s.source_page_id, s.source_page_url, s.crawl_state, s.initial_http_status, s.http_status, s.final_url,
                   s.redirect_count, s.response_time_ms, s.content_type, s.page_title, s.relationships_count,
                   s.image_relationships_count, s.business, s.page_type, s.language, s.source_sitemap,
                   p.indexable, p.meta_robots, p.h1_count, p.word_count, p.title_length, p.meta_description_length""",
                "FROM on_page_source_pages s LEFT JOIN on_page_seo_pages p ON p.job_id = s.job_id AND p.source_page_url = s.source_page_url",
                where, args, sort, {"id": "s.source_page_id", "status": "s.http_status, s.source_page_id",
                                    "url": "s.source_page_url", "time": "s.response_time_ms, s.source_page_id",
                                    "links": "s.relationships_count, s.source_page_id"}, order, offset, limit)
        finally:
            conn.close()

    def links(self, job_id: str, scope: Optional[str] = None, url_type: Optional[str] = None,
              status_bucket: Optional[str] = None, source_page: Optional[str] = None, search: Optional[str] = None,
              sort: str = "", order: str = "asc", offset: int = 0, limit: int = 100):
        """Link audit explorer: one row per page -> URL relationship with its (deduplicated) validation."""
        self._meta(job_id)
        conn = self._db()
        try:
            where, args = ["r.job_id = ?"], [job_id]
            for col, val in (("r.found_url_scope", scope), ("r.found_url_type", url_type), ("c.status_bucket", status_bucket),
                             ("r.source_page_url", source_page)):
                if val:
                    where.append(f"{col} = ?"); args.append(val)
            if search:
                where.append("(r.normalized_found_url LIKE ? OR r.anchor_text LIKE ?)"); args += [f"%{search}%"] * 2
            return self._page(conn,
                """r.id, r.source_page_url, r.found_url, r.normalized_found_url AS normalized_url, r.found_url_type AS url_type,
                   r.found_url_scope AS internal_external, r.anchor_text, r.rel, r.representative_location AS component,
                   r.language, r.business, r.page_type, r.validation_source, c.http_status, c.initial_http_status,
                   c.final_url, c.redirect_count, c.status_bucket, c.error_type""",
                "FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url",
                where, args, sort, {"id": "r.id", "status": "c.http_status, r.id", "url": "r.normalized_found_url, r.id",
                                    "page": "r.source_page_url, r.id"}, order, offset, limit)
        finally:
            conn.close()

    def images(self, job_id: str, internal_external: Optional[str] = None, status_bucket: Optional[str] = None,
               alt: Optional[str] = None, source_page: Optional[str] = None, search: Optional[str] = None,
               sort: str = "", order: str = "asc", offset: int = 0, limit: int = 100):
        """Image audit explorer: one row per page -> image placement with its (deduplicated) validation."""
        self._meta(job_id)
        conn = self._db()
        try:
            where, args = ["i.job_id = ?"], [job_id]
            for col, val in (("i.internal_external", internal_external), ("ic.status_bucket", status_bucket),
                             ("i.source_page_url", source_page)):
                if val:
                    where.append(f"{col} = ?"); args.append(val)
            if alt:
                cond = {"missing": "i.alt_present = 0", "empty": "i.alt_present = 1 AND i.alt_text = ''",
                        "present": "i.alt_present = 1 AND i.alt_text != ''"}.get(alt)
                if cond is None:
                    raise ValueError("alt must be missing, empty or present")
                where.append(cond)
            if search:
                where.append("i.normalized_image_url LIKE ?"); args.append(f"%{search}%")
            return self._page(conn,
                """i.id, i.source_page_url, i.image_url, i.normalized_image_url, i.attribute_source, i.alt_present, i.alt_text,
                   i.width, i.height, i.loading, i.internal_external, i.validation_source, ic.http_status, ic.content_type,
                   ic.content_length, ic.status_bucket, ic.image_issue, ic.error_type""",
                "FROM on_page_image_relationships i LEFT JOIN on_page_image_validation_cache ic ON ic.job_id = i.job_id AND ic.normalized_url = i.normalized_image_url",
                where, args, sort, {"id": "i.id", "status": "ic.http_status, i.id", "url": "i.normalized_image_url, i.id",
                                    "size": "ic.content_length, i.id"}, order, offset, limit)
        finally:
            conn.close()

    def url_detail(self, job_id: str, url: str) -> Dict[str, Any]:
        """Everything known about one audited page, grouped for the URL Detail view (raw evidence included)."""
        self._meta(job_id)
        conn = self._db()
        try:
            cur = conn.execute("SELECT * FROM on_page_source_pages WHERE job_id = ? AND source_page_url = ?", (job_id, url))
            row = cur.fetchone()
            if row is None:
                raise KeyError(url)
            page = dict(zip([d[0] for d in cur.description], row))
            cur = conn.execute("SELECT * FROM on_page_seo_pages WHERE job_id = ? AND source_page_url = ?", (job_id, url))
            srow = cur.fetchone()
            seo = dict(zip([d[0] for d in cur.description], srow)) if srow else {}
            link_types = [dict(zip(("url_type", "scope", "relationships", "broken"), r)) for r in conn.execute(
                """SELECT r.found_url_type, r.found_url_scope, COUNT(*),
                          SUM(CASE WHEN c.http_status >= 400 OR c.status_bucket IN ('Timeout','DNS/Network','SSL') THEN 1 ELSE 0 END)
                     FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c
                       ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
                    WHERE r.job_id = ? AND r.source_page_url = ? GROUP BY r.found_url_type, r.found_url_scope""", (job_id, url))]
            crow = conn.execute(
                """SELECT r.normalized_found_url, c.http_status, c.final_url FROM on_page_link_relationships r
                     LEFT JOIN on_page_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
                    WHERE r.job_id = ? AND r.source_page_url = ? AND r.found_url_type = 'CANONICAL'
                    ORDER BY r.normalized_found_url LIMIT 1""", (job_id, url)).fetchone()
            canonical = ({"url": crow[0], "status": crow[1], "final_url": crow[2],
                          "self_referencing": crow[0] in (page.get("normalized_source_url"), page.get("final_url"))}
                         if crow else None)
            img = conn.execute(
                """SELECT COUNT(*), SUM(CASE WHEN i.alt_present = 0 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN ic.http_status >= 400 OR COALESCE(ic.image_issue, '') != '' THEN 1 ELSE 0 END)
                     FROM on_page_image_relationships i LEFT JOIN on_page_image_validation_cache ic
                       ON ic.job_id = i.job_id AND ic.normalized_url = i.normalized_image_url
                    WHERE i.job_id = ? AND i.source_page_url = ?""", (job_id, url)).fetchone()
        finally:
            conn.close()
        try:
            chain = json.loads(page.get("redirect_chain") or "[]")
        except ValueError:
            chain = []
        try:
            hreflang = json.loads(seo.get("hreflang_json") or "[]")
        except ValueError:
            hreflang = []
        issues = self.issues_db(job_id, page_url=url, limit=500)["items"]
        return {
            "url": url,
            "overview": {k: page.get(k) for k in ("source_page_id", "crawl_state", "business", "page_type", "language",
                                                  "source_sitemap", "checked_at", "classification_method")},
            "http": {k: page.get(k) for k in ("initial_http_status", "http_status", "final_url", "content_type",
                                              "error_type", "error_message", "robots_status", "waf_status", "attempts")},
            "redirect": {"count": page.get("redirect_count"), "chain": chain, "final_url": page.get("final_url")},
            "canonical": canonical,
            "robots": {"meta_robots": seo.get("meta_robots"), "x_robots_tag": seo.get("x_robots_tag"),
                       "indexable": seo.get("indexable"), "noindex": seo.get("noindex"), "nofollow": seo.get("nofollow")},
            "metadata": {k: seo.get(k) for k in ("title", "title_length", "title_count", "meta_description",
                                                 "meta_description_length", "html_lang", "has_viewport", "has_charset",
                                                 "og_title", "og_description", "og_image", "twitter_card", "meta_refresh")},
            "headings": {k: seo.get(k) for k in ("h1_count", "h1_text", "h2_count", "heading_skips")},
            "hreflang": hreflang,
            "structured_data": {"blocks": seo.get("jsonld_blocks"), "invalid": seo.get("jsonld_invalid"),
                                "types": seo.get("schema_types")},
            "content": {k: seo.get(k) for k in ("word_count", "content_hash", "js_rendering_suspected", "script_count")},
            "performance": {"response_time_ms": page.get("response_time_ms"), "html_bytes": page.get("html_bytes"),
                            "html_truncated": page.get("html_truncated")},
            "security": {"insecure_resources": seo.get("insecure_resources"), "examples": seo.get("insecure_examples")},
            "links": {"by_type": link_types, "relationships": page.get("relationships_count"),
                      "raw_occurrences": page.get("raw_occurrences_count")},
            "images": {"placements": img[0] or 0, "missing_alt": img[1] or 0, "broken": img[2] or 0},
            "issues": issues,
            "raw": {"source_page": page, "seo": seo},
        }

    def issue_detail(self, job_id: str, key: str) -> Dict[str, Any]:
        if not re.match(r"^[a-f0-9]{20}$", key or ""):
            raise ValueError("invalid issue key")
        self._meta(job_id)
        conn = self._db()
        try:
            cur = conn.execute(
                """SELECT i.*, COALESCE(st.status, 'OPEN') AS status, COALESCE(st.note, '') AS status_note,
                          st.updated_at AS status_updated_at, st.last_job_id AS status_job_id
                     FROM on_page_issues i LEFT JOIN on_page_issue_status st
                       ON st.project_id = ? AND st.issue_key = i.issue_key
                    WHERE i.job_id = ? AND i.issue_key = ?""", (self._project(job_id), job_id, key))
            row = cur.fetchone()
            if row is None:
                raise KeyError(key)
            it = dict(zip([d[0] for d in cur.description], row))
        finally:
            conn.close()
        from on_page_issue_rules import action_for
        it["recommended_action"] = action_for(it["rule_code"], it["category"])
        it["evidence"] = {k: it.get(k) for k in ("element_type", "found_url", "subject_url", "anchor_or_alt_text",
                                                 "location", "http_status", "initial_status", "final_url", "hop_count",
                                                 "detail", "validation_source")}
        return it

    def issues(self, job_id: str, severity: Optional[str] = None, issue_type: Optional[str] = None,
               category: Optional[str] = None, search: Optional[str] = None, offset: int = 0,
               limit: int = 100) -> Dict[str, Any]:
        """Filtered page of issues: from the database (indexed, server-side) when the audit has persisted issues,
        else from 12_issues.csv (audits packaged before issue persistence)."""
        conn = self._db()
        try:
            in_db = issue_store.has_issues_table_rows(conn, job_id)
        finally:
            conn.close()
        if in_db:
            return self.issues_db(job_id, severity=severity, rule=issue_type, category=category, search=search,
                                  offset=offset, limit=min(int(limit), 500))
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        s = (search or "").lower().strip()
        items, matched = [], 0
        with open(self._csv(job_id, "12_issues.csv"), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if severity and r["severity"] != severity:
                    continue
                if issue_type and r["issue_type"] != issue_type:
                    continue
                if category and r["category"] != category:
                    continue
                if s and s not in (r["source_page_url"] + " " + r["normalized_url"] + " " + r["found_url"]).lower():
                    continue
                if offset <= matched < offset + limit:
                    items.append(r)
                matched += 1
        return {"items": items, "matched": matched, "offset": offset, "limit": limit}

    def worst_pages(self, job_id: str, limit: int = 25) -> List[Dict[str, Any]]:
        out = []
        with open(self._csv(job_id, "14_page_issue_summary.csv"), newline="", encoding="utf-8") as f:
            for i, r in enumerate(csv.DictReader(f)):
                if i >= max(1, min(int(limit), 500)):
                    break
                out.append(r)
        return out

    # ------------------------------------------------------------------ smart insights
    def insights(self, job_id: str, stats: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Plain-language findings, most important first. Every number is computed, never invented."""
        meta = self._meta(job_id)
        cfg = meta.get("config") or {}
        out: List[Dict[str, Any]] = []
        stats = stats or {}
        live = stats.get("live_metrics") if isinstance(stats.get("live_metrics"), dict) else {}
        total = int(stats.get("source_pages_total") or meta.get("url_count") or 0)
        done = int(stats.get("source_pages_processed") or 0)
        state = self.run_state(job_id)

        if state in ("RUNNING", "STOPPING") and live:
            el = float(live.get("elapsed_sec") or 0)
            pps = float(live.get("source_pages_per_sec") or 0)
            if pps > 0 and total:
                projected = (el + (total - done) / pps) / 60.0
                kind = "success" if projected <= 20 else "warning"
                rps = float(live.get("primary_requests_per_sec") or 0)
                msg = f"Projected total {projected:.1f} min at {pps:.1f} pages/s ({rps:.1f} req/s on {cfg.get('primary_domain')})."
                if projected > 20:
                    est = estimate_duration(total, cfg.get("target_rps", 8))
                    msg += f" The 15-20 min target needs ~{est['rps_needed_for_20_min']} req/s sustained; the rate budget, not the code, is the limit."
                out.append({"kind": kind, "title": "Time to finish", "message": msg})
            src_r, url_r = float(live.get("source_request_rate") or 0), float(live.get("url_request_rate") or 0)
            if src_r + url_r > 0 and url_r / (src_r + url_r) > 0.25:
                share = 100.0 * url_r / (src_r + url_r)
                out.append({"kind": "warning", "title": "Link checks are sharing the budget",
                            "message": f"{share:.0f}% of primary-domain requests are validating links that are NOT in this sitemap "
                                       f"(the estimate assumes ~7%). Pages link to many URLs outside the audited set, so the run "
                                       f"takes longer; include those pages in the sitemap list or accept the longer time."})
            if int(live.get("waf_events") or 0) or int(live.get("cooldowns") or 0):
                out.append({"kind": "danger", "title": "WAF pushback",
                            "message": f"{live.get('waf_events')} WAF responses, {live.get('cooldowns')} cooldown(s); throttle now {live.get('current_target_rps')} req/s. Do not raise the rate."})
        elif not stats.get("source_pages_total"):
            est = estimate_duration(meta.get("url_count", 0), cfg.get("target_rps", 8))
            out.append({"kind": "info", "title": "Estimate",
                        "message": f"{meta.get('url_count')} pages at {cfg.get('target_rps')} req/s: ~{est['estimated_minutes']} min."})

        try:
            ov = self.issue_overview(job_id)
        except FileNotFoundError:
            ov = None
        if ov:
            sev = ov["by_severity"]
            high_types = [t for t in ov["types"] if t["severity"] == "HIGH"]
            if sev["HIGH"] == 0:
                out.append({"kind": "success", "title": "No high-severity issues", "message": "Nothing is broken for users or search engines."})
            else:
                top = sorted(high_types, key=lambda t: -t["relationships"])[0]
                share = 100.0 * top["relationships"] / max(1, sev["HIGH"])
                out.append({"kind": "danger", "title": f"Fix first: {top['issue_type']}",
                            "message": f"{top['relationships']:,} high-severity rows ({share:.0f}% of all HIGH) come from only {top['unique_urls']:,} unique URL(s) on {top['source_pages_affected']:,} page(s). {top['recommended_action']}"})
            if ov["top_high_urls"]:
                t10 = sum(u["relationships"] for u in ov["top_high_urls"])
                out.append({"kind": "info", "title": "Biggest wins",
                            "message": f"Fixing the top {len(ov['top_high_urls'])} URLs clears {t10:,} of {sev['HIGH']:,} high-severity rows ({100.0 * t10 / max(1, sev['HIGH']):.0f}%)."})
            waf = next((t for t in ov["types"] if t["issue_type"] in ("WAF_BLOCKED", "IMAGE_WAF_BLOCKED", "SOURCE_WAF_BLOCKED")), None)
            if waf:
                out.append({"kind": "warning", "title": "Results hidden by the WAF",
                            "message": f"{waf['unique_urls']:,} URL(s) were blocked by the CDN/WAF, so their real status is unknown. Re-run from an allow-listed IP or at a lower rate."})
            qc = ov["summary"].get("final_qc_status")
            out.append({"kind": "success" if qc == "PASS" else "danger", "title": f"Reconciliation {qc}",
                        "message": "Every relationship, validation and CSV row reconciles with the database." if qc == "PASS"
                        else "The trust gate found a discrepancy; see 11_reconciliation.csv before using this report."})
        order = {"danger": 0, "warning": 1, "info": 2, "success": 3}
        return sorted(out, key=lambda i: order.get(i["kind"], 9))

    def health_score(self, job_id: str) -> Optional[Dict[str, Any]]:
        """% of source pages without any HIGH issue (simple, explainable)."""
        try:
            path = self._csv(job_id, "14_page_issue_summary.csv")
            summary = self.issue_overview(job_id)["summary"]
        except FileNotFoundError:
            return None
        pages_total = int(summary.get("source_pages_total") or 0)
        with_high = 0
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if int(r["high"] or 0) > 0:
                    with_high += 1
        score = round(100.0 * (pages_total - with_high) / pages_total, 1) if pages_total else None
        return {"score": score, "pages_total": pages_total, "pages_with_high_issues": with_high,
                "definition": "share of source pages with no HIGH-severity issue"}
