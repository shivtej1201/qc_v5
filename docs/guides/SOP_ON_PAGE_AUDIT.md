# SOP — Running a QC V5 On-Page Audit

| | |
|---|---|
| Document | Standard Operating Procedure, on-page URL / link / image / SEO audit |
| Applies to | QC V5 Command Center (`/dashboard/`) and the `run_on_page_audit.py` command line |
| Audience | SEO analysts who run audits, the operator who owns the server, developers who fix the issues |
| Related | [User guide](USER_GUIDE.md) · [Architecture](../architecture/ARCHITECTURE.md) · [API contract](../api/API_CONTRACT.md) · [Final validation report](../audit/FINAL_VALIDATION_REPORT.md) |

---

## 1. Purpose

Audit every page listed in a site's sitemaps — every link, canonical, hreflang, script, stylesheet, image and SEO
signal on each page — check every unique URL exactly once, and hand developers a prioritized, deduplicated list of
issues with a recommended fix for each. This SOP makes every run **safe for the target site** (no WAF blocks),
**reproducible**, and **trustworthy** (numbers reconciled against the database before anyone acts on them).

## 2. Scope

In scope: sitemap-driven audits of sites you own or are authorized to test (e.g. `bajajfinserv.in` and its
sub-domains), from 1 page to 300,000 pages, run from the dashboard or the command line.

Out of scope: crawling sites you have no permission to crawl; rendering JavaScript-only pages (they are detected and
flagged `JS_RENDERING_SUSPECTED`, not rendered); multi-tenant access control (the app is single-tenant behind a
reverse proxy).

## 3. Roles

| Role | Responsibilities |
|---|---|
| **Operator** | Installs and runs the server, owns disk space, backups and the rate limits agreed with the site owner; handles crashes and WAF incidents |
| **Analyst** | Configures and launches audits, monitors them, validates the reports, triages issues, assigns them |
| **Developer / content owner** | Fixes issues, moves them to IN_PROGRESS → (re-audit) → VERIFIED → RESOLVED |
| **Site / CDN owner** | Confirms the request rate the edge (Akamai) tolerates from the audit machine; allow-lists its IP if needed |

## 4. Key terms

| Term | Meaning |
|---|---|
| Source page | A page listed in the sitemap; it is fetched and parsed |
| Destination URL | Any URL found on a source page (link, canonical, script, image …); validated once per audit, however many pages use it |
| Relationship | One (source page → destination URL) pair; every one is kept and reported |
| Primary domain | The site being audited; all its hosts share **one** request budget (req/s) |
| Preset | A named speed profile (rate + concurrency) |
| Trust gate | 35 reconciliation checks that compare every CSV against the database; a mismatch fails the run |
| Health score | 0–100, formula `qcv5-health-1.0`: Technical 35 %, Links 25 %, Content 20 %, Performance 10 %, Security 10 % |

## 5. Prerequisites

### 5.1 Machine

* Python 3.10+ (3.13 tested), 4 CPU cores, 8 GB RAM minimum (16 GB for > 100K pages).
* **Free disk** — budget about **75 KB per page** at peak (database + CSVs + ZIP), measured 1.48 GB for 20,000 pages:

  | Pages | Free disk to reserve |
  |---|---|
  | 10,000 | 1 GB |
  | 50,000 | 4 GB |
  | 150,000 | 12 GB |
  | 300,000 | 30 GB (a 300K report build ran out of space with ~28 GB free; reserve more) |

* Network route to the target site that is not shared with other heavy crawlers (they share the WAF budget).

### 5.2 Permissions

* Written OK from the site owner for the rate you will use (see §6).
* If the edge blocks unknown IPs: the audit machine's IP allow-listed at the CDN.

### 5.3 Install (one time)

```bash
git clone https://github.com/shivtej1201/qc_v5.git
```

```bash
cd qc_v5
```

```bash
./start.sh
```

On Windows run `start.bat`. The script creates a virtual environment, installs `requirements.txt` and Chromium, and
starts the server on <http://127.0.0.1:8002>. Later starts: activate the venv and run `python run.py`.

