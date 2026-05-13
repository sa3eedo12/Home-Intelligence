CREATE TABLE IF NOT EXISTS tv_left_on (
    id BIGSERIAL PRIMARY KEY,
    event_log_id BIGINT REFERENCES event_log(id) ON DELETE SET NULL,
    entity_id TEXT NOT NULL,
    friendly_name TEXT,
    on_since TIMESTAMPTZ,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    on_hours REAL,
    reason TEXT,                    -- 'nobody_home' | 'past_bedtime' | 'long_idle'
    suggested_action TEXT,
    confirmed_action TEXT,          -- 'turn_off' | 'snooze' | 'always_off_at_bedtime' | 'skip' | NULL
    confirmed_at TIMESTAMPTZ,
    confirmed_by_chat_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tv_left_on_detected_idx ON tv_left_on (detected_at DESC);
