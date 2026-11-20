# QC V6 — Architecture Blueprint

| | |
|---|---|
| **Audience** | Development lead and engineering team |
| **Status** | Proposal for approval (no code changes until approved) |
| **Replaces** | QC V5 (`qcv5/` package) — its crawl engine is kept and wrapped, everything around it is rebuilt |
| **Companion** | [Design spec](DESIGN_SPEC.md) and clickable prototype (`prototype.html`) |
| **Constraint** | Free and open-source components only. Where nothing free exists, this document specifies the code |

---

## 1. What we are building

QC V6 audits a large website the way a careful human tester would, and reports back the way a senior SEO
consultant would talk to you.

It does three jobs:

1. **Coverage crawl.** Every page in the sitemap (up to 300,000), every link, image and SEO signal on it, each
   unique URL validated once. This is QC V5's engine, proven at 300K pages in 3 h 22 min end to end.
2. **Real-visitor review.** A sample of pages, chosen so every page template is covered, opened in a real browser
   by "visitors" who behave like people: they wait for the page, scroll, let lazy images load, dismiss the cookie
   banner, read for a few seconds and move on. We measure what a person actually experiences: rendered content,
   layout shift, slow paint, broken widgets, console errors, accessibility.
3. **User journeys.** Scripted paths a customer takes (land → browse → search → apply → form), run on a schedule,
   with human pacing and typing. A journey fails when a human would get stuck.

Everything it finds becomes a **fix**, written in plain language, owned by a person, and verified on the next run.

### 1.1 What "human-like" means here (and what it does not)

| We do | We do not |
|---|---|
| Render pages in real Chromium (JavaScript, fonts, lazy images, consent banners) | Spoof browser fingerprints, canvas/WebGL noise, fake plugins |
| Human pacing: one page at a time per visitor, think time 2–12 s, stepwise scrolling, typed input | Rotate IPs, residential proxies, or identities to look like many people |
| Desktop and mobile visitors in the proportions real traffic has (from GA4 when connected) | Solve or bypass CAPTCHAs and bot challenges |
| Identify honestly: real Chromium UA + `QC-Audit/6` suffix, an `X-QC-Audit` header with a per-site token | Ignore robots.txt, `Crawl-delay` or a site's rate limits |
| When a bot challenge appears: stop that host, tell a person exactly what to ask the CDN team | Retry harder after a block |

For our own sites the reliable way through Akamai Bot Manager is an **allowlist rule** keyed on the
`X-QC-Audit` token (or the egress IP). The product generates the exact rule text for the CDN admin (section 7.4).

### 1.2 Goals and hard numbers

| Goal | Target | Basis |
|---|---|---|
| Coverage crawl, 300K pages, complete pipeline (crawl + validation + issues + reports) | ≤ 4 h | V5 measured 3 h 21 min 57 s at 30 req/s (simulated edge) |
| Coverage crawl, 10K pages | ≤ 15 min | V5 measured 11.25 min at 16 req/s |
| Real-visitor review, 300K-page site | every template covered, ≤ 10,000 renders, finishes inside the same 4 h | §6.3 capacity model |
| Journey run (10 journeys × 8 steps) | ≤ 10 min | human pacing ≈ 6 s/step |
| UI: first meaningful paint of any screen | ≤ 1.5 s on a 2019 laptop | budget in design spec |
| Report numbers | every count reconciles with the database; a mismatch fails the run | V5 trust gate (35 checks), carried over |
| Cost | ₹0 licences; runs on one 8-core / 32 GB / 200 GB SSD server | free stack, §9 |

### 1.3 Non-goals (this release)

* Multi-tenant SaaS billing. (Workspaces and roles are in; billing is not.)
* Backlink crawling of the open web. (We read free sources: Bing Webmaster, Search Console, Common Crawl index.)
* Paid SEO data (Ahrefs, Semrush, DataForSEO, Moz).

---

## 2. Architecture at a glance

```
                         ┌──────────────────────────── Browser ────────────────────────────┐
                         │  Web app (React + TypeScript + Vite, GSAP motion, SSE live)      │
                         └───────────────▲───────────────────────────────┬─────────────────┘
                                         │ HTTPS (Caddy, OIDC session)   │ Server-Sent Events
┌────────────────────────────────────────┴───────────────────────────────▼──────────────────┐
│ API service (FastAPI, Python 3.12)                                                         │
│  REST /api/v2 · SSE /api/v2/stream · auth (OIDC via Keycloak) · RBAC · audit log           │
│  Read models: Postgres (state) + DuckDB over Parquet (big facts: links, images)           │
└───────┬───────────────────────────────┬──────────────────────────────┬────────────────────┘
        │ jobs table (SKIP LOCKED)      │ LISTEN/NOTIFY (progress)     │ object storage (MinIO / disk)
┌───────▼────────┐ ┌───────────────────▼──┐ ┌────────────────────┐ ┌──▼─────────────────────┐
│ crawl-worker   │ │ visit-worker          │ │ journey-worker      │ │ integration-worker     │
│ V5 engine:     │ │ Playwright pool,      │ │ Playwright, YAML    │ │ PSI/CrUX, GSC, GA4,     │
│ httpx, per-host│ │ human pacing, axe,    │ │ journeys, human     │ │ Bing, IndexNow, Wayback,│
│ sharded pools  │ │ web-vitals, render    │ │ typing, assertions, │ │ Observatory, Nu HTML,   │
│ rate budgets   │ │ diff, screenshots     │ │ filmstrip, traces   │ │ LanguageTool            │
└───────┬────────┘ └──────────┬───────────┘ └──────────┬──────────┘ └──────────┬─────────────┘
        └──────── shared per-host rate budget (token bucket in Postgres, one per primary host) ─┘
                                         │
┌────────────────────────────────────────▼──────────────────────────────────────────────────┐
│ analysis-worker: rules engine → findings → fixes (grouped) → score → plain-language text  │
│ report-worker: DuckDB → CSV / XLSX / ZIP + manifest + trust gate                          │
└────────────────────────────────────────────────────────────────────────────────────────────┘
 Observability: OpenTelemetry → Prometheus + Grafana + Loki.  Scheduler: Postgres-backed cron.
```

