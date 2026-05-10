CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts (created_at);
CREATE INDEX IF NOT EXISTS idx_reminders_due_status ON reminders (due_at, status);
CREATE INDEX IF NOT EXISTS idx_workflows_status_updated_at ON workflows (status, updated_at);
