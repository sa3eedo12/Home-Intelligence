-- Observed nag-window engagement + cross-goal insight history.
--
-- notify_engagement: one row per outbound nag, with timestamps for
-- delivery and the user's first message that arrived after it.
-- The weekly observation job groups by (dow, hour) and proposes
-- window adjustments when engagement drops below a threshold for
-- enough samples.
--
-- cross_goal_insights: one row per generated weekly cross-goal
-- insight, so we can show the rolling history on the dashboard and
-- avoid repeating the same observation week-over-week.

CREATE TABLE IF NOT EXISTS notify_engagement (
    id              bigserial PRIMARY KEY,
    member_id       int REFERENCES household_members(id) ON DELETE CASCADE,
    sent_at         timestamptz NOT NULL DEFAULT now(),
    topic           text,
    agent           text,
    capability      text,
    -- The next inbound user message that arrived AFTER sent_at.
    -- Updated by the engagement tracker when a Telegram message lands.
    first_reply_at  timestamptz,
    -- Convenience: difference in seconds. NULL until first_reply_at fires.
    reply_seconds   int
);

CREATE INDEX IF NOT EXISTS notify_engagement_member_sent_idx
    ON notify_engagement(member_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS notify_engagement_pending_idx
    ON notify_engagement(member_id) WHERE first_reply_at IS NULL;


CREATE TABLE IF NOT EXISTS cross_goal_insights (
    id              bigserial PRIMARY KEY,
    member_id       int NOT NULL REFERENCES household_members(id) ON DELETE CASCADE,
    generated_at    timestamptz NOT NULL DEFAULT now(),
    -- The goals considered (jsonb array of goal_ids) so the reflector
    -- can avoid repeating the same combination too often.
    goal_ids        jsonb NOT NULL DEFAULT '[]'::jsonb,
    insight_text    text NOT NULL,
    -- Optional structured suggestion the LLM tagged on
    -- (e.g. "increase weekly target on Run by 1").
    suggestion      jsonb
);

CREATE INDEX IF NOT EXISTS cross_goal_insights_member_idx
    ON cross_goal_insights(member_id, generated_at DESC);