Configuration is read from environment variables / `.env` (see `config.py`). Never set `ALLOW_LOCAL_TESTING=true`
on a production server — it disables the private-address (SSRF) protection and exists only for tests and demos.

## 6. Pre-flight: find the safe request rate (once per site, then quarterly)

Duration is decided by the requests per second the site's edge accepts, not by the tool (it holds 30 req/s on the
simulated edge). Measure the real ceiling **from the audit machine**:

```bash
python bench/live_edge_matrix.py --urls sample_200_urls.txt --primary-domain bajajfinserv.in --levels 6,8,10,12,15,18 --window-min 3 --out edge_matrix.json
```

1. Use ~200 representative page URLs (`sample_200_urls.txt`, one per line).
2. Read `edge_matrix.json`: the highest level with **0 % 403 / 429 / challenge pages** and stable latency is the safe rate.
3. Pick the audit rate at **80 % of that level** and record it in the site's run sheet (§13).
4. Repeat after CDN / WAF changes, or if an audit reports WAF events.

| Safe rate | Use preset | 10K pages take |
|---|---|---|
| < 10 req/s | Conservative (8 req/s) | ~23 min |
| 12 req/s | 15–20 min target | ~15 min |
| 14 req/s | Max tested | ~13 min |
| ≥ 18 req/s | Enterprise 150K (16, adaptive to 18) | ~11 min (measured) |

300,000 pages in ≤ 4 h needs the edge to accept **≥ 23 req/s** sustained.

## 7. Procedure — run an audit from the Command Center

### 7.1 Start

1. Open <http://127.0.0.1:8002/dashboard/> (or the server's proxied URL).
2. Confirm no other audit is running for the same domain (Command Center → running audits). Only one crawl per domain
   is allowed; a second one is refused with 409.

### 7.2 Configure

1. Click **New audit** → **From sitemap**.
2. Enter the sitemap URL, the `robots.txt` URL, or just the domain → **Discover sitemaps**.
3. Review the sitemap tree. Check every file's health: HTTP status, URL count, duplicates, size, content type,
   response time. A file in red (404, WAF page, invalid XML) is reported, not silently skipped — tell the site owner.
4. Select the sitemaps to include. Add **Include / Exclude URL patterns** if only part of the site is needed.
   Use **Max pages** for a trial run (e.g. 500).
5. **Primary domain** — the registrable domain (`bajajfinserv.in`); every sub-domain shares its budget.
6. **Speed preset** — the one chosen in §6. The wizard shows the predicted duration; if it exceeds your window,
   lower the scope, not the safety margin.
7. Optional: tick **Save as a reusable sitemap profile** (e.g. "Loans weekly") for recurring runs.
8. **Launch audit**.

Alternative inputs: **Paste URLs** (one per line) or **Upload file** (`.txt`, `.csv`, `.xlsx`). Invalid lines and
duplicates are not lost — they are listed in `17_input_exceptions.csv` with the reason.

### 7.3 Monitor (check at least every 15 minutes for long runs)

On the audit's **Overview** tab:

| Signal | Normal | Action if not |
|---|---|---|
| Lifecycle pill | RUNNING | see §9 |
| REQ / S (PRIMARY) | at or just under the preset rate | far below for > 10 min → check network / server CPU |
| WAF EVENTS | 0 | > 0: the engine cools down and lowers the rate by itself; if events repeat, **Pause** and follow §9.2 |
| THROTTLE REQ/S | equal to the preset | dropping = the engine is backing off because of WAF signals |
| Failed / blocked counters | close to 0 | spike → open URLs tab, filter by status, look at error type |
| ETA | stable or falling | rising steadily → edge slowing; consider pausing out of business hours |

### 7.4 Pause / resume / cancel

* **Pause** — graceful; in-flight work is committed. Use for maintenance windows or WAF trouble.
* **Resume** — continues from the checkpoint; nothing already done is repeated.
* **Cancel** — stops for good, keeps completed data queryable, builds no report, cannot be resumed. Use only when the
  configuration was wrong; then launch a new audit.

### 7.5 Completion

When the pill shows **COMPLETED** the report package is built automatically (≈ 25 s per 10K pages).
**PARTIAL** means some pages ended FAILED / BLOCKED / TIMEOUT — the report is still valid for the rest; see §8.3.

