-- Closes the "34h 35min sleep" bug at the storage layer.
--
-- The original unique index on (metric, started_at, COALESCE(value, 0), source)
-- treated each HealthKit-Auto-Export snapshot as a distinct row because the
-- value (duration in minutes) changes every time HAE re-syncs partway through
-- a sleep session. Result: ONE real sleep session ended up as 2-3 rows in
-- health_metrics, all with the same started_at but progressively-larger
-- ended_at and value. Naive aggregations summed them.
--
-- Replace the index with one keyed on (metric, started_at, source) so the
-- INSERT ... ON CONFLICT DO UPDATE path can refresh the row in-place when
-- HAE re-syncs the same session.
--
-- Before the new index can be created we have to coalesce existing duplicates.
-- Strategy: for every (metric, started_at, source) tuple that has more than
-- one row, keep only the one with the latest received_at (= the freshest
-- snapshot) and delete the rest.
BEGIN;

-- Step 1: delete duplicate rows, keeping the row with the latest received_at
-- per (metric, started_at, source).
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY metric, started_at, source
               ORDER BY received_at DESC NULLS LAST, id DESC
           ) AS rn
    FROM health_metrics
)
DELETE FROM health_metrics
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- Step 2: drop the old per-value unique index and create the new session-keyed one.
DROP INDEX IF EXISTS health_metrics_dedupe_idx;

CREATE UNIQUE INDEX IF NOT EXISTS health_metrics_session_dedupe_idx
    ON health_metrics(metric, started_at, source);

COMMIT;
