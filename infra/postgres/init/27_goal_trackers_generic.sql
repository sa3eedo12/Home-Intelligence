-- Generic goal-tracker engine.
--
-- The original design hardcoded workout_required / workout_completed as
-- booleans, which forced new goal shapes (sessions-per-day,
-- reps-per-session, calorie deficits, weight gauges) to either become
-- new schema fields or get squeezed into the wrong abstraction. This
-- migration replaces that with a spec-driven model:
--
--   * health_goals.tracker_spec     — jsonb describing trackers,
--                                     completion rule, and nudge rule.
--                                     Filled in by the LLM at goal
--                                     creation. No fixed schema beyond
--                                     "is valid JSON object".
--
--   * health_goal_log               — one row per user-reported event.
--                                     The store of truth for what the
--                                     user actually did. Compute reads
--                                     from here; nag reads from here.
--
--   * health_goal_progress.tracker_state — jsonb snapshot of every
--                                     tracker's current value at the
--                                     end of the day. Read-side cache
--                                     so the dashboard doesn't re-walk
--                                     the log every render.
--
-- The existing workout_required / workout_completed booleans stay so
-- the dashboard and nag scheduler keep working during the migration;
-- they become derived flags computed from tracker_state.

ALTER TABLE health_goals
    ADD COLUMN IF NOT EXISTS tracker_spec jsonb;

ALTER TABLE health_goal_progress
    ADD COLUMN IF NOT EXISTS tracker_state jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS health_goal_log (
    id          bigserial PRIMARY KEY,
    goal_id     bigint NOT NULL REFERENCES health_goals(id) ON DELETE CASCADE,
    member_id   int REFERENCES household_members(id) ON DELETE SET NULL,
    ts          timestamptz NOT NULL DEFAULT now(),
    raw_text    text,                       -- what the user said, verbatim
    deltas      jsonb NOT NULL DEFAULT '{}'::jsonb,
                                           -- {"sessions_today": 1, "reps_today": 30}
    source      text NOT NULL DEFAULT 'telegram',
                                           -- 'telegram' | 'dashboard' | 'auto'
    note        text
);

CREATE INDEX IF NOT EXISTS health_goal_log_goal_ts_idx
    ON health_goal_log(goal_id, ts DESC);
CREATE INDEX IF NOT EXISTS health_goal_log_member_ts_idx
    ON health_goal_log(member_id, ts DESC);
