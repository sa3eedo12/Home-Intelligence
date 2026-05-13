-- cleaning_runs tracks every completed vacuum cleaning event, the system's
-- best guess about room coverage, and the user's confirmation when they reply
-- via Telegram or the dashboard. The accumulated history feeds future expected
-- room-pattern inference.
CREATE TABLE IF NOT EXISTS cleaning_runs (
    id BIGSERIAL PRIMARY KEY,
    event_log_id BIGINT REFERENCES event_log(id) ON DELETE SET NULL,
    entity_id TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_seconds INTEGER,
    reported_rooms TEXT[] NOT NULL DEFAULT '{}',
    expected_rooms TEXT[] NOT NULL DEFAULT '{}',
    missed_rooms TEXT[] NOT NULL DEFAULT '{}',
    guessed_status TEXT,
    guessed_reasoning TEXT,
    confirmed_status TEXT,
    confirmed_at TIMESTAMPTZ,
    confirmed_by_chat_id BIGINT,
    attributes_at_finish JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cleaning_runs_ended_idx
    ON cleaning_runs (ended_at DESC);
