CREATE TABLE IF NOT EXISTS renewals (
    id bigserial PRIMARY KEY,
    label text,
    renews_on date,
    lead_days int DEFAULT 14,
    status text DEFAULT 'active',
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS appointments (
    id bigserial PRIMARY KEY,
    title text,
    starts_at timestamptz,
    ends_at timestamptz,
    location text,
    notes text,
    created_at timestamptz DEFAULT now()
);
