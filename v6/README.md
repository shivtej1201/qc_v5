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

## Configuration (environment)

| Variable | Purpose |
|---|---|
| `QC6_DATABASE_URL` | Postgres connection |
| `QC6_DATA_DIR` | screenshots, facts (Parquet), V5 report ZIPs |
| `QC6_AUTH` | `proxy` (behind oauth2-proxy + Keycloak/Google; `QC6_TRUSTED_PROXIES`, `QC6_AUTH_HEADER`), `oidc` (Bearer JWT: `QC6_OIDC_JWKS_URL`, `QC6_OIDC_ISSUER`, `QC6_OIDC_AUDIENCE`) or `dev` (refused when `QC6_ENV=production`) |
| `QC6_VISITORS_PER_HOST`, `QC6_VISIT_SHARE` | real-visitor concurrency and their share of a site's speed limit |
| `QC6_NU_URL`, `QC6_LANGUAGETOOL_URL`, `QC6_LIGHTHOUSE_CMD`, `QC6_CHROME_PATH` | optional extra checks (self-hosted Nu HTML checker, LanguageTool, local Lighthouse) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | GA4-led sampling (site setting `ga4_property_id`) and the V5 Search Console integration |
| `QC6_GITHUB_TOKEN`, `QC6_JIRA_TOKEN` | ticket creation (repository / Jira site set per site in Settings) |

Site settings (Settings page or `PATCH /api/v2/sites/{id}`): speed limit, visitors, cookie answer, sitemap,
`check_schedule` ("daily 06:30", "weekdays 07:15", "every 6 hours"), Slack/Teams webhooks and which events alert,
GitHub/Jira. Journeys can carry their own `schedule:`.

## Measured

| Run (simulated site, 30 req/s) | Result |
|---|---|
| Coverage, 2,000 pages | 78 s end to end in V6 (crawl, page types, V5 report package, Parquet facts, fixes): 73,503 links, 50,061 images, trust gate PASS |
| Full, 2,000 pages | 224 s: coverage, then 25 real-browser visits at human pace (6 visitors), then journeys |
| V5 engine alone, 300,000 pages | 3 h 22 min end to end (V5 benchmark) |

## Status against the delivery plan (blueprint §16)

| Phase | State |
|---|---|
| 0 Foundations | Done: schema + migrations, API, web app, compose file, CI workflow (GitHub Actions), sign-in (trusted proxy or OIDC bearer). Keycloak itself is deployed by you (any OIDC provider works) |
| 1 Coverage on new storage | Done: V5 engine inside V6, state in Postgres, link/image facts in Parquet + DuckDB (explorer, CSV export), live progress over SSE, pause/resume |
| 2 Fixes and inbox | Done |
| 3 Real-visitor review | Done, including visual change between checks |
| 4 Journeys | Done: YAML, runner, filmstrip, irreversible-action guard, schedules, recorder (Playwright codegen → steps), Slack/Teams alerts |
| 5 Integrations | Done: GA4-led sampling, Nu HTML checker, LanguageTool, local Lighthouse, GitHub/Jira tickets with status sync, V5 free integrations |
| 6 Hardening | Partly: security headers, SSRF guard on every browser request, webhook/ticket hosts allow-listed, auth tests, dependency audit in CI. Not done: an external security review, a 300K-page run inside V6, behaviour against a real Akamai/Cloudflare edge |

Known limits: in a full check the real-visitor stage starts after the coverage stage (the blueprint runs them in
parallel); visits need the page types the coverage stage produces. Live progress for a check started elsewhere
arrives over SSE only while the page is open.
