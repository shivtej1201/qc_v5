# QC V6

Site quality checks that work the way a careful person would:

* **Every page** in the sitemap, every link, image and SEO signal on it (the proven QC V5 engine: 300K pages in
  3 h 22 min on a simulated edge).
* **Real visitors**: a sample covering every page type, opened in real Chromium by visitors who wait for the page,
  answer the cookie banner, read, scroll, then measure what a person experienced (speed, layout shift, accessibility,
  content that only appears after JavaScript, browser errors) and take a screenshot.
* **Journeys**: paths customers take (YAML), walked at a person's pace; never presses Submit / Pay.
* **Fixes**, not rows: one fix per root cause, written in plain language (what's wrong, why it matters, how to fix,
  how we'll know), owned by a person, verified by the next check.

QC identifies itself honestly on every request (`QC-Audit/6.0` in the user agent and an `X-QC-Audit` token). It never
disguises itself or gets around bot protection: if a CDN challenges it, it stops and gives you the allow rule.

Blueprint: [`docs/blueprint/ARCHITECTURE.md`](../docs/blueprint/ARCHITECTURE.md) ·
design: [`docs/blueprint/DESIGN_SPEC.md`](../docs/blueprint/DESIGN_SPEC.md).

## Layout

```
v6/
  qc6/
    api/app.py            REST /api/v2 + SSE live events + serves the web app
    workers/runner.py     job queue (SKIP LOCKED) and run orchestration: coverage -> visits -> journeys
    workers/coverage.py   QC V5 engine with honest identity + layout fingerprints; V5 issues -> fixes
    workers/visit.py      real-visitor checks (Playwright, human pacing, web-vitals, axe, render diff)
    workers/visit_stage.py visit plan (every page type) and visit findings (projected to the page type)
    workers/journey.py    YAML journeys, human typing/clicks, irreversible-action guard, plain failure text
    workers/analysis.py   findings -> fixes -> lifecycle -> score
    domain/               templates (SimHash), rules (plain-language catalogue + copy linter), fixes, score
    crawl/                pacing, per-host token bucket (Postgres), bot-challenge detector + allow rules
    db/                   asyncpg pool, migrations
    services.py           workspace / site / journey / run / fix changes (+ audit log)
    assets/               axe-core (MPL-2.0) and web-vitals (Apache-2.0), vendored
  web/                    React + TypeScript + Vite + GSAP (Today, Live run, Fixes, Page, Journeys,
                          Integrations, Crawler access)
  tests/                  core, real Chromium visits, journeys, full pipeline on Postgres, API
  scripts/demo.py         local demo site + seed data
```

## Run it locally

Needs Python 3.11+, Node 20+, PostgreSQL 16 and Playwright's Chromium.

```bash
cd v6
pip install fastapi uvicorn asyncpg httpx playwright pyyaml duckdb pyarrow pillow
python -m playwright install chromium
(cd web && npm ci && npm run build)
export QC6_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/qc6
python -m qc6.db.migrate
uvicorn qc6.api.app:app --port 8003        # web app + API on http://127.0.0.1:8003
python -m qc6.workers.runner               # in a second terminal: runs the checks
```

Demo with a local test site (private addresses are blocked unless you allow them; only for local testing):

```bash
QC6_ALLOW_PRIVATE_TARGETS=true python scripts/demo.py --port 8892
```

Docker (single box): `docker compose -f v6/docker-compose.yml up --build` from the repository root.

## Tests

```bash
cd v6 && QC6_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/qc6_test python -m pytest -q
```

19 tests: clustering purity, fix grouping and lifecycle, copy linter, pacing, challenge detection, the shared rate
budget on Postgres, real Chromium visits on a fixture site (finds JS-only headings, console errors, accessibility,
layout shift; nothing on the clean page type; a challenge stops visits), journeys (pass, explained failure, refusal of
irreversible steps), the full pipeline twice (no false "verified"), and the API (roles, isolation, errors).

## Status against the delivery plan (blueprint §16)

| Phase | State |
|---|---|
| 0 Foundations | Done: schema, migrations, API, web app, compose file. Not done: Keycloak/OIDC (dev auth via `X-QC-User`), CI pipeline |
| 1 Coverage on new storage | Partly: V5 engine runs inside V6 and feeds Postgres; link/image facts still in V5's SQLite + CSV/ZIP, Parquet/DuckDB not yet |
| 2 Fixes and inbox | Done: fix grouping, inbox, drawer, owners, comments, lifecycle, plain-language rules |
| 3 Real-visitor review | Done: clustering, visit plan, visitors, axe, web-vitals, render diff, screenshots, challenge handling. Not done: visual diff between runs |
| 4 Journeys | Done: YAML, runner, filmstrip, guard. Not done: schedules, recorder, alerts |
| 5 Integrations | V5's free integrations exposed in V6. Not done: GSC/GA4-driven sampling, Slack/Jira, Nu HTML, LanguageTool, Lighthouse |
| 6 Hardening | Not started |

Not verified: behaviour against a real CDN (Akamai/Cloudflare) and the 300K-page run inside V6 (the engine itself was
measured in V5). Pausing a running check is not implemented yet (queued checks can be cancelled).
