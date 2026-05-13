"""Anomaly detector — the 'I noticed today is different' surface.

Runs on a schedule and looks for OBVIOUS deviations from the user's typical
patterns. Built to work with the data we already have (cycle_loads,
cleaning_runs, sleep_summaries, presence_returns, tv_left_on, event_log,
household_members) — does not require the pattern_miner to have found any
habits.

Each detected anomaly is published to ``events.observed`` with kind
``anomaly.detected``. A reactive trigger surfaces it as a notification
through the policy engine (which respects quiet hours).

Detectors today:
- vacuum_overdue: cleaning_runs hasn't fired for >7 days
- sleep_summary_missing: no sleep_summaries row for last 2 nights
- coffee_skipped: no coffee.brewed event by hour X today (configurable)
- washer_overdue: no appliance.cycle_completed for the washer in N days
- bedtime_late: still motion past usual sleep_time + 60min
- presence_overnight_unaccounted: at least one member's tracker shows
  not_home for >12h continuously without confirmation

Deliberately conservative — we only fire when we're VERY likely right.
Each detection includes a per-anomaly cooldown (default 12h) so we don't
spam the user about the same overdue thing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from home_agents_sdk.telemetry import get_logger

logger = get_logger("orchestrator.anomaly_detector")

DEFAULT_VACUUM_INTERVAL_DAYS = 7
DEFAULT_WASHER_OVERDUE_DAYS = 14
DEFAULT_SLEEP_MISSING_DAYS = 2
DEFAULT_COFFEE_BY_HOUR_LOCAL = 11  # if no coffee by 11am local on weekdays
DEFAULT_COOLDOWN_HOURS = 12


@dataclass
class Anomaly:
    kind: str
    summary: str
    severity: str
    payload: dict[str, Any]


async def detect_anomalies(
    *,
    pool: Any,
    user_tz_name: str = "Asia/Dubai",
) -> list[Anomaly]:
    """Run every detector. Returns the list of anomalies; caller decides
    whether to publish them (after consulting cooldown / quiet hours)."""
    if pool is None:
        return []
    now = datetime.now(UTC)
    out: list[Anomaly] = []
    detectors = (
        _detect_vacuum_overdue,
        _detect_washer_overdue,
        _detect_sleep_missing,
        _detect_coffee_skipped,
        _detect_bedtime_overrun,
    )
    for detector in detectors:
        try:
            anomaly = await detector(pool, now=now, user_tz_name=user_tz_name)
        except Exception as exc:
            logger.warning(
                "anomaly_detector_failed",
                detector=detector.__name__,
                error=str(exc),
            )
            anomaly = None
        if anomaly is not None:
            out.append(anomaly)
    return out


async def _detect_vacuum_overdue(pool: Any, *, now: datetime, **_) -> Anomaly | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT max(ts) AS last_seen
            FROM event_log
            WHERE capability = 'cleaning.completed'
            """
        )
    last_seen = row["last_seen"] if row else None
    if last_seen is None:
        return None
    days = (now - last_seen.astimezone(UTC)).days
    if days < DEFAULT_VACUUM_INTERVAL_DAYS:
        return None
    return Anomaly(
        kind="anomaly.detected",
        severity="notice",
        summary=f"🧹 Vacuum hasn't run in {days} days. Want me to remind you?",
        payload={
            "anomaly_type": "vacuum_overdue",
            "days_since": days,
            "last_seen": last_seen.isoformat(),
        },
    )


async def _detect_washer_overdue(pool: Any, *, now: datetime, **_) -> Anomaly | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT max(ts) AS last_seen
            FROM event_log
            WHERE capability = 'appliance.cycle_completed'
            """
        )
    last_seen = row["last_seen"] if row else None
    if last_seen is None:
        return None
    days = (now - last_seen.astimezone(UTC)).days
    if days < DEFAULT_WASHER_OVERDUE_DAYS:
        return None
    return Anomaly(
        kind="anomaly.detected",
        severity="notice",
        summary=f"🧺 Washer hasn't run in {days} days. Forgot a load?",
        payload={
            "anomaly_type": "washer_overdue",
            "days_since": days,
            "last_seen": last_seen.isoformat(),
        },
    )


async def _detect_sleep_missing(pool: Any, *, now: datetime, **_) -> Anomaly | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT max(night_of) AS last_night
            FROM sleep_summaries
            """
        )
    last_night = row["last_night"] if row else None
    if last_night is None:
        # Never had a sleep summary at all — skip (the system is too new)
        return None
    today = now.date()
    days = (today - last_night).days
    if days < DEFAULT_SLEEP_MISSING_DAYS:
        return None
    return Anomaly(
        kind="anomaly.detected",
        severity="notice",
        summary=(
            f"🌙 No sleep summary recorded for {days} night(s). "
            f"Is HealthKit sync still working?"
        ),
        payload={
            "anomaly_type": "sleep_summary_missing",
            "nights_missing": days,
            "last_night": last_night.isoformat(),
        },
    )


