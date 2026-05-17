-- Capability gap log: records every user request the router/escalator
-- couldn't fulfill, so the nightly reflector can mine the patterns and
-- propose new tools / refactors. Without this table the system silently
-- forgets every failure and learns nothing.
--
-- Architectural contract:
-- - Written by router.py (low-confidence classify, dispatch failure,
--   chat-fallback for action-verb) and escalator.py (ReAct loop gave up)
-- - Read by reflector.py during nightly reflection; clusters by domain
--   and produces code_change proposals via reflection_store
-- - mark_resolved when reflector files a proposal that addresses the gap
--   OR when a human dismisses the gap from the dashboard

CREATE TABLE IF NOT EXISTS capability_gaps (
    id BIGSERIAL PRIMARY KEY,

    -- What the user asked
    user_text TEXT NOT NULL,
    member_id INTEGER NULL,
    member_name TEXT NULL,

    -- What the router/escalator tried
    -- Shape: {"router": {"agent": str, "capability": str, "inputs": {...}},
    --         "escalator_steps": [{"iter": 1, "tool": "list_entities",
    --                              "args": {...}, "result_summary": "..."}],
    --         "tools_considered": ["climate_status", ...]}
    router_pick JSONB NULL,
    escalation_path JSONB NULL,

    -- Why it failed. Open-ended TEXT not CHECK constrained because new
    -- failure categories will appear as we instrument more code paths.
    failure_reason TEXT NOT NULL,

    -- LLM honest reply text actually returned to the user, for audit
    user_reply TEXT NULL,

    -- Resolution tracking
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    proposal_id INTEGER NULL REFERENCES proposals(id) ON DELETE SET NULL,
    resolved_at TIMESTAMPTZ NULL,
    resolution_note TEXT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The reflector's nightly mining query: WHERE NOT resolved ORDER BY recent
CREATE INDEX IF NOT EXISTS idx_capability_gaps_unresolved
    ON capability_gaps (resolved, created_at DESC)
    WHERE resolved = FALSE;

-- Dashboard timeline view: recent gaps across all states
CREATE INDEX IF NOT EXISTS idx_capability_gaps_created_at
    ON capability_gaps (created_at DESC);

-- Pattern clustering by failure reason
CREATE INDEX IF NOT EXISTS idx_capability_gaps_failure_reason
    ON capability_gaps (failure_reason, created_at DESC);
