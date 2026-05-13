CREATE TABLE IF NOT EXISTS presence_returns (
    id BIGSERIAL PRIMARY KEY,
    household_member_id INTEGER REFERENCES household_members(id) ON DELETE SET NULL,
    entity_id TEXT,
    person TEXT,
    left_at TIMESTAMPTZ,
    returned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    away_minutes INTEGER,
    guessed_context TEXT,
    guessed_confidence REAL,
    guessed_reasoning TEXT,
    confirmed_context TEXT,
    confirmed_at TIMESTAMPTZ,
    confirmed_by_chat_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS presence_returns_returned_idx
    ON presence_returns (returned_at DESC);
CREATE INDEX IF NOT EXISTS presence_returns_person_history_idx
    ON presence_returns (person, confirmed_at DESC)
    WHERE confirmed_context IS NOT NULL;
