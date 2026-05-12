CREATE TABLE IF NOT EXISTS event_log (
    id serial PRIMARY KEY,
    ts timestamptz DEFAULT now(),
    agent text,
    capability text,
    summary text,
    payload jsonb
);

CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_event_log_agent_ts ON event_log (agent, ts DESC);