**Shape:** a modular monolith in one repository, deployed as a handful of processes from one image. Each worker is
the same codebase started with a different entry point. Split into separate services only when one needs its own
scaling (the visit-worker is the first candidate).

**Why not microservices now:** one team, one database, and the expensive part (crawling) is already isolated per
process. Kafka, Kubernetes and service meshes would add weeks of work and no capability we need at 300K pages.

---

## 3. Technology choices (all free)

| Layer | Choice | Licence | Why |
|---|---|---|---|
| API | FastAPI + Pydantic v2 + Uvicorn | MIT / BSD | Team already uses it; typed contracts generate OpenAPI and the TS client |
| State DB | PostgreSQL 16 | PostgreSQL | Jobs, issues, lifecycle, users; `SKIP LOCKED` queue; `LISTEN/NOTIFY` for live progress |
| Fact store | Parquet files + DuckDB | Apache-2 / MIT | 11M link rows per 300K run; columnar scans and CSV export are 10–50× faster than SQLite row scans |
| Object storage | MinIO (or local disk in single-box mode) | AGPL-3 (unmodified, network use OK) | Screenshots, traces, filmstrips, ZIPs |
| HTTP crawl | httpx + V5 per-host sharded pools | BSD | Proven: 27.5 pages/s, CPU no longer the bottleneck |
| Browser | Playwright (Python) + Chromium | Apache-2 | Rendering, journeys, screenshots, traces |
| Accessibility | axe-core | MPL-2.0 | Industry standard WCAG checks, runs inside our renders |
| Web vitals | `web-vitals` JS library | Apache-2 | LCP, CLS, INP, TTFB measured in our real-visitor renders |
| Lab performance | Lighthouse CLI (local) | Apache-2 | No API quota; run on one page per template |
| HTML validity | W3C Nu Html Checker (self-hosted jar) | MIT | Markup errors that break parsers and screen readers |
| Language | LanguageTool (self-hosted server) | LGPL-2.1 | Spelling and grammar in titles, headings and body copy |
| Readability | textstat | MIT | Reading level per page |
| Visual diff | pixelmatch (via Node) or our Python version (§10.6) | ISC | Before/after screenshots between runs |
| Auth | Keycloak (OIDC) | Apache-2 | SSO with Google Workspace / Azure AD later, roles |
| Front end | React 18, TypeScript, Vite, TanStack Router/Query/Table/Virtual, Radix UI primitives | MIT | Accessible primitives, virtualised tables for 300K rows |
| Motion | GSAP 3 (standard licence, free) | GSAP "no-charge" | Already in V5; view-scoped timelines |
| Charts | Observable Plot / D3 | ISC | Small, honest charts |
| Reverse proxy | Caddy | Apache-2 | Automatic TLS, simple config |
| Observability | OpenTelemetry, Prometheus, Grafana, Loki | Apache-2 / AGPL-3 | Traces per audit, rate and error dashboards |
| Optional writing help | Ollama + an open model (e.g. Llama 3.1 8B) | MIT + model licence | Suggested title/description rewrites, offline; product works without it |

### 3.1 External free integrations

| Integration | Free tier | Key | Used for |
|---|---|---|---|
| Google PageSpeed Insights + Chrome UX Report | 25,000 PSI calls/day with a key | free Google API key | Real-user field data (CrUX) per origin and URL |
| Google Search Console API + URL Inspection | free | service account | Clicks, impressions, CTR, position; index status per URL |
| Google Analytics 4 Data API | free | service account | Device mix for visitors; top landing pages for sampling and journeys |
| Bing Webmaster Tools API | free | Bing key | Inbound links, crawl issues, submission quota |
| IndexNow | free | self-made key file | Tell Bing/Yandex/Seznam/Naver/Yep which URLs changed after fixes |
| archive.org Wayback CDX | free | none | Page and domain history, "when did this change" |
| Common Crawl URL index (CDX) | free | none | Is a page in Common Crawl (proxy for discoverability by AI crawlers) |
| Mozilla HTTP Observatory API v2 | free | none | Second opinion on security headers |
| Slack / Microsoft Teams incoming webhooks | free | webhook URL | Run summaries and "a fix regressed" alerts |
| GitHub Issues / Jira Cloud (free plan) | free | token | Push a fix to the dev team's tracker, sync status back |
| SMTP (company relay) | free | credentials | Weekly digest e-mail |

