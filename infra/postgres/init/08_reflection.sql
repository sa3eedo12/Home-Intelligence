CREATE TABLE IF NOT EXISTS morning_brief (
    id serial PRIMARY KEY,
    generated_at timestamptz DEFAULT now(),
    summary text,
    body_json jsonb NOT NULL,
    sent_at timestamptz NULL
);

CREATE TABLE IF NOT EXISTS user_profile (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    confidence real DEFAULT 0.0,
    source text,
    last_confirmed_at timestamptz NULL,
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS proposals (
    id serial PRIMARY KEY,
    kind text NOT NULL CHECK (
        kind IN (
            'code_change',
            'habit_inference',
            'preference_inference',
            'routine_inference',
            'cleanup_action'
        )
    ),
    title text NOT NULL,
    rationale text,
    evidence_event_ids int[] DEFAULT '{}',
    confidence real DEFAULT 0.0,
    cost_estimate text,
    impact_estimate text,
    status text DEFAULT 'pending' CHECK (
        status IN ('pending', 'accepted', 'dismissed', 'expired', 'auto_confirmed')
    ),
    created_at timestamptz DEFAULT now(),
    resolved_at timestamptz NULL,
    delivery_channel text NULL,
    rejected_at timestamptz NULL
);

CREATE INDEX IF NOT EXISTS idx_morning_brief_generated_at
    ON morning_brief (generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_proposals_status_created_at
    ON proposals (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_profile_updated_at
    ON user_profile (updated_at DESC);
