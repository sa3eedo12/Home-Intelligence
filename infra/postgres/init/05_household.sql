CREATE TABLE IF NOT EXISTS chores (
    id bigserial PRIMARY KEY,
    title text,
    due_at timestamptz,
    recurrence text,
    status text DEFAULT 'pending',
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shopping_list (
    id bigserial PRIMARY KEY,
    item text,
    qty text,
    checked boolean DEFAULT false,
    added_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pantry (
    id bigserial PRIMARY KEY,
    item text,
    qty numeric,
    unit text,
    expires_on date,
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meal_plans (
    id bigserial PRIMARY KEY,
    plan_date date,
    meal text,
    dish text,
    recipe_id bigint,
    created_at timestamptz DEFAULT now()
);
