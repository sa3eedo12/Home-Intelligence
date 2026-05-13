CREATE TABLE IF NOT EXISTS auto_inferences (
  id BIGSERIAL PRIMARY KEY,
  source_event_log_id BIGINT REFERENCES event_log(id) ON DELETE SET NULL,
  source_kind TEXT NOT NULL,
  inference TEXT NOT NULL,
  confidence REAL NOT NULL,
  reasoning TEXT,
  proposed_action JSONB,
  status TEXT NOT NULL DEFAULT 'proposed',  -- 'proposed' | 'confirmed' | 'rejected' | 'skipped' | 'expired'
  confirmed_action_result JSONB,
  confirmed_at TIMESTAMPTZ,
  confirmed_by_chat_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auto_inferences_status_idx ON auto_inferences (status, created_at DESC);
