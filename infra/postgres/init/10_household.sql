CREATE TABLE IF NOT EXISTS household_members (
    id serial PRIMARY KEY,
    name text NOT NULL,
    role text NOT NULL DEFAULT 'adult',
    telegram_chat_id bigint NULL,
    allergies text[] DEFAULT '{}',
    dietary_restrictions text[] DEFAULT '{}',
    sleep_time time NULL,
    wake_time time NULL,
    attributes jsonb DEFAULT '{}',
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS household_members_chat_id_uq
  ON household_members(telegram_chat_id) WHERE telegram_chat_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS household_members_role_idx ON household_members(role);

ALTER TABLE proposals ADD COLUMN IF NOT EXISTS for_member_id int NULL;
ALTER TABLE things    ADD COLUMN IF NOT EXISTS owner_member_id int NULL;
ALTER TABLE habits    ADD COLUMN IF NOT EXISTS last_confirmed_at timestamptz NULL;

ALTER TABLE proposals DROP CONSTRAINT IF EXISTS proposals_kind_check;
ALTER TABLE proposals ADD CONSTRAINT proposals_kind_check CHECK (
    kind IN (
        'code_change',
        'habit_inference',
        'preference_inference',
        'routine_inference',
        'cleanup_action',
        'suggested_action',
        'auto_action',
        'household_inference'
    )
);
