-- Phase 5: suggest → confirm × N → auto promotion

-- ────────────────────────────────────────────────────────────────
-- Add lifecycle state to routines so we know whether a candidate
-- is still being evaluated, has been auto-promoted, or was killed.
--   suggested : new candidate from routine_sequence_miner — not
--               yet user-validated
--   active    : promoted (≥ N user confirmations) — Phase 6 dashboard
--               surfaces it as a known routine
--   dismissed : user said no — hidden from suggestion list, but the
--               row is kept so we can detect "user dismissed this
--               same routine in the past" and not re-suggest it
-- ────────────────────────────────────────────────────────────────
ALTER TABLE routines
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'suggested',
  ADD COLUMN IF NOT EXISTS confirmed_count int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS promoted_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS dismissed_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE routines DROP CONSTRAINT IF EXISTS routines_status_check;
ALTER TABLE routines ADD CONSTRAINT routines_status_check CHECK (
  status IN ('suggested', 'active', 'dismissed')
);

CREATE INDEX IF NOT EXISTS idx_routines_status ON routines(status);
CREATE INDEX IF NOT EXISTS idx_routines_promoted_at
  ON routines(promoted_at) WHERE status = 'active';

-- ────────────────────────────────────────────────────────────────
-- Audit log of every user action on a routine candidate. The
-- promotion logic counts confirms here, so the lifecycle is fully
-- reconstructible even if someone tampers with routines.confirmed_count.
-- ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS routine_confirmations (
  id serial PRIMARY KEY,
  routine_id int NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
  action text NOT NULL,
  source text NULL,
  note text NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE routine_confirmations
  DROP CONSTRAINT IF EXISTS routine_confirmations_action_check;
ALTER TABLE routine_confirmations
  ADD CONSTRAINT routine_confirmations_action_check CHECK (
    action IN ('confirm', 'dismiss', 'override')
  );

CREATE INDEX IF NOT EXISTS idx_routine_confirmations_routine
  ON routine_confirmations(routine_id, created_at DESC);
