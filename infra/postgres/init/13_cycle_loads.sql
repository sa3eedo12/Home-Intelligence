-- cycle_loads tracks every completed appliance cycle, the system's best guess
-- for what kind of load it was (e.g. "colors", "delicates", "towels"), and the
-- user's confirmation (or correction) when they reply via Telegram or the
-- dashboard. The accumulated history feeds future inferences.
CREATE TABLE IF NOT EXISTS cycle_loads (
    id BIGSERIAL PRIMARY KEY,
    event_log_id BIGINT REFERENCES event_log(id) ON DELETE SET NULL,
    appliance TEXT NOT NULL DEFAULT 'washer',
    entity_id TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_seconds INTEGER,
    program TEXT,
    brand TEXT,
    attributes_at_finish JSONB NOT NULL DEFAULT '{}'::jsonb,
    guessed_label TEXT,
    guessed_confidence REAL,
    guessed_reasoning TEXT,
    confirmed_label TEXT,
    confirmed_at TIMESTAMPTZ,
    confirmed_by_chat_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cycle_loads_appliance_ended_idx
    ON cycle_loads (appliance, ended_at DESC);
CREATE INDEX IF NOT EXISTS cycle_loads_pending_confirmation_idx
    ON cycle_loads (created_at DESC)
    WHERE confirmed_label IS NULL;
