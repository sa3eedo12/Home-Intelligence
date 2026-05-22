"""Routine sequence miner — detect "event A is followed by event B within W".

Complements ``pattern_miner`` (which clusters individual events by
day-of-week + hour to find recurring habits). This module finds
*sequences*: when one event reliably triggers another within a short
window.

Example output: "washer.cycle_complete is followed by appliance.dryer.start
within 30 minutes 85% of the time (47/55)."

Algorithm (single pass over event_log, last N days):

1. Bucket every event by ``subject = f"{agent}.{capability}"``.
2. For every (event_a, event_b) where event_a happens first and
   ts_b - ts_a <= W:
   - count[(subject_a, subject_b)] += 1
   - support[subject] = count of all events with that subject
3. For each candidate pair (A, B):
   - confidence = count[(A,B)] / support[A]
   - lift = confidence / (support[B] / total_events)
   - filter on min_support_a, min_pair_count, min_confidence, min_lift
4. Persist surviving candidates to the ``routines`` table — each row
   represents one sequence "A → B", schema reuses steps/schedule jsonb.

The miner is read-only on event_log and idempotent on routines (UPSERT
on the natural key ``name = f"{A} -> {B}"``).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any

from home_agents_sdk.telemetry import get_logger

from .common import SingleFlightJob

logger = get_logger("orchestrator.data_science.routine_sequence_miner")


# Defaults are deliberately conservative so we don't flood `routines`
# with weak patterns. Tune in `run(...)` per call if needed.
DEFAULT_WINDOW_MINUTES = 30
DEFAULT_MIN_SUPPORT_A = 5         # subject A must occur at least this often
DEFAULT_MIN_PAIR_COUNT = 4        # A → B must happen at least this many times
DEFAULT_MIN_CONFIDENCE = 0.50     # P(B | A) >= 50%
DEFAULT_MIN_LIFT = 2.0            # at least 2× base rate
# If subject B's inter-arrival CV (std / mean) is below this threshold,
# treat it as a cron-driven job and exclude it from being a follow-up
# candidate. 0.20 admits real-world jitter while still rejecting fixed
# 30-minute intervals which would otherwise win every miner candidate.
DEFAULT_REGULAR_CADENCE_CV = 0.20
DEFAULT_MIN_SAMPLES_FOR_CV = 6
# Skip these subjects on both sides — they're cron-driven housekeeping
# or self-recursion noise. The cadence detector above catches most of
# this automatically, but the static list is a belt-and-braces backstop
# (and is checked first so we can short-circuit before any counting).
_SKIP_SUBJECTS = {
    # data_science / orchestrator-internal
    "data_science.pattern_mining",
    "data_science.routine_sequence_mining",
    "data_science.maintenance",
    "data_science.reembed",
    "data_science.weekly_report",
    "data_science.monthly_report",
    "data_science.lora_training",
    "reflector.run",
    "advisor.run",
    "orchestrator.anomaly_check",
    "orchestrator.missing_routine_check",
    "orchestrator.proactive_scan",
    "orchestrator.pre_bedtime_scan",
    # Cron-driven scheduled jobs that appear in event_log as
    # "<dispatch_agent>.<capability>"; mining A → these is meaningless
    # because they fire on a fixed clock, not in response to A.
    "system_health.anomaly_check",
    "storage_backup.summarize_storage",
    "storage_backup.validate_backup",
    "knowledge_notes.index_path",
    "household_ops.pantry_low_stock",
    "personal_assistant.morning_brief",
    "personal_assistant.evening_recap",
    "personal_assistant.infer_sleep_summary",
    "personal_assistant.late_bedtime_check",
    "dashboard_curator.summarize_activity",
    "dashboard_curator.summarize_alerts",
}


@dataclass(slots=True)
class _Event:
    event_id: int
    ts: datetime
    subject: str


@dataclass(slots=True)
class SequenceCandidate:
    subject_a: str
    subject_b: str
    pair_count: int
    support_a: int
    support_b: int
    confidence: float
    lift: float
    window_minutes: int
    sample_event_ids: list[int]

    @property
    def name(self) -> str:
        return f"{self.subject_a} -> {self.subject_b}"

    def to_steps(self) -> list[dict[str, Any]]:
        return [
            {"step": 1, "trigger": self.subject_a},
            {"step": 2, "follows_within_minutes": self.window_minutes,
             "action": self.subject_b},
        ]

    def to_attributes(self) -> dict[str, Any]:
        return {
            "pair_count": self.pair_count,
            "support_a": self.support_a,
            "support_b": self.support_b,
            "confidence": round(self.confidence, 4),
            "lift": round(self.lift, 4),
            "window_minutes": self.window_minutes,
            "sample_event_ids": self.sample_event_ids[:20],
        }


class RoutineSequenceMiner(SingleFlightJob):
    """Mines A→B-within-W sequences from event_log into the routines table."""

    def __init__(
        self,
        pool: Any,
        *,
        event_log_store: Any | None = None,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        min_support_a: int = DEFAULT_MIN_SUPPORT_A,
        min_pair_count: int = DEFAULT_MIN_PAIR_COUNT,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        min_lift: float = DEFAULT_MIN_LIFT,
    ) -> None:
        super().__init__(
            job_name="routine_sequence_mining",
            pool=pool,
            event_log_store=event_log_store,
        )
        self.window_minutes = int(window_minutes)
        self.min_support_a = int(min_support_a)
        self.min_pair_count = int(min_pair_count)
        self.min_confidence = float(min_confidence)
        self.min_lift = float(min_lift)

    async def run(self, window_days: int = 30) -> dict[str, Any]:
        self.window_days = max(1, int(window_days or 30))
        return await self._run_singleflight(self._run)

    async def _run(self) -> dict[str, Any]:
        if self.pool is None:
            return {
                "status": "skipped",
                "reason": "postgres_unavailable",
                "candidates": [],
                "stored": 0,
            }
        try:
            rows = await self._load_events(self.window_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("routine_sequence_miner_load_failed", error=str(exc))
            return {
                "status": "skipped",
                "reason": "postgres_unavailable",
                "candidates": [],
                "stored": 0,
            }

        events = [e for row in rows if (e := _event_from_row(row))]
        candidates = self._mine(events)

        stored = 0
        for c in candidates:
            try:
                if await self._upsert_routine(c):
                    stored += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "routine_sequence_miner_store_failed",
                    name=c.name,
                    error=str(exc),
                )
        return {
            "candidates": [
                {
                    "name": c.name,
                    "confidence": c.confidence,
                    "lift": c.lift,
                    "pair_count": c.pair_count,
                    "window_minutes": c.window_minutes,
                }
                for c in candidates
            ],
            "stored": stored,
            "events_seen": len(events),
        }

    async def _load_events(self, window_days: int) -> list[Any]:
        async with self.pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT id, ts, agent, capability
                    FROM event_log
                    WHERE ts >= now() - ($1::int * interval '1 day')
                      AND agent <> 'data_science'
                      AND agent <> 'dashboard_curator'
                      AND agent NOT LIKE '__orchestrator__%'
                      AND agent NOT LIKE 'observer.%'
                    ORDER BY ts ASC
                    """,
                    window_days,
                )
            )

    def _mine(self, events: list[_Event]) -> list[SequenceCandidate]:
        """Look-forward scan: for each A event, see which subjects fire
        at least once within the next ``window_minutes``. That way
        confidence = P(B occurs within W after A) is a proper
        probability in [0,1] and a single A followed by 5 B's doesn't
        inflate the count."""
        # Defensive sort — the SQL has ORDER BY ts ASC but if callers
        # feed events directly we must not assume that.
        events = sorted(events, key=lambda e: e.ts)

        # Detect subjects whose inter-arrival cadence is too regular to
        # be event-driven (i.e. they're cron-driven housekeeping). Any
        # such subject is barred from being a B (followup), because the
        # miner would otherwise pair it with everything that fires in
        # the same 30-minute window — exactly what we saw on the first
        # production run where `system_health.anomaly_check` won every
        # candidate slot.
        cron_like_subjects = _detect_cron_like_subjects(events)

        window = timedelta(minutes=self.window_minutes)
        support: Counter[str] = Counter()
        pair_counts: Counter[tuple[str, str]] = Counter()
        samples: dict[tuple[str, str], list[int]] = defaultdict(list)
        n = len(events)

        for i, a in enumerate(events):
            if a.subject in _SKIP_SUBJECTS:
                continue
            support[a.subject] += 1
            seen_b: set[str] = set()
            j = i + 1
            while j < n and (events[j].ts - a.ts) <= window:
                b = events[j]
                # Skip A→A self-sequences, housekeeping noise, and
                # detected-cron subjects on the B side.
                if (
                    b.subject != a.subject
                    and b.subject not in _SKIP_SUBJECTS
                    and b.subject not in cron_like_subjects
                    and b.subject not in seen_b
                ):
                    key = (a.subject, b.subject)
                    pair_counts[key] += 1
                    seen_b.add(b.subject)
                    if len(samples[key]) < 40:
                        samples[key].extend([a.event_id, b.event_id])
                j += 1

        total = sum(support.values()) or 1
        out: list[SequenceCandidate] = []
        for (a, b), count in pair_counts.items():
            sup_a = support[a]
            sup_b = support[b]
            if sup_a < self.min_support_a:
                continue
            if count < self.min_pair_count:
                continue
            conf = count / sup_a
            if conf < self.min_confidence:
                continue
            base = sup_b / total if total else 0.0
            lift = (conf / base) if base > 0 else float("inf")
            if lift < self.min_lift:
                continue
            out.append(
                SequenceCandidate(
                    subject_a=a,
                    subject_b=b,
                    pair_count=count,
                    support_a=sup_a,
                    support_b=sup_b,
                    confidence=conf,
                    lift=lift if lift != float("inf") else 999.0,
                    window_minutes=self.window_minutes,
                    sample_event_ids=samples[(a, b)],
                )
            )
        out.sort(key=lambda c: (-c.confidence, -c.pair_count, c.subject_a))
        return out

    async def _upsert_routine(self, c: SequenceCandidate) -> bool:
        """Insert or refresh the routines row. Stores the proposal as a
        'suggested' routine; Phase 5 promotion logic decides whether to
        auto-execute it.

        Skip routines the user has explicitly dismissed — re-suggesting
        them would be obnoxious. They live on as dismissed rows so we
        can detect repeat-suggestion attempts.
        """
        steps_with_attrs = {
            "steps": c.to_steps(),
            "attributes": c.to_attributes(),
        }
        async with self.pool.acquire() as conn:
            existing_status = await conn.fetchval(
                "SELECT status FROM routines WHERE name = $1",
                c.name,
            )
            if existing_status == "dismissed":
                return False
            status = await conn.execute(
                """
                INSERT INTO routines(name, steps, schedule, source, status)
                VALUES ($1, $2::jsonb, NULL, 'routine_sequence_miner', 'suggested')
                ON CONFLICT (name) DO UPDATE SET
                    steps = EXCLUDED.steps,
                    source = COALESCE(EXCLUDED.source, routines.source),
                    updated_at = now()
                """,
                c.name,
                json.dumps(steps_with_attrs, default=str),
            )
        s = str(status)
        return s.startswith("INSERT") or s.startswith("UPDATE")