## 8. Procedure — validate before sharing (mandatory)

Never forward numbers from a run that has not passed these checks.

1. **Reports tab → trust gate**: all **35/35 checks PASS** and the integrity check is `ok`.
   Any FAIL: do not distribute; raise to the operator with `11_reconciliation.csv`.
2. **Manifest** (`00_manifest.csv`): every file has status OK; SHA-256 is verified against the ZIP automatically.
3. **Input accounting**: pages audited + rows in `17_input_exceptions.csv` = lines supplied.
4. **Spot-check 5 issues** (Issues tab → open issue → evidence): open the page in a browser and confirm the problem
   is real (e.g. the link really returns 404). Record the result in the run sheet.
5. **PARTIAL runs**: on the URLs tab filter `crawl_state` ≠ COMPLETED; if the failures cluster on one section or
   coincide with WAF events, resume or re-run that section at a lower rate before reporting.
6. Download the ZIP (**Download ZIP**) and store it with the run sheet (§13).

## 9. Incident handling

### 9.1 Server or machine crash, container restart

The audit shows **INTERRUPTED**. Restart the server (`python run.py`), open the audit, click **Resume**. The engine
continues from its checkpoint without duplicating pages, relationships or issues (tested with a SIGKILL mid-run and
a container restart at 250K of 300K pages).

### 9.2 WAF blocks (403 / 429 / challenge pages)

1. The engine reacts automatically: cooldown (default 120 s), then a lower rate.
2. If WAF events keep rising: **Pause**.
3. Inform the site/CDN owner with the time window and the audit machine IP.
4. Re-measure (§6); resume with a lower preset (stop the run, start a new one at the lower rate, or resume and accept
   the adaptive rate).
5. Never work around a WAF by rotating IPs, spoofing user agents or disabling robots.txt — prohibited.

### 9.3 Disk full

