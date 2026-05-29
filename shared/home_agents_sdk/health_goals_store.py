"""HealthGoalsStore — per-member, free-form goals with structured tracking.

Goals are described in plain English by the user. At creation, an LLM
decides which underlying metrics to monitor (`metric_links` jsonb) and
which days workouts are expected (`workout_budget` jsonb). After that
the daily compute job walks each active goal, pulls the latest values
from the relevant tables (`health_metrics`, `sleep_summaries`,
`workout` rows), and writes a single progress row per (goal, day).

This module is the data layer. Nag scheduling, plan generation, and
Telegram intent routing all live in the orchestrator and just call the
methods here. Nothing in this file talks to an LLM directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg


# A goal can be in any of these states. 'paused' is distinct from
# 'abandoned' because pausing keeps the row counting toward "active
# goals" on the dashboard with a banner, while abandoned drops to the
# archive list.
VALID_STATUSES = {"active", "achieved", "paused", "abandoned"}
VALID_LABELS = {"on_track", "slipping", "regressing", "achieved", "paused"}
VALID_EVENT_KINDS = {
    "created", "paused", "resumed", "excused_today", "weekly_review",
    "achieved", "abandoned", "plan_refreshed", "nag_sent", "window_changed",
}

_DOW_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(slots=True)
class GoalSnapshot:
    """Computed view of a single goal at a point in time."""
    goal_id: int
    member_id: int
    title: str
    description: str
    status: str
    start_date: date
    target_date: date | None
    days_remaining: int | None
    quiet_until: datetime | None
    metric_links: list[dict[str, Any]]
    workout_budget: dict[str, Any] | None
    plan_text: str | None
    latest_progress: dict[str, Any] | None
    milestones: list[dict[str, Any]] = field(default_factory=list)


class HealthGoalsStore:
    """CRUD + status helpers for health_goals and its child tables."""

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    # ── Goal CRUD ────────────────────────────────────────────────

    async def create(
        self,
        *,
        member_id: int,
        title: str,
        description: str,
        metric_links: list[dict[str, Any]] | None = None,
        workout_budget: dict[str, Any] | None = None,
        plan_text: str | None = None,
        target_date: date | None = None,
        start_date: date | None = None,
    ) -> int | None:
        """Insert a new active goal. Returns the new id, or None when
        the pool is unavailable."""
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO health_goals(
                    member_id, title, description, metric_links,
                    workout_budget, plan_text, plan_generated_at,
                    start_date, target_date
                )
                VALUES (
                    $1, $2, $3, $4::jsonb, $5::jsonb, $6,
                    CASE WHEN $6 IS NOT NULL THEN now() ELSE NULL END,
                    COALESCE($7, CURRENT_DATE), $8
                )
                RETURNING id
                """,
                int(member_id), title, description,
                json.dumps(metric_links or [], default=str),
                json.dumps(workout_budget, default=str) if workout_budget else None,
                plan_text, start_date, target_date,
            )
            goal_id = int(row["id"]) if row else None
            if goal_id is not None:
                await conn.execute(
                    """
                    INSERT INTO health_goal_events(goal_id, member_id, kind, note)
                    VALUES ($1, $2, 'created', $3)
                    """,
                    goal_id, int(member_id), title,
                )
        return goal_id

    async def get(self, goal_id: int) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, member_id, title, description, metric_links,
                       workout_budget, plan_text, plan_generated_at,
                       start_date, target_date, status, quiet_until,
                       created_at, updated_at
                FROM health_goals WHERE id = $1
                """,
                int(goal_id),
            )
        return _decode_goal_row(row)

    async def list_active(
        self, *, member_id: int | None = None
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        clauses, params = ["status = 'active'"], []
        if member_id is not None:
            params.append(int(member_id))
            clauses.append(f"member_id = ${len(params)}")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, member_id, title, description, metric_links,
                       workout_budget, plan_text, plan_generated_at,
                       start_date, target_date, status, quiet_until,
                       created_at, updated_at
                FROM health_goals
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                """,
                *params,
            )
        return [_decode_goal_row(r) for r in rows]

    async def list_all_for_member(
        self, member_id: int, *, include_archived: bool = True
    ) -> list[dict[str, Any]]:
        """For the dashboard: active + paused + recently achieved or
        abandoned. include_archived=False to hide the latter two."""
        if not self._ready or self.pool is None:
            return []
        clauses = ["member_id = $1"]
        params: list[Any] = [int(member_id)]
        if not include_archived:
            clauses.append("status IN ('active', 'paused')")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, member_id, title, description, metric_links,
                       workout_budget, plan_text, plan_generated_at,
                       start_date, target_date, status, quiet_until,
                       created_at, updated_at
                FROM health_goals
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    CASE status WHEN 'active' THEN 1 WHEN 'paused' THEN 2
                                WHEN 'achieved' THEN 3 ELSE 4 END,
                    updated_at DESC
                """,
                *params,
            )
        return [_decode_goal_row(r) for r in rows]

    async def update_plan(
        self,
        goal_id: int,
        *,
        plan_text: str,
        metric_links: list[dict[str, Any]] | None = None,
        workout_budget: dict[str, Any] | None = None,
        milestones: list[dict[str, Any]] | None = None,
    ) -> None:
        """Refresh the plan narrative + optionally the structured fields.
        Called by the LLM plan-generation tool at creation and by the
        weekly reflection job when slippage warrants a re-plan."""
        if not self._ready or self.pool is None:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                sets = ["plan_text = $2", "plan_generated_at = now()",
                        "updated_at = now()"]
                params: list[Any] = [int(goal_id), plan_text]
                if metric_links is not None:
                    params.append(json.dumps(metric_links, default=str))
                    sets.append(f"metric_links = ${len(params)}::jsonb")
                if workout_budget is not None:
                    params.append(json.dumps(workout_budget, default=str))
                    sets.append(f"workout_budget = ${len(params)}::jsonb")
                await conn.execute(
                    f"UPDATE health_goals SET {', '.join(sets)} WHERE id = $1",
                    *params,
                )
                if milestones is not None:
                    # Wipe and re-insert. Milestones are a generated
                    # artifact; trying to merge versions is fiddly and
                    # the user can re-edit on the dashboard anyway.
                    await conn.execute(
                        "DELETE FROM health_goal_milestones WHERE goal_id = $1",
                        int(goal_id),
                    )
                    for m in milestones:
                        await conn.execute(
                            """
                            INSERT INTO health_goal_milestones(
                                goal_id, due_date, target_description
                            ) VALUES ($1, $2, $3)
                            """,
                            int(goal_id),
                            _coerce_date(m.get("due_date")),
                            str(m.get("target_description") or m.get("description") or ""),
                        )
                await conn.execute(
                    """
                    INSERT INTO health_goal_events(goal_id, kind, note)
                    VALUES ($1, 'plan_refreshed', $2)
                    """,
                    int(goal_id), (plan_text[:300] if plan_text else None),
                )

    async def set_status(
        self,
        goal_id: int,
        new_status: str,
        *,
        note: str | None = None,
    ) -> None:
        if new_status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        if not self._ready or self.pool is None:
            return
        kind_map = {
            "active": "resumed",
            "paused": "paused",
            "achieved": "achieved",
            "abandoned": "abandoned",
        }
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE health_goals
                    SET status = $2, updated_at = now()
                    WHERE id = $1
                    """,
                    int(goal_id), new_status,
                )
                await conn.execute(
                    """
                    INSERT INTO health_goal_events(goal_id, kind, note)
                    VALUES ($1, $2, $3)
                    """,
                    int(goal_id), kind_map[new_status], note,
                )

    async def set_quiet_until(
        self, goal_id: int, *, until: datetime | None
    ) -> None:
        """Mute proactive notifications for this goal until `until` (or
        clear when None)."""
        if not self._ready or self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE health_goals SET quiet_until = $2, updated_at = now()
                WHERE id = $1
                """,
                int(goal_id), until,
            )

    # ── Milestones ───────────────────────────────────────────────

    async def list_milestones(self, goal_id: int) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, due_date, target_description, achieved_at, status
                FROM health_goal_milestones
                WHERE goal_id = $1 ORDER BY due_date
                """,
                int(goal_id),
            )
        return [dict(r) for r in rows]

    async def mark_milestone(
        self, milestone_id: int, status: str
    ) -> None:
        if status not in {"achieved", "missed", "skipped", "pending"}:
            raise ValueError("invalid milestone status")
        if not self._ready or self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE health_goal_milestones
                SET status = $2,
                    achieved_at = CASE WHEN $2 = 'achieved' THEN now() ELSE NULL END
                WHERE id = $1
                """,
                int(milestone_id), status,
            )

    # ── Progress per (goal, day) ────────────────────────────────

    async def upsert_progress(
        self,
        goal_id: int,
        *,
        day: date,
        metric_snapshots: dict[str, Any],
        on_track_score: int | None,
        on_track_label: str | None,
        workout_required: bool,
        workout_completed: bool,
        rest_day_excused: bool,
        note: str | None = None,
    ) -> None:
        if on_track_label is not None and on_track_label not in VALID_LABELS:
            raise ValueError(f"label must be one of {VALID_LABELS}")
        if not self._ready or self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO health_goal_progress(
                    goal_id, day, metric_snapshots, on_track_score,
                    on_track_label, workout_required, workout_completed,
                    rest_day_excused, note
                )
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (goal_id, day) DO UPDATE SET
                    metric_snapshots = EXCLUDED.metric_snapshots,
                    on_track_score = EXCLUDED.on_track_score,
                    on_track_label = EXCLUDED.on_track_label,
                    workout_required = EXCLUDED.workout_required,
                    workout_completed = EXCLUDED.workout_completed,
                    rest_day_excused = EXCLUDED.rest_day_excused,
                    note = COALESCE(EXCLUDED.note, health_goal_progress.note),
                    updated_at = now()
                """,
                int(goal_id), day,
                json.dumps(metric_snapshots, default=str),
                on_track_score, on_track_label,
                workout_required, workout_completed, rest_day_excused, note,
            )

    async def get_progress(
        self, goal_id: int, *, day: date | None = None
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        target_day = day or date.today()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT goal_id, day, metric_snapshots, on_track_score,
                       on_track_label, workout_required, workout_completed,
                       rest_day_excused, nags_sent_today, last_nag_at, note,
                       updated_at
                FROM health_goal_progress
                WHERE goal_id = $1 AND day = $2
                """,
                int(goal_id), target_day,
            )
        return _decode_progress_row(row)

    async def recent_progress(
        self, goal_id: int, *, days: int = 30
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT day, metric_snapshots, on_track_score, on_track_label,
                       workout_required, workout_completed, rest_day_excused,
                       nags_sent_today, note
                FROM health_goal_progress
                WHERE goal_id = $1 AND day >= CURRENT_DATE - ($2::int * INTERVAL '1 day')
                ORDER BY day
                """,
                int(goal_id), int(days),
            )
        return [_decode_progress_row(r) for r in rows]

    # ── Workout excuse / rest day ────────────────────────────────

    async def excuse_today(
        self, goal_id: int, *, day: date | None = None, note: str | None = None
    ) -> None:
        """User said 'skip workout today'. Mark today as rest-day-excused
        so the nag scheduler shuts up."""
        if not self._ready or self.pool is None:
            return
        target = day or date.today()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO health_goal_progress(
                        goal_id, day, rest_day_excused, workout_required, note
                    )
                    VALUES ($1, $2, true, true, $3)
                    ON CONFLICT (goal_id, day) DO UPDATE SET
                        rest_day_excused = true,
                        note = COALESCE(EXCLUDED.note, health_goal_progress.note),
                        updated_at = now()
                    """,
                    int(goal_id), target, note,
                )
                await conn.execute(
                    """
                    INSERT INTO health_goal_events(goal_id, kind, note)
                    VALUES ($1, 'excused_today', $2)
                    """,
                    int(goal_id), note,
                )

    async def excuses_this_week(self, goal_id: int) -> int:
        """How many rest_day_excused rows in the trailing 7 days. Used
        by the rest-day budget enforcement when a user asks to skip
        too often."""
        if not self._ready or self.pool is None:
            return 0
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(*)::int FROM health_goal_progress
                WHERE goal_id = $1
                  AND rest_day_excused
                  AND day >= CURRENT_DATE - INTERVAL '6 days'
                """,
                int(goal_id),
            )
        return int(value or 0)

    # ── Nag bookkeeping ──────────────────────────────────────────

    async def record_nag(self, goal_id: int, *, day: date | None = None) -> None:
        if not self._ready or self.pool is None:
            return
        target = day or date.today()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO health_goal_progress(
                        goal_id, day, nags_sent_today, last_nag_at
                    )
                    VALUES ($1, $2, 1, now())
                    ON CONFLICT (goal_id, day) DO UPDATE SET
                        nags_sent_today = health_goal_progress.nags_sent_today + 1,
                        last_nag_at = now(),
                        updated_at = now()
                    """,
                    int(goal_id), target,
                )
                await conn.execute(
                    """
                    INSERT INTO health_goal_events(goal_id, kind)
                    VALUES ($1, 'nag_sent')
                    """,
                    int(goal_id),
                )

    # ── Events / audit ───────────────────────────────────────────

    async def log_event(
        self,
        goal_id: int,
        kind: str,
        *,
        member_id: int | None = None,
        note: str | None = None,
    ) -> None:
        if kind not in VALID_EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        if not self._ready or self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO health_goal_events(goal_id, member_id, kind, note)
                VALUES ($1, $2, $3, $4)
                """,
                int(goal_id), member_id, kind, note,
            )

    async def recent_events(
        self, goal_id: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, kind, note FROM health_goal_events
                WHERE goal_id = $1 ORDER BY ts DESC LIMIT $2
                """,
                int(goal_id), int(limit),
            )
        return [dict(r) for r in rows]


# ── Helpers ──────────────────────────────────────────────────────


def workout_required_today(
    goal: dict[str, Any], *, today: date | None = None
) -> bool:
    """Returns True iff this goal's workout_budget says today is a
    workout day. Used both by the daily compute job and by the nag
    scheduler."""
    target = today or date.today()
    budget = goal.get("workout_budget") or {}
    if not budget:
        return False
    days_preferred = budget.get("days_preferred")
    if not days_preferred:
        # No specific days set — use a default of "any weekday counts
        # as a workout day until the weekly quota is hit". We surface
        # this via the daily compute job which checks weekly count.
        return True
    today_name = _DOW_NAMES[target.weekday()]
    return today_name in {str(d).lower()[:3] for d in days_preferred}


def _decode_goal_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for k in ("metric_links", "workout_budget"):
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = None if k == "workout_budget" else []
        elif v is None and k == "metric_links":
            out[k] = []
    return out


def _decode_progress_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    snap = out.get("metric_snapshots")
    if isinstance(snap, str):
        try:
            out["metric_snapshots"] = json.loads(snap)
        except json.JSONDecodeError:
            out["metric_snapshots"] = {}
    elif snap is None:
        out["metric_snapshots"] = {}
    return out


def _coerce_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    raise ValueError(f"cannot coerce {value!r} to date")
