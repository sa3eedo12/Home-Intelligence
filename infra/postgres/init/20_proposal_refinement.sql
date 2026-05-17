-- Nightly proposal refinement: the 35B reasoner sits idle for hours
-- of the night window. We use that time to take draft proposals
-- (filed by the day-time router/escalator with limited context) and
-- refine them with deep reasoning + the full HA entity catalog.
--
-- refined_at — set when the 35B has reprocessed the proposal.
--   NULL = original (draft). NOT NULL = refined, do not touch again.
-- original_rationale — the rationale at file-time, preserved so we
--   can recover if a refinement is bad. Reviewers see both via
--   the dashboard.
-- refinement_notes — short LLM-generated summary of what was changed
--   ("dropped iPhone/RPi noise; identified BYD HAN as the EV").

ALTER TABLE proposals
    ADD COLUMN IF NOT EXISTS refined_at TIMESTAMPTZ NULL;

ALTER TABLE proposals
    ADD COLUMN IF NOT EXISTS original_rationale TEXT NULL;

ALTER TABLE proposals
    ADD COLUMN IF NOT EXISTS refinement_notes TEXT NULL;

-- Used by the nightly reflector's refine phase to find candidates
-- without scanning the whole proposals table.
CREATE INDEX IF NOT EXISTS idx_proposals_unrefined_pending
    ON proposals (created_at DESC)
    WHERE status = 'pending' AND kind = 'code_change' AND refined_at IS NULL;