Already built in V5 and kept: AI-crawler robots.txt and llms.txt check, security headers, structured data
validation, Google update timeline.

---

## 4. Domain model

```
Workspace ─┬─ Member (role: owner | editor | viewer)
           └─ Site ─┬─ Settings (hosts, budgets, schedule, manners, allowlist token)
                    ├─ Template (cluster of pages sharing a layout)            [§10.1]
                    ├─ Journey ── Step                                         [§8]
                    ├─ Run (kind: coverage | visit | journey | integration)
                    │    ├─ Page        (one per sitemap URL in this run)
                    │    ├─ Visit       (one real-visitor render of a page)
                    │    ├─ JourneyRun ── StepResult (screenshot, timings, assertion)
                    │    ├─ Finding     (rule hit on a subject in this run; immutable)
                    │    └─ ScoreSnapshot
                    ├─ Fix (stable across runs: one problem, many pages)       [§10.4]
                    │    ├─ status: open | in_progress | fixed_pending_check | verified | wont_fix
                    │    ├─ owner, due, comments, external ticket link
                    │    └─ FixEvent (who/what/when; regression = verified → open)
                    └─ Url (unique destination) ── Validation (latest per run)
```

**Key change from V5:** V5 issues are per (page, rule, subject). People fix *problems*, not rows. V6 adds the
**Fix** entity: one Fix groups every Finding with the same root cause ("the product template has no H1": 41,200
pages, one fix). Owners, status and verification live on the Fix. Findings stay immutable per run, so history and
regressions are exact.

### 4.1 Storage split

| Data | Volume (300K run) | Store | Notes |
|---|---|---|---|
| Runs, pages, fixes, users, journeys | ~1M rows | Postgres | Normal OLTP, indexed for the UI |
| Link and image relationships | 11M + 7.5M rows | Parquet (partitioned by run) | Written by the crawl worker in 100K-row row groups; queried with DuckDB |
| Unique URL validations | 0.3M + 1.8M | Postgres (latest) + Parquet (history) | UI reads Postgres, exports read Parquet |
| Findings | 2–3M | Postgres partitioned by run | Fix rollups are materialised |
| Screenshots, traces, filmstrips | ~10K × 300 KB | Object storage | WebP, 7-day retention for traces, 90 days for screenshots |
| Reports (CSV/ZIP) | 9 GB CSV / 0.4 GB ZIP | Object storage | Streamed from DuckDB; never staged in the database |

The V5 disk incident (SQLite WAL growing to 9.6 GB while issues were written during report streaming) cannot
recur: facts stream to Parquet files, and findings are written by the analysis worker before reports start.

---

## 5. Pipeline

```
 Sitemap discovery ─► Inventory ─► Coverage crawl ─────────────► Analysis ─► Fixes ─► Score ─► Reports
                          │          (all pages, HTTP)    ▲          ▲
                          │                               │          │
                          └► Template clustering ─► Visit plan ─► Real-visitor review (sample)
                                                                     │
 Scheduler ─► Journeys (independent, can run daily) ─────────────────┘
 Integrations (PSI/CrUX, GSC, GA4, Bing, Wayback …) ─► enrich pages and fixes ─┘
```

1. **Discovery** — V5 sitemap discovery (indexes, gzip, robots.txt, recovery of `<loc>` from malformed XML).
2. **Inventory** — dedupe, normalise, record exceptions (V5 `17_input_exceptions`).
3. **Coverage crawl** — V5 engine unchanged in behaviour; output goes to Parquet + Postgres instead of SQLite.
4. **Template clustering** — runs on the first 5% of fetched HTML and keeps updating (§10.1).
5. **Visit plan** — per template: up to 25 pages, chosen as the most-visited (GA4) plus random others; plus every
   page the raw HTML flags as a JavaScript shell; plus pages with traffic but thin raw content.
6. **Real-visitor review** — §6.
7. **Analysis** — rules over raw facts + rendered facts + integration data → Findings → Fixes (§10.4).
8. **Score** — V5 `qcv5-health-1.0` extended with an experience dimension (§10.5).
9. **Reports** — V5's 18-file contract (kept for compatibility) + `19_visits.csv`, `20_fixes.csv`,
   `21_journeys.csv`; manifest + trust gate.

---

## 6. Real-visitor review

### 6.1 A visit, step by step

