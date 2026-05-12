CREATE TABLE IF NOT EXISTS things (
    id serial PRIMARY KEY,
    type text NOT NULL,
    friendly_name text NOT NULL,
    attributes jsonb DEFAULT '{}'::jsonb,
    ha_entity_ids text[] DEFAULT '{}'::text[],
    photo_path text NULL,
    confidence real DEFAULT 0.0,
    learned_at timestamptz DEFAULT now(),
    last_confirmed_at timestamptz NULL,
    source text
);

CREATE TABLE IF NOT EXISTS habits (
    id serial PRIMARY KEY,
    subject text NOT NULL,
    pattern jsonb NOT NULL,
    frequency text,
    confidence real DEFAULT 0.0,
    last_observed_at timestamptz NULL,
    source text,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS preferences (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    confidence real DEFAULT 0.0,
    source text,
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS routines (
    id serial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    steps jsonb NOT NULL,
    schedule jsonb NULL,
    last_run_at timestamptz NULL,
    source text,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_things_type ON things (type);
CREATE INDEX IF NOT EXISTS idx_habits_subject ON habits (subject);
CREATE INDEX IF NOT EXISTS idx_routines_name ON routines (name);