Symptoms: audit FAILED with "database or disk is full" / "No space left on device".
Free space (old audits' report folders under `reports/onpage/`, old databases), confirm the reserve from §5.1,
then **Resume** — crawled pages are kept; only the report build is repeated.

### 9.4 Report build or trust gate fails

Keep the database. Send `11_reconciliation.csv`, the audit id and `events.jsonl` to the operator / developer.
Do not edit CSVs by hand to make numbers match.

### 9.5 Escalation

| Severity | Example | Who | Target response |
|---|---|---|---|
| P1 | Site owner reports load / WAF incident caused by the audit | Operator: **pause immediately**, then site owner | 15 min |
| P2 | Trust gate FAIL, crash that does not resume | Operator → developer | 1 business day |
| P3 | Wrong classification, cosmetic report issue | Developer backlog | next release |

## 10. Issue triage and remediation workflow

1. **Issues tab**: filter Severity = HIGH first, then MEDIUM. Each issue is unique per page + rule + subject; a broken
   URL linked from 400 pages is one destination with 400 page rows — fix it once.
2. Open an issue: read **evidence** and **recommended action**.
3. Set status **IN_PROGRESS** with a note (owner / ticket id) when assigned; **DISMISSED** with a reason if it is a
   deliberate exception (e.g. an intentional noindex).
4. **Remediation tab → Generate from issues** builds the fix plan (grouped by destination URL; site-wide items when a
   problem affects > 25 pages). Export or share it with the owning team.
5. After fixes are deployed, **re-run the same profile**. The new audit automatically:
   * marks issues that are no longer detected (and whose page was audited successfully) **VERIFIED**;
   * **re-opens** VERIFIED / RESOLVED issues that are detected again (regression).
6. A lead reviews VERIFIED issues and sets **RESOLVED**.

Priority rules: HIGH = broken pages/links, server errors, redirect loops, soft-404s, noindex on pages that should
rank, canonical to error pages. Fix sitemap-listed pages before external links. Site-wide template issues (header,
footer) first — one fix clears thousands of rows.

## 11. Reports reference

| File | Use |
|---|---|
| `00_manifest.csv` | File list with rows, size, SHA-256 — check first |
| `01_summary.csv` | Totals, rates, issues by severity, health score, reconciliation status |
| `02_source_page_audit.csv` | URL audit — one row per sitemap page: status, redirects, canonical, robots, title/description/H1, word count |
| `03_url_audit.csv` | Link audit — every page → URL relationship with type, rel, status, business / page type of the source page |
| `04_unique_url_validation.csv` | Each unique URL checked once: status, final URL, redirect chain |
| `05_image_audit.csv` · `06_unique_image_validation.csv` | Every image placement · each unique image |
| `07_broken_urls.csv` · `08_redirect_urls.csv` · `09_broken_images.csv` · `10_external_urls.csv` | Ready-made work lists |
| `11_reconciliation.csv` | The 35 trust-gate checks |
| `12_issues.csv` | One row per issue with severity, evidence, recommended action, status |
| `13_issue_summary.csv` · `14_page_issue_summary.csv` | Per issue type · per page, worst first |
| `15_seo_page_audit.csv` · `16_sitemap_audit.csv` | SEO facts per page · health per sitemap file |
| `17_input_exceptions.csv` | Input lines that were invalid or duplicates, with the reason |

Excel tip: open CSVs with *Data → From Text/CSV*, UTF-8. Cells that start with `= + - @` are prefixed with `'` on
purpose (formula-injection protection).

## 12. Command-line procedure (scheduled / very large runs)

```bash
python run_on_page_audit.py --urls sitemap_urls.txt --job-id loans_2026w47 --primary-domain bajajfinserv.in --target-rps 12 --max-rps 12 --source-concurrency 20 --dest-concurrency 20 --image-concurrency 30 --out-dir reports/onpage
```

* Re-run the same command (same `--job-id`) to **resume** after any interruption.
* Graceful stop: `--stop-file stop.flag`, then create `stop.flag`.
* Images on their own CDN with a separate budget: `--host-budget cms-assets.bajajfinserv.in=20`.
* `--events-log events.jsonl` keeps a per-stage event log for incident analysis.
* `python run_on_page_audit.py -h` lists every option.

## 13. Records to keep per run (run sheet)

| Field | Example |
|---|---|
| Audit id / name | `onpage_1b0adca6e100` / Loans weekly |
| Date, start–end time (IST) | 2026-11-20 22:00–22:16 |
| Scope | sitemaps selected, include/exclude patterns, pages |
| Preset and agreed safe rate | 15–20 min target, 12 req/s (edge matrix 2026-11-01: 15 req/s clean) |
| Result | COMPLETED / PARTIAL, WAF events, failed pages |
| Trust gate | 35/35 PASS, manifest OK |
| Health score | 53.3 (qcv5-health-1.0) |
| Spot-check | 5/5 confirmed |
| ZIP location | shared drive path |
| Operator / analyst | names |

## 14. Housekeeping

* Keep the last 4 runs per profile online (issue lifecycle compares against earlier runs of the same domain); archive
  older ZIPs and delete their report folders.
* Back up the SQLite database (`audit_cache.db`) before upgrades; stop running audits first.
* After each upgrade, run the test suite (`python -m pytest -q --ignore=test_container.py --ignore=test_ex.py
  --ignore=test_html.py --ignore=test_js.py`) and a 500-page trial audit.

## 15. Checklists

**Before launch** — ☐ permission for the rate · ☐ safe rate measured within 3 months · ☐ disk reserve (§5.1) ·
☐ no other crawl on the domain · ☐ sitemap tree healthy / problems noted · ☐ preset matches run sheet ·
☐ trial run (Max pages 500) for a new site or profile.

**During** — ☐ RUNNING · ☐ WAF events 0 · ☐ rate ≈ preset · ☐ ETA plausible.

**After** — ☐ COMPLETED (or PARTIAL explained) · ☐ 35/35 trust gate · ☐ manifest OK · ☐ input accounting ·
☐ 5 issues spot-checked · ☐ ZIP archived · ☐ run sheet filled · ☐ HIGH issues assigned.

## 16. Revision history

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-11-20 | First issue (backend hardening release: issue lifecycle, health score, manifest, per-host connection pools) |