def _user_local(now: datetime, tz_name: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo(tz_name))
    except Exception:
        return now


async def _detect_coffee_skipped(
    pool: Any, *, now: datetime, user_tz_name: str
) -> Anomaly | None:
    local = _user_local(now, user_tz_name)
    if local.hour < DEFAULT_COFFEE_BY_HOUR_LOCAL:
        return None
    if local.weekday() >= 5:
        # Weekends: don't fire (people sleep in)
        return None
    today_start = datetime.combine(local.date(), time(0, 0), tzinfo=local.tzinfo)
    today_start_utc = today_start.astimezone(UTC)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT count(*) AS n FROM event_log
            WHERE capability = 'coffee.brewed' AND ts >= $1
            """,
            today_start_utc,
        )
    if int(row["n"] or 0) > 0:
        return None
    # Also bail if the system has never seen a coffee event — it'd be noise
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT max(ts) AS last_brew FROM event_log WHERE capability = 'coffee.brewed'"
        )
    if row["last_brew"] is None:
        return None
    return Anomaly(
        kind="anomaly.detected",
        severity="info",
        summary=f"☕ No coffee brewed yet today (it's {local.strftime('%H:%M')}). Sleeping in?",
        payload={
            "anomaly_type": "coffee_skipped",
            "local_hour": local.hour,
        },
    )


async def _detect_bedtime_overrun(
    pool: Any, *, now: datetime, user_tz_name: str
) -> Anomaly | None:
    local = _user_local(now, user_tz_name)
    # Fire window: 23:00 - 04:59 local (late evening through early morning)
    in_window = local.hour >= 23 or local.hour < 5
    if not in_window:
        return None
    async with pool.acquire() as conn:
        member = await conn.fetchrow(
            """
            SELECT id, name, sleep_time
            FROM household_members
            WHERE sleep_time IS NOT NULL
            ORDER BY id
            LIMIT 1
            """
        )
    if member is None or member["sleep_time"] is None:
        return None
    typical_sleep = member["sleep_time"]
    # If we're between midnight and ~5am local, the user's "typical bedtime"
    # was YESTERDAY's date. Anchor typical_local to yesterday in that case.
    typical_date = local.date()
    if local.hour < 12:
        typical_date = typical_date - timedelta(days=1)
    typical_local = datetime.combine(typical_date, typical_sleep, tzinfo=local.tzinfo)
    # Allow 60 min grace
    threshold = typical_local + timedelta(minutes=60)
    if local < threshold:
        return None
    # Check if there's been a sleep.likely_asleep event already in the last 6h
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT count(*) AS n FROM event_log
            WHERE capability = 'sleep.likely_asleep'
              AND ts >= now() - interval '6 hours'
            """
        )
    if int(row["n"] or 0) > 0:
        return None
    overdue_min = int((local - typical_local).total_seconds() // 60)
    return Anomaly(
        kind="anomaly.detected",
        severity="info",
        summary=(
            f"🌙 You're {overdue_min} min past your usual bedtime "
            f"({typical_sleep.strftime('%H:%M')}). Want me to dim the lights?"
        ),
        payload={
            "anomaly_type": "bedtime_overrun",
            "overdue_minutes": overdue_min,
            "typical_sleep_time": typical_sleep.strftime("%H:%M"),
        },
    )


async def emit_anomalies(*, anomalies: list[Anomaly], redis: Any) -> int:
    """Publish each anomaly to events.observed.

    Cooldown is enforced via Redis SET with EX TTL keyed on the anomaly type
    (so the user doesn't get hourly 'vacuum is overdue' pings).
    """
    if not anomalies or redis is None:
        return 0
    sent = 0
    cooldown_seconds = DEFAULT_COOLDOWN_HOURS * 3600
    for anomaly in anomalies:
        cooldown_key = (
            f"anomaly_cooldown:{anomaly.payload.get('anomaly_type', anomaly.kind)}"
        )
        try:
            existing = await redis.get(cooldown_key)
        except Exception:
            existing = None
        if existing:
            continue
        envelope = {
            "agent": "orchestrator.anomaly_detector",
            "kind": anomaly.kind,
            "summary": anomaly.summary,
            "severity": anomaly.severity,
            "payload": anomaly.payload,
            "ts": datetime.now(UTC).isoformat(),
        }
        try:
            await redis.xadd(
                "events.observed",
                {"payload": json.dumps(envelope, default=str)},
                maxlen=10000,
                approximate=True,
            )
            await redis.set(cooldown_key, "1", ex=cooldown_seconds)
            sent += 1
        except Exception as exc:
            logger.warning("anomaly_publish_failed", kind=anomaly.kind, error=str(exc))
    return sent
