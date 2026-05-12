ALTER TABLE event_log ADD COLUMN IF NOT EXISTS embedding_model text;
CREATE INDEX IF NOT EXISTS event_log_embedding_model_idx ON event_log(embedding_model);

CREATE TABLE IF NOT EXISTS event_log_archive (LIKE event_log INCLUDING ALL);

CREATE TABLE IF NOT EXISTS lora_training_runs (
    id serial PRIMARY KEY,
    started_at timestamptz DEFAULT now(),
    finished_at timestamptz NULL,
    status text NOT NULL DEFAULT 'pending',
    model_base text,
    training_file text,
    quality_score real,
    error text
);

CREATE TABLE IF NOT EXISTS reports (
    id serial PRIMARY KEY,
    kind text NOT NULL,
    period_label text NOT NULL,
    file_path text NOT NULL,
    summary text,
    body_markdown text,
    generated_at timestamptz DEFAULT now(),
    UNIQUE (kind, period_label)
);
