-- QC V6 schema v1 (blueprint §4). State lives here; bulk link/image facts go to Parquet (qc6.facts).

CREATE TABLE IF NOT EXISTS workspaces (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS members (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, email)
);

CREATE TABLE IF NOT EXISTS sites (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    base_url      TEXT NOT NULL,
    primary_host  TEXT NOT NULL,
    settings      JSONB NOT NULL DEFAULT '{}'::jsonb,     -- speed, manners, schedule, device mix
    access_token  TEXT NOT NULL,                          -- X-QC-Audit allowlist token (never returned in lists)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, primary_host)
);

-- One token bucket per host, shared by every worker (blueprint §10.2).
CREATE TABLE IF NOT EXISTS host_budget (
    host          TEXT PRIMARY KEY,
    rate_rps      DOUBLE PRECISION NOT NULL CHECK (rate_rps > 0),
    burst         DOUBLE PRECISION NOT NULL CHECK (burst >= 1),
    tokens        DOUBLE PRECISION NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    paused_until  TIMESTAMPTZ,
    pause_reason  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('coverage', 'visit', 'journey', 'full')),
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'pausing', 'paused', 'completed', 'failed', 'cancelled')),
    options       JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress      JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary       JSONB NOT NULL DEFAULT '{}'::jsonb,
    error         TEXT,
    created_by    BIGINT REFERENCES members(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS runs_site_created ON runs (site_id, created_at DESC);

-- Work queue (SELECT ... FOR UPDATE SKIP LOCKED).
CREATE TABLE IF NOT EXISTS jobs (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT REFERENCES runs(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                    -- coverage | visit_plan | visit | journey | analyse
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts      INT NOT NULL DEFAULT 0,
    locked_by     TEXT,
    locked_at     TIMESTAMPTZ,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (kind, status, id);

CREATE TABLE IF NOT EXISTS templates (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    fingerprint   BIGINT NOT NULL,                  -- SimHash centroid (signed 64-bit)
    name          TEXT NOT NULL,
    path_pattern  TEXT NOT NULL DEFAULT '',
    page_count    INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS templates_site ON templates (site_id);

CREATE TABLE IF NOT EXISTS pages (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    path          TEXT NOT NULL,
    template_id   BIGINT REFERENCES templates(id) ON DELETE SET NULL,
    http_status   INT,
    final_url     TEXT,
    title         TEXT,
    facts         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- raw SEO facts from the coverage crawl
    fetched_at    TIMESTAMPTZ,
    UNIQUE (run_id, url)
);
CREATE INDEX IF NOT EXISTS pages_run_template ON pages (run_id, template_id);

CREATE TABLE IF NOT EXISTS visits (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    page_url      TEXT NOT NULL,
    template_id   BIGINT REFERENCES templates(id) ON DELETE SET NULL,
    device        TEXT NOT NULL CHECK (device IN ('desktop', 'mobile')),
    status        TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'challenged', 'skipped')),
    http_status   INT,
    vitals        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- lcp_ms, cls, inp_ms, ttfb_ms, fcp_ms
    a11y          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- axe violations (id, impact, count, help)
    render_diff   JSONB NOT NULL DEFAULT '{}'::jsonb,   -- only-in-rendered: h1, links, canonical, text ratio
    console       JSONB NOT NULL DEFAULT '[]'::jsonb,
    consent       JSONB NOT NULL DEFAULT '{}'::jsonb,
    screenshot    TEXT,                                 -- object key
    timeline      JSONB NOT NULL DEFAULT '[]'::jsonb,   -- human steps with timings (load, think, scroll …)
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms   INT
);
CREATE INDEX IF NOT EXISTS visits_run ON visits (run_id);

CREATE TABLE IF NOT EXISTS journeys (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    spec          JSONB NOT NULL,                       -- parsed YAML (blueprint §8.1)
    source_yaml   TEXT NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (site_id, name)
);

CREATE TABLE IF NOT EXISTS journey_runs (
    id            BIGSERIAL PRIMARY KEY,
    journey_id    BIGINT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
    run_id        BIGINT REFERENCES runs(id) ON DELETE SET NULL,
    device        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'error')),
    failed_step   INT,
    failure_text  TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms   INT
);

CREATE TABLE IF NOT EXISTS step_results (
    id              BIGSERIAL PRIMARY KEY,
    journey_run_id  BIGINT NOT NULL REFERENCES journey_runs(id) ON DELETE CASCADE,
    idx             INT NOT NULL,
    action          TEXT NOT NULL,
    label           TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'skipped')),
    detail          TEXT NOT NULL DEFAULT '',
    screenshot      TEXT,
    duration_ms     INT,
    UNIQUE (journey_run_id, idx)
);

-- Stable unit of work across runs (blueprint §4, §10.4).
CREATE TABLE IF NOT EXISTS fixes (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    fix_key       TEXT NOT NULL,
    rule          TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    dimension     TEXT NOT NULL,
    scope         TEXT NOT NULL CHECK (scope IN ('template', 'subject', 'page')),
    template_id   BIGINT REFERENCES templates(id) ON DELETE SET NULL,
    subject       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'in_progress', 'fixed_pending_check', 'verified', 'wont_fix')),
    owner_id      BIGINT REFERENCES members(id) ON DELETE SET NULL,
    pages_affected INT NOT NULL DEFAULT 0,
    first_seen_run BIGINT REFERENCES runs(id) ON DELETE SET NULL,
    last_seen_run  BIGINT REFERENCES runs(id) ON DELETE SET NULL,
    regressed     BOOLEAN NOT NULL DEFAULT false,
    ticket_url    TEXT,
    source        TEXT NOT NULL CHECK (source IN ('coverage', 'visit', 'journey', 'integration')),
    texts         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- rendered title / why / fix / verify for people
    example_url   TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (site_id, fix_key)
);
CREATE INDEX IF NOT EXISTS fixes_site_status ON fixes (site_id, status);

CREATE TABLE IF NOT EXISTS findings (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    fix_id        BIGINT NOT NULL REFERENCES fixes(id) ON DELETE CASCADE,
    page_url      TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
    source        TEXT NOT NULL CHECK (source IN ('coverage', 'visit', 'journey', 'integration')),
    UNIQUE (run_id, fix_id, page_url, subject)
);
CREATE INDEX IF NOT EXISTS findings_fix ON findings (fix_id, run_id);

CREATE TABLE IF NOT EXISTS fix_events (
    id            BIGSERIAL PRIMARY KEY,
    fix_id        BIGINT NOT NULL REFERENCES fixes(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                 -- found | assigned | status | regressed | verified | comment | ticket
    actor_id      BIGINT REFERENCES members(id) ON DELETE SET NULL,
    run_id        BIGINT REFERENCES runs(id) ON DELETE SET NULL,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fix_events_fix ON fix_events (fix_id, created_at);

CREATE TABLE IF NOT EXISTS score_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    run_id        BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    formula       TEXT NOT NULL,
    score         DOUBLE PRECISION NOT NULL,
    dimensions    JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, formula)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  BIGINT REFERENCES workspaces(id) ON DELETE CASCADE,
    actor_id      BIGINT REFERENCES members(id) ON DELETE SET NULL,
    action        TEXT NOT NULL,
    target        TEXT NOT NULL,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
