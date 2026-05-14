"""Cross-source correlation engine.

The base orchestrator collects from many independent streams — HA state
changes (presence, appliances, TV), HealthKit (steps, sleep, HR, workouts),
observer summaries (washer cycles, vacuum runs, sleep windows). Each is
useful in isolation, but the *interesting* observations come from joining
them: "your sleep was worse on nights you watched TV past 22:30",
"resting HR has crept up 9 bpm over 2 weeks", "you usually start the
laundry within 30 min of getting home from work".

This module runs a handful of SQL-driven queries against the live
database and turns them into short, human-readable insights for the
morning brief. Each correlator is independent and skipped silently if it
has nothing to say (no false positives like "your HR is normal!").

Designed to be fast (a few seconds total) so it can run as part of the
nightly reflection without blowing the budget.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import mean, stdev
from typing import Any

import asyncpg
from home_agents_sdk.telemetry import get_logger

logger = get_logger("orchestrator.correlations")


async def correlate_recent(pool: asyncpg.Pool, *, lookback_days: int = 14) -> list[dict[str, Any]]:
    """Run all correlators and return a flat list of insights.

    Each insight is ``{"id": str, "headline": str, "detail": str | None,
    "confidence": float}``. The brief renders them under "I noticed:".
    """
    insights: list[dict[str, Any]] = []
    for fn in (
        _resting_hr_drift,
        _sleep_vs_late_tv,
        _coffee_then_hr_spike,
        _step_count_trend,
        _missing_morning_routine,
    ):
        try:
            result = await fn(pool, lookback_days=lookback_days)
            if result:
                insights.append(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "correlation_failed",
                correlator=fn.__name__,
                error=f"{type(exc).__name__}: {exc}",
            )
    return insights


async def _resting_hr_drift(pool: asyncpg.Pool, *, lookback_days: int) -> dict[str, Any] | None:
    """Detect a sustained shift in resting heart rate vs the user's baseline.

    Compares the mean resting HR over the last 7 days to the prior 14 days.
    A drift of >5 bpm with reasonable sample sizes is worth surfacing —
    can indicate illness, overtraining, dehydration, or improved fitness.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT value, started_at FROM health_metrics
            WHERE metric = 'resting_heart_rate'
              AND started_at > now() - ($1::int * interval '1 day')
            ORDER BY started_at
            """,
            lookback_days + 7,
        )
    if len(rows) < 5:
        return None
    cutoff = datetime.now(UTC) - timedelta(days=7)
    recent = [r["value"] for r in rows if r["started_at"] >= cutoff]
    older = [r["value"] for r in rows if r["started_at"] < cutoff]
    if len(recent) < 3 or len(older) < 3:
        return None
    diff = mean(recent) - mean(older)
    if abs(diff) < 5.0:
        return None
    direction = "up" if diff > 0 else "down"
    arrow = "↑" if diff > 0 else "↓"
    return {
        "id": "resting_hr_drift",
        "headline": (
            f"Resting heart rate trended {direction} {abs(diff):.0f} bpm "
            f"this week ({mean(recent):.0f} {arrow} from baseline {mean(older):.0f})."
        ),
        "detail": (
            "Sustained changes in resting HR can reflect illness, training "
            "load, sleep debt, or hydration. Worth keeping an eye on if it "
            "persists another week."
        ),
        "confidence": 0.80 if abs(diff) < 8 else 0.92,
    }


async def _sleep_vs_late_tv(pool: asyncpg.Pool, *, lookback_days: int) -> dict[str, Any] | None:
    """If the user often watches TV past 22:30, do they sleep less on those nights?"""
    async with pool.acquire() as conn:
        sleep_rows = await conn.fetch(
            """
            SELECT started_at, value FROM health_metrics
            WHERE metric = 'sleep_asleep'
              AND started_at > now() - ($1::int * interval '1 day')
            ORDER BY started_at
            """,
            lookback_days,
        )
        tv_rows = await conn.fetch(
            """
            SELECT date_trunc('day', ts) AS night,
                   max(ts::time) AS latest_event
            FROM event_log
            WHERE capability = 'entertainment.left_on'
              AND ts > now() - ($1::int * interval '1 day')
            GROUP BY 1
            """,
            lookback_days,
        )
    if len(sleep_rows) < 4:
        return None
    by_date = {r["started_at"].date(): float(r["value"] or 0) for r in sleep_rows}
    late_dates = {
        r["night"].date()
        for r in tv_rows
        if r["latest_event"] and r["latest_event"].hour >= 22
    }
    late_sleep = [v for d, v in by_date.items() if d in late_dates and v > 0]
    other_sleep = [v for d, v in by_date.items() if d not in late_dates and v > 0]
    if len(late_sleep) < 2 or len(other_sleep) < 2:
        return None
    diff_min = mean(other_sleep) - mean(late_sleep)
    if diff_min < 20:
        return None
    return {
        "id": "sleep_vs_late_tv",
        "headline": (
            f"On nights you watched TV past 22:30 you slept "
            f"{diff_min:.0f} min less on average "
            f"({mean(late_sleep)/60:.1f} h vs {mean(other_sleep)/60:.1f} h)."
        ),
        "detail": (
            "Could be coincidence — late TV often correlates with late "
            "bedtime regardless of the screen itself. Worth a one-week "
            "experiment if you're trying to lengthen sleep."
        ),
        "confidence": 0.70,
    }


async def _coffee_then_hr_spike(pool: asyncpg.Pool, *, lookback_days: int) -> dict[str, Any] | None:
    """When you brew coffee, does HR spike within 30 minutes?

    Confirms the system's basic 'I see your routines and their effects'
    competence. Skips silently if there's no coffee data — most users
    won't have this hooked up.
    """
    async with pool.acquire() as conn:
        coffee = await conn.fetch(
            """
            SELECT ts FROM event_log
            WHERE capability = 'coffee.brewed'
              AND ts > now() - ($1::int * interval '1 day')
            ORDER BY ts
            """,
            lookback_days,
        )
        if not coffee:
            return None
        hr = await conn.fetch(
            """
            SELECT started_at, value FROM health_metrics
            WHERE metric = 'heart_rate'
              AND started_at > now() - ($1::int * interval '1 day')
            ORDER BY started_at
            """,
            lookback_days,
        )
    if not hr:
        return None
    by_ts = [(r["started_at"], float(r["value"] or 0)) for r in hr]
    spikes: list[float] = []
    for c in coffee:
        ts = c["ts"]
        before = [v for t, v in by_ts if ts - timedelta(minutes=20) <= t < ts and v > 0]
        after = [v for t, v in by_ts if ts < t <= ts + timedelta(minutes=30) and v > 0]
        if len(before) >= 2 and len(after) >= 2:
            spikes.append(mean(after) - mean(before))
    if len(spikes) < 3:
        return None
    avg_spike = mean(spikes)
    if abs(avg_spike) < 5:
        return None
    return {
        "id": "coffee_hr_spike",
        "headline": (
            f"After coffee.brewed your HR averages "
            f"{'+' if avg_spike > 0 else ''}{avg_spike:.0f} bpm in the next 30 min "
            f"(across {len(spikes)} mornings)."
        ),
        "detail": None,
        "confidence": 0.85,
    }


async def _step_count_trend(pool: asyncpg.Pool, *, lookback_days: int) -> dict[str, Any] | None:
    """Compare the last 7-day step total vs the prior 7 days."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('day', started_at) AS day, sum(value) AS total
            FROM health_metrics
            WHERE metric = 'steps' AND started_at > now() - ($1::int * interval '1 day')
            GROUP BY 1 ORDER BY 1
            """,
            lookback_days,
        )
    if len(rows) < 6:
        return None
    cutoff = datetime.now(UTC).date() - timedelta(days=7)
    recent = [float(r["total"] or 0) for r in rows if r["day"].date() > cutoff]
    older = [float(r["total"] or 0) for r in rows if r["day"].date() <= cutoff]
    if len(recent) < 3 or len(older) < 3:
        return None
    avg_recent = mean(recent)
    avg_older = mean(older)
    diff_pct = (avg_recent - avg_older) / max(avg_older, 1) * 100
    if abs(diff_pct) < 20:
        return None
    arrow = "↑" if diff_pct > 0 else "↓"
    return {
        "id": "step_trend",
        "headline": (
            f"Daily steps {arrow} {abs(diff_pct):.0f}% this week "
            f"({avg_recent:.0f} vs {avg_older:.0f} prior 7 days)."
        ),
        "detail": None,
        "confidence": 0.75 if len(recent) >= 5 else 0.6,
    }


async def _missing_morning_routine(
    pool: asyncpg.Pool, *, lookback_days: int
) -> dict[str, Any] | None:
    """If sleep wake time is consistent but today's wake event is way later,
    flag it — could be a forgotten alarm or a sick day.

    Uses the standard deviation of the last week's wake times as the
    user's personal "what's normal" range.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ended_at FROM health_metrics
            WHERE metric = 'sleep_asleep'
              AND ended_at IS NOT NULL
              AND ended_at > now() - ($1::int * interval '1 day')
            ORDER BY ended_at DESC
            """,
            min(lookback_days, 14),
        )
    if len(rows) < 5:
        return None
    today = rows[0]["ended_at"]
    others = [r["ended_at"] for r in rows[1:]]
    # Convert each to "minutes after midnight (local)" — works as long as the
    # user has a stable timezone, which we assume here. Cross-DST edge cases
    # would need timezone-aware math.
    def _min_of_day(ts: datetime) -> int:
        return ts.hour * 60 + ts.minute
    today_min = _min_of_day(today)
    other_mins = [_min_of_day(t) for t in others]
    if len(other_mins) < 4:
        return None
    avg = mean(other_mins)
    sd = stdev(other_mins) if len(other_mins) > 1 else 0
    if sd < 5:
        sd = 30  # avoid divide-by-tiny on very-consistent users
    delta = today_min - avg
    if abs(delta) < max(45, 1.5 * sd):
        return None
    direction = "later" if delta > 0 else "earlier"
    return {
        "id": "wake_time_drift",
        "headline": (
            f"You woke up {abs(delta):.0f} min {direction} than usual today "
            f"(typical wake ~{int(avg)//60:02d}:{int(avg)%60:02d})."
        ),
        "detail": None,
        "confidence": 0.80,
    }
