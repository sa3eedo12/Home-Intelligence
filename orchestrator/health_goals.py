"""Daily compute + workout nag for active health goals.

Two entrypoints:

- `compute_today(...)`: walks every active goal, pulls today's snapshot
  for each linked metric, writes one progress row per goal. Run nightly
  at 23:30 so it has the full day of data.

- `run_workout_nags(...)`: every 30 minutes during the day. For each
  goal whose workout_budget says today is a workout day and the user
  hasn't logged a workout (or excused it), emit a playful nag via the
  notify.outbound Redis stream — but only if the member's nag-window
  preference says we're inside an allowed window AND we haven't already
  nagged 3 times today.

Both are pure orchestration over the HealthGoalsStore + the existing
health_metrics table; no LLM calls here. Nag wording is intentionally
canned and varied; the LLM-generated plan_text gives each goal its own
voice during the dashboard / weekly reflection pass.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
from home_agents_sdk.health_goals_store import (
    HealthGoalsStore,
    workout_required_today,
)
from home_agents_sdk.member_nag_windows_store import MemberNagWindowsStore
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis


logger = get_logger("orchestrator.health_goals")

# How many nags we send per goal per day. Three feels like the line
# between "playful prod" and "annoying app". After the third the
# scheduler stays quiet until the daily compute resets the counter.
MAX_NAGS_PER_DAY = 3

# Cap how close together two nags can land. 90 minutes feels human;
# without this, two ticks of the 30-min interval scheduler could each
# clear the window-just-opened gate and double-nag.
MIN_NAG_GAP_MINUTES = 90


_NAG_TEMPLATES_FIRST = [
    "Hey, {title} day. Want to knock out the workout before it gets late?",
    "Just a heads up — your plan has a workout penciled in for today. Up for it?",
    "Today's a workout day for {title}. No pressure, but the earlier you start the easier it lands.",
    "Friendly nudge: {title} is on the schedule today. Want to get moving?",
]
_NAG_TEMPLATES_SECOND = [
    "Still hoping to fit in that workout today? Even 20 minutes counts.",
    "Round two of the nag: workout for {title} is still open.",
    "I haven't seen a workout from you today. Want to do something short?",
    "Quick check — workout for {title}? You've got time.",
]
_NAG_TEMPLATES_THIRD = [
    "Last call from me today on the workout. Whatever you decide, I'll log it tomorrow.",
    "Final nudge: workout day for {title}. After this I'll back off until morning.",
    "If today's a no, just tell me and I'll mark it skipped — no shame.",
]


def _pick_nag_text(goal_title: str, nags_today: int) -> str:
    bucket = (
        _NAG_TEMPLATES_FIRST if nags_today == 0
        else _NAG_TEMPLATES_SECOND if nags_today == 1
        else _NAG_TEMPLATES_THIRD
    )
    return random.choice(bucket).format(title=goal_title)


# ── Daily compute ────────────────────────────────────────────────


async def compute_today(
    *,
    pool: asyncpg.Pool,
    store: HealthGoalsStore,
    today: date | None = None,
) -> dict[str, Any]:
    """Walk every active goal and write today's progress row.

    For workout-frequency goals the snapshot just counts workouts logged
    today + this week. For other metrics we pull the latest value from
    health_metrics. The progress label is a coarse on_track/slipping/
    regressing/achieved bucket; the dashboard renders this verbatim
    and the weekly reflection job uses it as context.
    """
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    active = await store.list_active()
    processed = 0
    nags_emitted = 0  # not used here, included so the schedule notify
                      # payload looks consistent with run_workout_nags
    for goal in active:
        try:
            snapshot, score, label, completed = await _snapshot_for_goal(
                pool=pool, goal=goal, today=today, week_start=week_start,
            )
        except Exception as exc:
            logger.warning(
                "health_goal_compute_failed", goal_id=goal["id"], error=str(exc)
            )
            continue
        required = workout_required_today(goal, today=today)
        await store.upsert_progress(
            int(goal["id"]),
            day=today,
            metric_snapshots=snapshot,
            on_track_score=score,
            on_track_label=label,
            workout_required=required,
            workout_completed=completed,
            rest_day_excused=False,  # honored by excuse_today path, not here
        )
        processed += 1
    return {
        "ok": True, "processed": processed, "day": today.isoformat(),
        "nags_emitted": nags_emitted,
    }


async def _snapshot_for_goal(
    *,
    pool: asyncpg.Pool,
    goal: dict[str, Any],
    today: date,
    week_start: date,
) -> tuple[dict[str, Any], int | None, str | None, bool]:
    """Return (metric_snapshot, on_track_score, on_track_label, workout_completed)."""
    snapshot: dict[str, Any] = {}
    member_id = int(goal["member_id"])
    metric_links = goal.get("metric_links") or []
    workout_completed = False
    workout_count_week = 0
    weight_actual: float | None = None
    weight_target: float | None = None

    async with pool.acquire() as conn:
        for link in metric_links:
            if not isinstance(link, dict):
                continue
            metric = str(link.get("metric") or "").strip()
            if not metric:
                continue
            if metric == "workout":
                row = await conn.fetchrow(
                    """
                    SELECT
                        count(*) FILTER (
                            WHERE ts::date = $2 AND member_id = $1
                        )::int AS today_count,
                        count(*) FILTER (
                            WHERE ts::date >= $3 AND member_id = $1
                        )::int AS week_count
                    FROM health_metrics
                    WHERE metric = 'workout'
                      AND ts >= ($3::date - INTERVAL '1 day')
                    """,
                    member_id, today, week_start,
                )
                today_count = int((row or {}).get("today_count") or 0)
                week_count = int((row or {}).get("week_count") or 0)
                workout_completed = workout_completed or (today_count > 0)
                workout_count_week = max(workout_count_week, week_count)
                snapshot["workouts_today"] = today_count
                snapshot["workouts_this_week"] = week_count
                snapshot["workouts_target_per_week"] = link.get("target_per_week")
            else:
                # Generic latest-value pull. Works for weight, hrv, rhr,
                # steps, etc. Sleep gets its own path below.
                latest = await conn.fetchval(
                    """
                    SELECT value FROM health_metrics
                    WHERE metric = $1 AND member_id = $2
                    ORDER BY ts DESC LIMIT 1
                    """,
                    metric, member_id,
                )
                if latest is not None:
                    snapshot[metric] = float(latest)
                    if metric == "weight":
                        weight_actual = float(latest)
                        try:
                            weight_target = float(link.get("target") or 0) or None
                        except (TypeError, ValueError):
                            weight_target = None

    # Score: simple weighted blend so the dashboard has a number even
    # when only one metric matters.
    score = _score_from_snapshot(
        workout_count_week=workout_count_week,
        workout_target=_workout_target(goal),
        weight_actual=weight_actual,
        weight_target=weight_target,
    )
    label = _label_from_score(score)
    return snapshot, score, label, workout_completed


def _workout_target(goal: dict[str, Any]) -> int | None:
    budget = goal.get("workout_budget") or {}
    if not isinstance(budget, dict):
        return None
    raw = budget.get("required_per_week")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _score_from_snapshot(
    *,
    workout_count_week: int,
    workout_target: int | None,
    weight_actual: float | None,
    weight_target: float | None,
) -> int | None:
    """Roll the available metrics into a 0–100 number. Missing signal
    just contributes nothing rather than penalizing the user."""
    parts: list[float] = []
    if workout_target and workout_target > 0:
        ratio = min(workout_count_week / workout_target, 1.0)
        parts.append(100.0 * ratio)
    if weight_actual is not None and weight_target is not None:
        # Distance from target as a fraction of the starting gap;
        # we don't know the start, so just give credit for being
        # within 2 kg of target.
        diff = abs(weight_actual - weight_target)
        weight_score = max(0.0, min(100.0, 100.0 - 50.0 * diff))
        parts.append(weight_score)
    if not parts:
        return None
    return int(round(sum(parts) / len(parts)))


def _label_from_score(score: int | None) -> str | None:
    if score is None:
        return None
    if score >= 80:
        return "on_track"
    if score >= 50:
        return "slipping"
    return "regressing"


# ── Workout nags ─────────────────────────────────────────────────


async def run_workout_nags(
    *,
    pool: asyncpg.Pool,
    redis: Redis,
    store: HealthGoalsStore,
    nag_store: MemberNagWindowsStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    """For each active workout-frequency goal: if today is a workout
    day, the workout isn't done, the user hasn't excused today, the
    nag window allows it, and the cap hasn't been hit — fire a nag."""
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    active = await store.list_active()
    emitted = 0
    considered = 0
    skipped: dict[str, int] = {
        "outside_window": 0, "muted": 0, "not_required": 0,
        "already_done": 0, "excused": 0, "cap": 0, "too_soon": 0,
    }
    for goal in active:
        if not workout_required_today(goal, today=today):
            skipped["not_required"] += 1
            continue
        # Goal-level mute beats everything
        if _is_muted(goal, now):
            skipped["muted"] += 1
            continue
        considered += 1
        member_id = int(goal["member_id"])
        if not await nag_store.is_nag_allowed_now(member_id, now=now):
            skipped["outside_window"] += 1
            continue
        progress = await store.get_progress(int(goal["id"]), day=today) or {}
        if progress.get("workout_completed"):
            skipped["already_done"] += 1
            continue
        if progress.get("rest_day_excused"):
            skipped["excused"] += 1
            continue
        nags_today = int(progress.get("nags_sent_today") or 0)
        if nags_today >= MAX_NAGS_PER_DAY:
            skipped["cap"] += 1
            continue
        last_nag_at = progress.get("last_nag_at")
        if isinstance(last_nag_at, datetime):
            gap = (now - last_nag_at.astimezone(UTC)).total_seconds() / 60
            if gap < MIN_NAG_GAP_MINUTES:
                skipped["too_soon"] += 1
                continue
        text = _pick_nag_text(goal["title"], nags_today)
        chat_id = await _chat_id_for_member(pool, member_id)
        if chat_id is None:
            logger.warning("nag_skipped_no_chat", member_id=member_id)
            continue
        payload = {
            "chat_id": chat_id,
            "text": text,
            "severity": "info",
            "topic": f"goal:{goal['id']}",
            "agent": "health_goals",
            "capability": "workout_nag",
        }
        await redis.xadd("notify.outbound", {"payload": json.dumps(payload)})
        await store.record_nag(int(goal["id"]), day=today)
        emitted += 1
    return {
        "ok": True, "considered": considered, "emitted": emitted,
        "skipped": skipped,
    }


def _is_muted(goal: dict[str, Any], now: datetime) -> bool:
    quiet = goal.get("quiet_until")
    if quiet is None:
        return False
    if isinstance(quiet, datetime):
        return quiet > now.astimezone(quiet.tzinfo or UTC)
    return False


async def _chat_id_for_member(pool: asyncpg.Pool, member_id: int) -> int | None:
    async with pool.acquire() as conn:
        chat_id = await conn.fetchval(
            "SELECT telegram_chat_id FROM household_members WHERE id = $1",
            member_id,
        )
    if chat_id is None:
        return None
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None
