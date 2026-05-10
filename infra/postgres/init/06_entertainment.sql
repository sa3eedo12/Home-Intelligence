CREATE TABLE IF NOT EXISTS media_history (
    id bigserial PRIMARY KEY,
    kind text,
    title text,
    status text,
    rated int,
    watched_at timestamptz DEFAULT now()
);
