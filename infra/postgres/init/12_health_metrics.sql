CREATE TABLE IF NOT EXISTS health_metrics (
    id bigserial PRIMARY KEY,
    metric text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NULL,
    value double precision NULL,
    unit text NULL,
    source text NOT NULL DEFAULT 'health_auto_export',
    member_id int NULL,
    metadata jsonb DEFAULT '{}',
    raw jsonb DEFAULT '{}',
    received_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS health_metrics_metric_started_idx
  ON health_metrics(metric, started_at DESC);
CREATE INDEX IF NOT EXISTS health_metrics_member_idx
  ON health_metrics(member_id);
CREATE UNIQUE INDEX IF NOT EXISTS health_metrics_dedupe_idx
  ON health_metrics(metric, started_at, COALESCE(value, 0), source);
