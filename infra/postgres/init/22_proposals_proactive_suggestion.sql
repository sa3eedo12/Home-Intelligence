-- Add 'proactive_suggestion' to the proposals.kind allowlist.
--
-- The SDK's reflection_store already accepts proactive_suggestion (used by
-- the pre-bedtime nudge), but the DB-level CHECK constraint defined in
-- 10_household.sql doesn't list it. Result: every pre-bedtime nudge
-- silently failed to insert with "violates check constraint
-- proposals_kind_check", even though the Telegram message went out fine.
--
-- Idempotent rewrite of the constraint so the allowlist matches the SDK.
BEGIN;

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
        'household_inference',
        'proactive_suggestion'
    )
);

COMMIT;