def _event_from_row(row: Any) -> _Event | None:
    try:
        agent = str(row["agent"] or "").strip()
        cap = str(row["capability"] or "").strip()
        if not agent or not cap:
            return None
        ts = row["ts"]
        if not isinstance(ts, datetime):
            return None
        return _Event(
            event_id=int(row["id"]),
            ts=ts,
            subject=f"{agent}.{cap}",
        )
    except (KeyError, TypeError, ValueError):
        return None


def _detect_cron_like_subjects(
    events: list[_Event],
    *,
    cv_threshold: float = DEFAULT_REGULAR_CADENCE_CV,
    min_samples: int = DEFAULT_MIN_SAMPLES_FOR_CV,
) -> set[str]:
    """Find subjects whose inter-arrival times are too regular to be
    user/event-driven.

    Cron and interval jobs land in event_log with tightly-spaced
    inter-arrival times (60 min ± a few seconds). Real human-driven
    events (lights, washer, presence) have wide spread. We compute the
    coefficient of variation (CV = std/mean) per subject and flag
    anything below ``cv_threshold``.

    Requires at least ``min_samples`` events per subject — otherwise
    we have too few intervals to make a reliable judgement and the
    subject is left in play.
    """
    by_subject: dict[str, list[datetime]] = defaultdict(list)
    for e in events:
        by_subject[e.subject].append(e.ts)

    cron_like: set[str] = set()
    for subject, timestamps in by_subject.items():
        if len(timestamps) < min_samples:
            continue
        timestamps.sort()
        intervals = [
            (timestamps[i] - timestamps[i - 1]).total_seconds()
            for i in range(1, len(timestamps))
        ]
        avg = mean(intervals)
        if avg <= 0:
            continue
        cv = pstdev(intervals) / avg
        if cv < cv_threshold:
            cron_like.add(subject)
    return cron_like
