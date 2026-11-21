-- Phase 3-5: schedules, alert log, visual change. Additive only.
CREATE TABLE IF NOT EXISTS schedule_state (
    key           TEXT PRIMARY KEY,                -- 'site:<id>:check' | 'journey:<id>'
    last_fired_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts_sent (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    run_id        BIGINT REFERENCES runs(id) ON DELETE SET NULL,
    channel       TEXT NOT NULL,                   -- slack | teams
    kind          TEXT NOT NULL,                   -- summary | regressed | journey_failed | access
    status        INT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE visits ADD COLUMN IF NOT EXISTS visual_change DOUBLE PRECISION;   -- share of pixels changed vs the previous check
ALTER TABLE fixes ADD COLUMN IF NOT EXISTS ticket_key TEXT;                   -- GH-123 / PROJ-45
