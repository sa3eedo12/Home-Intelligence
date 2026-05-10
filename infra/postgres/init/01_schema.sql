CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash text PRIMARY KEY,
    vector real[],
    model text,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id bigserial PRIMARY KEY,
    agent text,
    severity text,
    topic text,
    payload jsonb,
    created_at timestamptz DEFAULT now(),
    acknowledged_at timestamptz
);

CREATE TABLE IF NOT EXISTS reminders (
    id bigserial PRIMARY KEY,
    user_id text,
    text text,
    due_at timestamptz,
    recurrence text,
    status text DEFAULT 'pending',
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflows (
    id uuid PRIMARY KEY,
    status text,
    payload jsonb,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS capabilities_registry (
    agent text,
    capability text,
    description text,
    manifest jsonb,
    last_seen timestamptz,
    PRIMARY KEY (agent, capability)
);
