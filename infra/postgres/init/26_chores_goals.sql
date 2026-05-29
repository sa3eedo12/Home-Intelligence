-- Chore tracking, health goals, member nag windows.
-- One migration because the three live together: the chore system feeds
-- workout-related goal progress, and both gate notifications through
-- the per-member nag window preferences.

-- ── Member nag windows ─────────────────────────────────────────
-- Per-member weekday/weekend quiet hours. Defaults give a reasonable
-- "don't ping me during work" baseline; users tune via the natural-
-- language Telegram path or via the dashboard.
CREATE TABLE IF NOT EXISTS member_nag_windows (
    member_id           int PRIMARY KEY REFERENCES household_members(id) ON DELETE CASCADE,
    weekday_start_hour  int NOT NULL DEFAULT 14,
    weekday_end_hour    int NOT NULL DEFAULT 21,
    weekend_start_hour  int NOT NULL DEFAULT 10,
    weekend_end_hour    int NOT NULL DEFAULT 21,
    timezone            text NOT NULL DEFAULT 'Asia/Dubai',
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT member_nag_windows_weekday_range CHECK (
        weekday_start_hour BETWEEN 0 AND 23
        AND weekday_end_hour BETWEEN 0 AND 24
        AND weekday_end_hour > weekday_start_hour
    ),
    CONSTRAINT member_nag_windows_weekend_range CHECK (
        weekend_start_hour BETWEEN 0 AND 23
        AND weekend_end_hour BETWEEN 0 AND 24
        AND weekend_end_hour > weekend_start_hour
    )
);