| # | What the visitor does | What we record |
|---|---|---|
| 1 | Opens a fresh browser context (desktop 1366×768 or mobile Pixel 7 profile, locale and timezone of the site's audience) | context profile |
| 2 | Navigates to the URL, waits for `load` + network quiet (500 ms) or 15 s | status, redirect chain, TTFB, FCP |
| 3 | If a consent banner is present, clicks its *Accept* or *Reject* button (configurable) the way a visitor would | banner found, choice, layout shift caused |
| 4 | Pauses (think time, log-normal, median 2.5 s, 1–8 s) | — |
| 5 | Scrolls to the bottom in steps of 60–90% of the viewport, 300–900 ms apart, then back to the top | lazy images loaded, CLS during scroll |
| 6 | Waits for idle, collects `web-vitals` (LCP, CLS, INP from one synthetic tap on the main CTA if the template defines one) | vitals |
| 7 | Runs axe-core (WCAG 2.2 AA rules) | accessibility violations |
| 8 | Takes a full-page screenshot (WebP) and the rendered DOM | screenshot, DOM hash |
| 9 | Compares rendered DOM with the raw HTML the coverage crawl fetched | links/text/H1/canonical/meta only in rendered DOM |
| 10 | Collects console errors, failed requests, mixed content, third-party weight | errors, request log summary |
| 11 | Pauses again (think time) before the visitor's next page | — |

Each visitor visits pages **one after another** in one context for up to 10 pages (like a person browsing), then the
context is closed and a new one starts (fresh cookies, like a new person).

### 6.2 Manners

* **Concurrency:** at most `visitors_per_host` (default 6, max 20) visitors on a host at a time.
* **Budget:** every request a visitor's browser makes to a primary host takes a token from the same per-host bucket
  the coverage crawl uses. Default split: coverage 80%, visits 20% of the host budget.
* **robots.txt and `Crawl-delay`:** honoured for document navigations.
* **Quiet hours:** optional window (e.g. 01:00–06:00 IST) when visits are allowed to run at full budget.
* **Challenge detected** (Akamai/Cloudflare interstitial, 403/429 with CDN markers, `_abck` loop): stop visits on
  that host, keep coverage crawl at its own reduced rate, raise a *Needs a person* card with the allowlist rule.
  Never retry a challenged page within the same run.

### 6.3 Capacity model (why we sample)

| Item | Value |
|---|---|
| Time per visit (load 3 s + think 2.5 s + scroll 3 s + measure 1.5 s) | ≈ 10 s |
| Visitors on the primary host | 20 |
| Visits per hour | 20 × 360 = 7,200 |
| Primary-host requests per visit (document + same-site XHR/API) | 3–10 → 0.6–2 req/s for 20 visitors, inside a 20% share of 30 req/s |
| Rendering **all** 300K pages | 300,000 / 7,200 = **42 h** → not possible in 4 h |
| Rendering a template-covering sample (≈ 200 templates × 25 + JS-shell pages) | 5,000–10,000 visits = **0.7–1.4 h**, in parallel with the coverage crawl |

So the coverage crawl answers *"is every page and link correct?"* and the visits answer *"what does a person
actually experience on each kind of page?"*. A finding on a sampled page is projected to its template ("seen on 25
of 25 sampled product pages → likely on all 41,200").

### 6.4 Worker sizing

One Chromium context ≈ 150–250 MB. 20 visitors ≈ 5 GB RAM, 4 cores. The visit-worker therefore runs as its own
process with a hard cap from config, and the single-box deployment profile is 8 cores / 32 GB.

---

## 7. Coverage crawl (kept from V5, with changes)

### 7.1 Kept as is
Pipelined asyncio engine, per-host sharded connection pools (`http_pool.py`), shared per-host rate budget, adaptive
step-down on WAF signals, SSRF checks on every redirect hop, robots.txt, checkpoints and resume, single parse per
page for links/images/SEO, three-level dedup, issue rules, trust gate.

### 7.2 Changed
| V5 | V6 | Reason |
|---|---|---|
| SQLite (WAL) for everything | Postgres for state, Parquet for facts | Concurrency, disk safety, analytics speed |
| Polling `/progress` | `LISTEN/NOTIFY` → SSE | Live without load |
| Worker = subprocess per audit | Worker processes pull from a jobs table | Restart-safe, multiple runs queued, one host one run |
| Rate budget per process | Rate budget per host in Postgres (token bucket row, updated every 250 ms by the owning worker) | Coverage, visits and journeys share one budget |
| Honest UA string fixed in code | Per-site UA suffix + `X-QC-Audit` token | Allowlisting |

### 7.3 Throughput contract
Coverage crawl at 30 req/s: 27.5 pages/s, 300K in ≈ 3 h; report build ≈ 20 min (V5 measured). The real limit is
what the site's edge allows; the pre-flight edge test (V5 `live_edge_matrix.py`) becomes a button on the site's
settings page and sets the budget.

### 7.4 Allowlist helper
Settings → *Crawler access* shows, per site:
* the egress IP(s) of the workers,
* the `X-QC-Audit` header name and a 32-byte token (rotatable),
* ready-to-send text for the CDN team, e.g. for Akamai: *"Create a Bot Manager custom bot category 'QC Audit'
  matching request header `X-QC-Audit` equals `<token>` (and source IP `<ip>`), action: Allow; exclude from rate
  controls up to 40 req/s."*
* a **Test access** button that makes 20 paced requests and reports whether any were challenged.

---

## 8. User journeys

### 8.1 Journey file (YAML, versioned in the database and exportable)

```yaml
name: Personal loan — check eligibility
site: www.example.com
device: mobile            # mobile | desktop | both
schedule: "daily 06:30 Asia/Kolkata"
persona:                  # human pacing for this journey
  think_time: { median_s: 2.5, max_s: 8 }
  typing: { wpm: 38, mistakes: false }
steps:
  - open: /personal-loan
    expect:
      - visible: "role=heading[name=/Personal Loan/i]"
      - lcp_under_ms: 2500
  - click: "role=link[name='Check eligibility']"
    expect: [ url_contains: /eligibility ]
  - fill:
      "label=Monthly income": "85000"
      "label=City": "Pune"
  - select: { "label=Employment type": "Salaried" }
  - click: "role=button[name='Continue']"
    expect:
      - visible: "text=/You are eligible|Tell us more/"
      - no_console_errors: true
      - no_layout_shift_over: 0.1
  - stop_before: "role=button[name='Submit application']"   # never submit real applications
```

Rules: selectors are accessibility-first (role + name, label), like a person reads the page. A journey never submits
payments or real applications: `stop_before` is mandatory on any step whose button text matches a configurable
"irreversible" list (submit, pay, apply now, confirm).

### 8.2 Runner behaviour
* One browser context per journey run, device profile from the journey.
* Before each action: think time. Clicks: move to the element's centre with a short eased movement, then click.
  Typing: per-character delay from the persona's words-per-minute.
* After each step: screenshot (filmstrip), timings, console errors, network failures, assertion results.
* Failure output is written for a human: *"Step 4 'Continue' — nothing happened for 8 s after the click. The
  button was visible and enabled. 3 console errors appeared: …"* plus the screenshot and the Playwright trace.

### 8.3 Journey recorder (later phase)
Playwright codegen output converted to the YAML format, so a non-developer can record a journey by clicking through it.

---

## 9. Deployment

### 9.1 Single box (default)
Docker Compose on one 8-core / 32 GB / 200 GB SSD Linux server:

| Container | Count | Memory |
|---|---|---|
| caddy | 1 | 64 MB |
| web (static build served by Caddy) | — | — |
| api | 2 | 512 MB each |
| crawl-worker | 1 | 2 GB |
| visit-worker | 1 (20 contexts) | 6 GB |
| journey-worker | 1 (4 contexts) | 1.5 GB |
| integration-worker + analysis-worker + report-worker | 1 each | 1 GB each |
| postgres | 1 | 4 GB |
| minio | 1 | 512 MB |
| keycloak | 1 | 1 GB |
| languagetool, nu-validator | 1 each | 1 GB each |
| prometheus, grafana, loki | 1 each | 1.5 GB total |

Disk: budget 30 GB free per concurrent 300K run (V5 measured peak ~22 GB with SQLite; Parquet is ~40% smaller).

### 9.2 Scale-out
Visit-workers are stateless and can run on additional machines; they only need Postgres, object storage and the
per-host budget. Kubernetes manifests (Helm) come after phase 6, not before.

---

## 10. Components with no free equivalent — specified in code

These are the pieces we write ourselves. Reference implementations are below; the team turns them into tested modules.

### 10.1 Template clustering (SimHash over DOM structure)

```python
# packages/core/templates.py
"""Group pages by layout so the visitor can sample every template.
A page's fingerprint is a 64-bit SimHash of shingles of its tag-path skeleton (no text), so two product pages with
different products hash close together, and a product page and an article hash far apart."""
from __future__ import annotations
import hashlib
from html.parser import HTMLParser

SKIP = {"script", "style", "noscript", "svg", "path", "br", "wbr"}

class _Skeleton(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.paths: list[str] = []
    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            return
        cls = next((v for k, v in attrs if k == "class" and v), "")
        token = tag + ("." + cls.split()[0] if cls else "")      # first class only: stable, low noise
        self.stack.append(token)
        if len(self.stack) <= 12:                                 # deep nesting adds noise, not layout
            self.paths.append("/".join(self.stack[-4:]))
    def handle_endtag(self, tag):
        if tag in SKIP:
            return
        while self.stack:
            if self.stack.pop().split(".")[0] == tag:
                break

def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")

def simhash(html: str, shingle: int = 3) -> int:
    p = _Skeleton(); p.feed(html[:400_000])
    paths = p.paths
    weights = [0] * 64
    for i in range(max(1, len(paths) - shingle + 1)):
        h = _h64("|".join(paths[i:i + shingle]))
        for b in range(64):
            weights[b] += 1 if (h >> b) & 1 else -1
    return sum(1 << b for b in range(64) if weights[b] > 0)

def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()

class TemplateIndex:
    """Online clustering: assign each page to the nearest template within `radius` bits, else start a new one.
    Banding (4 × 16-bit bands) keeps lookups O(1) for 300K pages."""
    def __init__(self, radius: int = 6) -> None:
        self.radius = radius
        self.centroids: list[int] = []
        self.bands: list[dict[int, list[int]]] = [dict() for _ in range(4)]
    def assign(self, fp: int) -> int:
        cands = {tid for i, band in enumerate(self.bands) for tid in band.get((fp >> (16 * i)) & 0xFFFF, [])}
        best = min(cands, key=lambda t: distance(fp, self.centroids[t]), default=None)
        if best is not None and distance(fp, self.centroids[best]) <= self.radius:
            return best
        tid = len(self.centroids)
        self.centroids.append(fp)
        for i, band in enumerate(self.bands):
            band.setdefault((fp >> (16 * i)) & 0xFFFF, []).append(tid)
        return tid
```
Acceptance: on a labelled sample of 2,000 pages from 12 known templates, purity ≥ 0.95 and ≤ 20 clusters.

### 10.2 Visitor pacing and budget

```python
# workers/visit/pacing.py
import math, random, asyncio, time

def think_time(median_s: float = 2.5, sigma: float = 0.5, lo: float = 1.0, hi: float = 8.0) -> float:
    """Log-normal pause: most pauses near the median, a few long ones, like people reading."""
    return min(hi, max(lo, random.lognormvariate(math.log(median_s), sigma)))

def scroll_plan(page_height: int, viewport: int) -> list[tuple[int, float]]:
    """(y, pause_s) steps down the page: 60-90% of a viewport per step, 0.3-0.9 s apart."""
    y, plan = 0, []
    while y < page_height - viewport:
        y = min(page_height - viewport, y + int(viewport * random.uniform(0.6, 0.9)))
        plan.append((y, random.uniform(0.3, 0.9)))
    return plan

class HostBudget:
    """Token bucket shared by every worker for one host. The row lives in Postgres:
       host_budget(host PK, rate_rps, burst, tokens, updated_at, paused_until).
    take() is one UPDATE ... RETURNING, so coverage crawl, visitors and journeys never exceed the site's rate."""
    SQL = """
    UPDATE host_budget SET
      tokens = LEAST(burst, tokens + rate_rps * EXTRACT(EPOCH FROM (now() - updated_at))) - $2,
      updated_at = now()
    WHERE host = $1 AND (paused_until IS NULL OR paused_until < now())
      AND LEAST(burst, tokens + rate_rps * EXTRACT(EPOCH FROM (now() - updated_at))) >= $2
    RETURNING tokens"""
    def __init__(self, pool, host: str, share: float):
        self.pool, self.host, self.share = pool, host, share     # share: this worker's fraction of the rate
    async def take(self, n: float = 1.0) -> None:
        while True:
            row = await self.pool.fetchrow(self.SQL, self.host, n / self.share)
            if row is not None:
                return
            await asyncio.sleep(0.05 + random.random() * 0.05)
```

Note: `n / self.share` charges a low-share worker more per request, so each worker class stays inside its share
without a second bucket. Measured cost of the UPDATE on Postgres 16: ~0.2 ms, i.e. negligible at 30 req/s.

### 10.3 Challenge detector

```python
# workers/common/challenge.py
MARKERS = ("_abck", "ak_bmsc", "bm_sz", "akamai", "cf-chl", "cf_chl", "just a moment...", "attention required",
           "px-captcha", "_incapsula_resource", "datadome", "verify you are human", "access denied")

def is_challenge(status: int | None, headers: dict[str, str], body_head: str) -> str | None:
    """Return the vendor/reason when a response is a bot challenge, else None.
    Generic words count only together with a 403/429/503, so normal pages that mention 'access denied' pass."""
    h = {k.lower(): v.lower() for k, v in headers.items()}
    text = body_head[:20_000].lower()
    vendor = ("akamai" if "akamai" in h.get("server", "") or "_abck" in h.get("set-cookie", "") else
              "cloudflare" if "cloudflare" in h.get("server", "") else
              "imperva" if "x-iinfo" in h else None)
    if status in (403, 429, 503) and (vendor or any(m in text for m in MARKERS)):
        return vendor or "challenge"
    if status == 200 and any(m in text for m in ("just a moment...", "verify you are human", "px-captcha")):
        return vendor or "interstitial"
    return None
```
On a hit: set `host_budget.paused_until` for visits to the end of the run, log the event, create the
*Needs a person* card with the allowlist text. No retries.

### 10.4 Fix grouping (one problem → one fix)

```python
# packages/rules/fixes.py
import hashlib, re

NUM = re.compile(r"\d+")
def path_pattern(url_path: str) -> str:
    """/loans/personal-loan/pune-123 -> /loans/personal-loan/*  (last segment generalised, digits folded)."""
    parts = [p for p in url_path.split("/") if p]
    if not parts:
        return "/"
    parts[-1] = "*"
    return "/" + "/".join(NUM.sub("#", p) for p in parts)

def fix_key(rule: str, template_id: int | None, subject: str, url_path: str, scope: str) -> str:
    """Findings that share a root cause share a key, and so become one Fix.
    scope comes from the rule definition:
      'template'  -> rule fires because of the layout (missing H1, no canonical, CLS from a banner)
      'subject'   -> one broken target linked from many pages (404 link, broken image)
      'page'      -> genuinely page-specific (duplicate title text, thin content)"""
    if scope == "template":
        basis = f"t:{template_id}" if template_id is not None else f"p:{path_pattern(url_path)}"
    elif scope == "subject":
        basis = f"s:{subject}"
    else:
        basis = f"u:{url_path}"
    return hashlib.sha1(f"{rule}|{basis}".encode()).hexdigest()[:16]
```
Fix lifecycle on each run: a Fix whose key produced no findings in a complete run moves `fixed_pending_check →
verified`; a verified Fix that gets findings again moves to `open` with a *regressed* event (V5 lifecycle, now per Fix).

### 10.5 Score: adding "experience"
`qcv6-health-1.0` = technical 30 · links 20 · content 15 · experience 20 · performance 10 · security 5.
Experience = 100 − weighted share of sampled templates with: CLS > 0.1, LCP > 2.5 s, axe *serious/critical*
violations, console errors, content only after JavaScript. Template results are weighted by the number of pages in
the template, so a broken product template (41K pages) moves the score more than a broken 12-page legal template.
The formula version is stored with every snapshot; changing weights creates a new version, never rewrites history.

### 10.6 Visual change between runs (when pixelmatch is not installed)

```python
# packages/core/visual_diff.py
from PIL import Image, ImageChops
def changed_ratio(a_path: str, b_path: str, threshold: int = 24) -> float:
    a = Image.open(a_path).convert("L"); b = Image.open(b_path).convert("L")
    w, h = min(a.width, b.width), min(a.height, b.height)
    diff = ImageChops.difference(a.crop((0, 0, w, h)), b.crop((0, 0, w, h))).point(lambda p: 255 if p > threshold else 0)
    return sum(diff.histogram()[255:]) / float(w * h)
```
A template whose screenshot changed by more than 15% between runs is shown on the run summary ("The product page
looks different since Tuesday").

### 10.7 Plain-language explanations

Every rule ships a message template with four fields, written once by a person and reviewed like code:

```python
RULES["H1_MISSING"] = Rule(
    scope="template", severity="medium", dimension="content",
    title="{n_pages} {template_name} pages have no main heading",
    why="Search engines and screen-reader users use the main heading to understand what a page is about.",
    fix="Add one <h1> to the {template_name} template that states the page's topic, e.g. the product name.",
    verify="Next run: the count drops to 0. You can check one page now: {example_url}",
)
```
Style rules (enforced by a unit test on all templates): second person, active voice, no internal codes
(`SOURCE_PAGE`, `relationship`), numbers formatted for India (`1,23,456` toggle), maximum 140 characters per title.

---

## 11. API v2 (summary)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v2/sites` · `/sites/{id}` | Sites with latest score and run |
| POST | `/api/v2/sites/{id}/runs` | Start a run `{kind, scope, preset}` |
| GET | `/api/v2/runs/{id}` · `/runs/{id}/pages` · `/runs/{id}/visits` | Run detail and explorers (cursor paging) |
| GET | `/api/v2/stream?run={id}` | SSE: progress, visitor events, findings as they appear |
| GET/PATCH | `/api/v2/sites/{id}/fixes` · `/fixes/{key}` | Fix inbox, owner, status, comments |
| POST | `/api/v2/fixes/{key}/ticket` | Create a GitHub/Jira issue for a fix |
| GET/PUT | `/api/v2/sites/{id}/journeys/{jid}` · POST `.../run` | Journeys |
| GET | `/api/v2/sites/{id}/templates` | Templates with page counts and sample results |
| GET | `/api/v2/integrations` · POST `/integrations/{id}/run` | Integrations (V5 contract kept) |
| GET | `/api/v2/runs/{id}/exports/{file}` | CSV/XLSX/ZIP (V5 file names kept) |
| — | `/api/on-page/*` | V5 API kept read-only for one release through an adapter |

Conventions: cursor pagination, `ETag` on read models, RFC 7807 problem responses, OpenAPI → generated TypeScript client.

---

## 12. Security

* **SSRF:** V5 guard kept (every redirect hop, numeric/encoded IPs, metadata IPs); browsers run with
  a request interceptor that applies the same check to every navigation and subresource on primary hosts.
* **Journeys never perform irreversible actions** (`stop_before` enforced, list of blocked button names).
* **Secrets:** environment or Docker secrets; API keys never returned by the API, never logged (log scrubber test).
* **AuthN/Z:** OIDC (Keycloak); roles owner/editor/viewer per workspace; every write in an audit log table.
* **Browser isolation:** visit and journey workers run as an unprivileged user, Chromium sandbox on, no downloads,
  no file system access from pages, one context per visitor.
* **CSV injection:** V5 escaping kept for every export.
* **Supply chain:** pinned dependencies with hashes, Renovate for updates, `pip-audit` and `npm audit` in CI.

---

## 13. Observability and SLOs

| Signal | Where | Alert |
|---|---|---|
| Requests/s per host, challenges, 429s | Prometheus | challenge on a primary host → Slack |
| Worker queue depth, oldest job age | Prometheus | oldest job > 15 min |
| Run ETA vs target | API + Grafana | projected > target by 20% |
| Visit failures by reason | Prometheus | > 10% of visits failing |
| Disk free | node exporter | < 30 GB |
| Traces per run (discover → crawl → analyse → report) | OpenTelemetry → Grafana Tempo (optional) | — |

---

## 14. Testing strategy

| Level | Tooling | Must cover |
|---|---|---|
| Unit | pytest, vitest | rules, fix keys, template clustering, pacing, challenge detector, score |
| Contract | schemathesis on OpenAPI | every v2 endpoint |
| Integration | docker compose + fixture site (V5 `sim_site`/`sim_edge` extended with templates, banners, JS-only pages, a fake Akamai challenge) | full run, visits, journeys, reports, trust gate |
| E2E UI | Playwright | create site → run → live view → fix inbox → assign → verify on re-run; keyboard-only path |
| Performance | V5 `bench/run_10k.py` + new 300K job weekly | 10K ≤ 15 min, 300K ≤ 4 h, no regression > 10% |
| Accessibility of our own UI | axe in Playwright E2E | 0 serious/critical |
| Copy | rule-template linter | §10.7 style rules |

---

## 15. Migration from V5

1. Keep V5 running; V6 reads V5 SQLite runs through an importer (runs, issues → findings, lifecycle → fixes).
2. Move the crawl engine into `workers/crawl` unchanged; replace its storage layer behind an interface
   (`FactsWriter`: Parquet + Postgres) — the engine's tests run against both writers during the transition.
3. Keep the 18 report files byte-compatible (same names and columns) so downstream spreadsheets keep working.
4. Switch the UI per screen: V6 web app first shows *Sites* and *Fix inbox* from imported V5 data.
5. Retire V5 after two complete V6 runs per site match V5 counts within the trust gate.

---

## 16. Delivery plan

| Phase | Weeks | Outcome | Exit criteria |
|---|---|---|---|
| 0. Foundations | 1–2 | Monorepo, CI, Compose stack, Postgres schema, auth, design tokens, API skeleton | `docker compose up` gives a logged-in empty app; CI green |
| 1. Coverage on new storage | 3–5 | V5 engine on Postgres + Parquet, SSE progress, reports from DuckDB | 10K ≤ 15 min, trust gate 100%, byte-compatible CSVs |
| 2. Fixes and inbox | 6–7 | Fix grouping, inbox, owners, comments, lifecycle, plain-language rules | Imported V5 run shows ≤ 1 fix per root cause; copy linter passes |
| 3. Real-visitor review | 8–11 | Template clustering, visit plan, visit worker, axe, web-vitals, render diff, screenshots, challenge handling | Fixture site: every template visited; 300K plan finishes inside the 4 h run |
| 4. Journeys | 12–13 | YAML journeys, runner, filmstrip, schedule, alerts | 10 journeys × 8 steps ≤ 10 min; irreversible-step guard tested |
| 5. Integrations | 14–15 | GSC/GA4-driven sampling, CrUX, Bing, IndexNow on verify, Observatory, Nu, LanguageTool, Slack/Jira | Each integration has a fixture test and a live smoke check |
| 6. Hardening and launch | 16–17 | Load tests, security review, docs, V5 retirement | 300K ≤ 4 h on staging edge; 0 critical findings in security review |

### 16.1 Team

| Role | People | Focus |
|---|---|---|
| Dev lead / architect | 1 | Owns this document, reviews, unblocks |
| Backend | 2 | API, fixes, analysis, reports, integrations |
| Crawl and browser engineer | 1 | Coverage engine, visit and journey workers, performance |
| Front end | 2 | Web app, design system implementation, E2E |
| QA automation | 1 | Fixture site, contract and E2E suites, performance runs |
| Product designer | 1 | Design spec, prototype, usability tests |
| DevOps (part time) | 0.5 | Compose, backups, monitoring |

---

## 17. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| CDN blocks visits despite pacing | Medium | Visits missing | Allowlist helper + test button; visits stop cleanly; coverage continues |
| Template clustering mixes layouts | Medium | Wrong projection | Show templates to the user with example pages; allow merge/split; purity test |
| Browser memory on one box | Medium | Worker crashes | Hard context cap, restart contexts every 10 pages, memory alarm |
| Postgres write load during 300K run | Low | Slower crawl | Facts go to Parquet; Postgres gets batched state only |
| Journeys break on UI changes | High | Noisy failures | Accessibility selectors; failure text distinguishes "selector not found" from "page broken" |
| Scope creep | High | Late delivery | Phase exit criteria are the contract; anything else goes to the backlog |

---

## 18. Decisions log (ADRs to write in phase 0)

1. Modular monolith, one image, many entry points.
2. Postgres for state, Parquet + DuckDB for facts; no Elasticsearch/Kafka.
3. Real-visitor review samples by template; coverage crawl stays HTTP-only.
4. Honest identification + allowlisting; no fingerprint spoofing, no challenge solving.
5. Fix (root cause) is the unit of work; findings are immutable per run.
6. V5 report files stay byte-compatible for one major version.
7. GSAP for motion, scoped per view; every animation has a reduced-motion path.
