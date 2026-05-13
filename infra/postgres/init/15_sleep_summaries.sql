-- sleep_summaries stores the assistant's nightly HealthKit + bedroom-observer
-- inference, plus the user's Telegram confirmation/correction.
CREATE TABLE IF NOT EXISTS sleep_summaries (
    id BIGSERIAL PRIMARY KEY,
    household_member_id INTEGER REFERENCES household_members(id) ON DELETE SET NULL,
    night_of DATE NOT NULL,
    asleep_at TIMESTAMPTZ,
    awake_at TIMESTAMPTZ,
    duration_minutes INTEGER,
    deep_sleep_minutes INTEGER,
    observer_likely_asleep_at TIMESTAMPTZ,
    observer_likely_awake_at TIMESTAMPTZ,
    interruptions INTEGER NOT NULL DEFAULT 0,
    guessed_quality TEXT,
    guessed_reasoning TEXT,
    confirmed_quality TEXT,
    confirmed_at TIMESTAMPTZ,
    confirmed_by_chat_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (household_member_id, night_of)
);

CREATE INDEX IF NOT EXISTS sleep_summaries_member_night_idx
    ON sleep_summaries (household_member_id, night_of DESC);
CREATE INDEX IF NOT EXISTS sleep_summaries_pending_confirmation_idx
    ON sleep_summaries (created_at DESC)
    WHERE confirmed_quality IS NULL;