-- ── Chore templates + log ──────────────────────────────────────
-- The existing `chores` table is a simple one-off reminder list and
-- we leave it alone. chore_templates + chore_log replace it for
-- recurring household work where cadence + auto-detect matter.
CREATE TABLE IF NOT EXISTS chore_templates (
    id                  bigserial PRIMARY KEY,
    name                text NOT NULL UNIQUE,
    category            text NOT NULL DEFAULT 'general',
    cadence_days        int NOT NULL,
    grace_days          int NOT NULL DEFAULT 1,
    auto_detect_kind    text,                          -- 'vacuum' | 'washer' | 'dryer' | NULL
    auto_detect_entity  text,                          -- e.g. 'vacuum.dreame_l10s'
    default_member_id   int REFERENCES household_members(id) ON DELETE SET NULL,
    description         text,
    active              boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chore_templates_category_idx ON chore_templates(category);
CREATE INDEX IF NOT EXISTS chore_templates_active_idx ON chore_templates(active) WHERE active;

CREATE TABLE IF NOT EXISTS chore_log (
    id                  bigserial PRIMARY KEY,
    chore_template_id   bigint NOT NULL REFERENCES chore_templates(id) ON DELETE CASCADE,
    completed_at        timestamptz NOT NULL DEFAULT now(),
    member_id           int REFERENCES household_members(id) ON DELETE SET NULL,
    source              text NOT NULL DEFAULT 'manual',  -- 'manual' | 'auto_vacuum' | 'auto_washer' | 'dashboard' | 'telegram'
    note                text,
    evidence_event_log_id bigint
);

CREATE INDEX IF NOT EXISTS chore_log_template_idx ON chore_log(chore_template_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS chore_log_member_idx ON chore_log(member_id, completed_at DESC);

-- ── Health goals (per-member, free-form text + structured metric links) ──
-- metric_links carries the structured plan the LLM picked at creation:
--   [{"metric": "workout", "direction": "up", "target_per_week": 4,
--     "days_preferred": ["sun","tue","thu","sat"]},
--    {"metric": "weight", "direction": "down", "target": 88,
--     "unit": "kg", "window": "7d_median"}]
-- workout_budget is optional and only set when the goal includes a
-- workout requirement:
--   {"required_per_week": 4, "flexible_rest_per_week": 2,
--    "days_preferred": ["sun","tue","thu","sat"]}
CREATE TABLE IF NOT EXISTS health_goals (
    id              bigserial PRIMARY KEY,
    member_id       int NOT NULL REFERENCES household_members(id) ON DELETE CASCADE,
    title           text NOT NULL,
    description     text NOT NULL,
    metric_links    jsonb NOT NULL DEFAULT '[]'::jsonb,
    workout_budget  jsonb,
    plan_text       text,
    plan_generated_at timestamptz,
    start_date      date NOT NULL DEFAULT CURRENT_DATE,
    target_date     date,
    status          text NOT NULL DEFAULT 'active',
    quiet_until     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT health_goals_status_check CHECK (
        status IN ('active', 'achieved', 'paused', 'abandoned')
    )
);

CREATE INDEX IF NOT EXISTS health_goals_member_idx ON health_goals(member_id, status);
CREATE INDEX IF NOT EXISTS health_goals_active_idx
    ON health_goals(member_id) WHERE status = 'active';

-- One row per goal per day. metric_snapshots holds the values we
-- captured for each linked metric on that day so the dashboard can
-- show trends without recomputing.
CREATE TABLE IF NOT EXISTS health_goal_progress (
    goal_id              bigint NOT NULL REFERENCES health_goals(id) ON DELETE CASCADE,
    day                  date NOT NULL,
    metric_snapshots     jsonb NOT NULL DEFAULT '{}'::jsonb,
    on_track_score       int,
    on_track_label       text,
    workout_required     boolean NOT NULL DEFAULT false,
    workout_completed    boolean NOT NULL DEFAULT false,
    rest_day_excused     boolean NOT NULL DEFAULT false,
    nags_sent_today      int NOT NULL DEFAULT 0,
    last_nag_at          timestamptz,
    note                 text,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (goal_id, day),
    CONSTRAINT health_goal_progress_score_check CHECK (
        on_track_score IS NULL OR on_track_score BETWEEN 0 AND 100
    ),
    CONSTRAINT health_goal_progress_label_check CHECK (
        on_track_label IS NULL OR on_track_label IN
            ('on_track', 'slipping', 'regressing', 'achieved', 'paused')
    )
);

CREATE INDEX IF NOT EXISTS health_goal_progress_day_idx
    ON health_goal_progress(day DESC);

-- LLM-generated checkpoints. Editable on the dashboard.
CREATE TABLE IF NOT EXISTS health_goal_milestones (
    id              bigserial PRIMARY KEY,
    goal_id         bigint NOT NULL REFERENCES health_goals(id) ON DELETE CASCADE,
    due_date        date NOT NULL,
    target_description text NOT NULL,
    achieved_at     timestamptz,
    status          text NOT NULL DEFAULT 'pending',
    CONSTRAINT health_goal_milestones_status_check CHECK (
        status IN ('pending', 'achieved', 'missed', 'skipped')
    )
);

CREATE INDEX IF NOT EXISTS health_goal_milestones_goal_idx
    ON health_goal_milestones(goal_id, due_date);

-- Audit trail for "what happened on this goal" — also the substrate
-- the weekly reflection job uses to remember context across runs.
CREATE TABLE IF NOT EXISTS health_goal_events (
    id          bigserial PRIMARY KEY,
    goal_id     bigint NOT NULL REFERENCES health_goals(id) ON DELETE CASCADE,
    member_id   int REFERENCES household_members(id) ON DELETE SET NULL,
    ts          timestamptz NOT NULL DEFAULT now(),
    kind        text NOT NULL,    -- 'created'|'paused'|'resumed'|'excused_today'|
                                  -- 'weekly_review'|'achieved'|'abandoned'|
                                  -- 'plan_refreshed'|'nag_sent'|'window_changed'
    note        text
);

CREATE INDEX IF NOT EXISTS health_goal_events_goal_idx
    ON health_goal_events(goal_id, ts DESC);
