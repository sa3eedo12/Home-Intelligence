"""Missing-routine detector — 'I expected X by now, but it hasn't happened.'

Reads the ``habits`` table (populated by data_science.pattern_miner) and
the ``routines`` table (populated by routine_sequence_miner) and emits
anomalies for two cases:

1. **Day/time habit didn't fire.** A habit like
   "home_automation.coffee_started fires Mon/Tue/Wed/Thu/Fri between
   07:00-08:30". If today is one of those days, we're past 08:30 local,
   and no matching subject fired today → emit ``missing_habit``.

2. **Sequence routine A→B didn't fire after A.** A routine like
   "washer.cycle_complete -> dryer.start within 30min" with high
   confidence. If A fired more than W minutes ago and B didn't follow
   → emit ``missing_followup``.

Only checks habits/routines with reasonable confidence (default 0.60)
and recent activity (default within last 21 days), so brand-new noisy
candidates don't spam the user.

Both kinds use the existing ``Anomaly`` envelope from anomaly_detector
so they slot into the same emit pipeline + cooldown logic.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from home_agents_sdk.telemetry import get_logger

from .anomaly_detector import Anomaly

logger = get_logger("orchestrator.missing_routine_detector")

DEFAULT_CONFIDENCE_FLOOR = 0.60
DEFAULT_RECENCY_DAYS = 21
DEFAULT_SEQUENCE_LOOKBACK_HOURS = 6

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _decode_jsonish(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _parse_time_window(text: str | None) -> tuple[time, time] | None:
    """Parse ``HH:MM-HH:MM`` (possibly wrapping past midnight)."""
    if not text or "-" not in text:
        return None
    try:
        a, b = text.split("-", 1)
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
        return time(ah % 24, am % 60), time(bh % 24, bm % 60)
    except (ValueError, IndexError):
        return None


def _expected_today(
    pattern: dict[str, Any],
    *,
    today_local: date,
    now_local: datetime,
) -> tuple[bool, time | None]:
    """Returns (today_is_expected, end_of_window_local).

    The detector only fires if today_is_expected is True AND we're past
    the window's end_time (so we know the habit had its chance and
    didn't fire)."""
    days = pattern.get("days_of_week") or []
    if isinstance(days, list) and days:
        weekday_name = _DAY_NAMES[today_local.weekday()]
        if weekday_name not in {str(d).lower() for d in days}:
            return False, None
    window_text = pattern.get("time_window_local")
    window = _parse_time_window(window_text) if isinstance(window_text, str) else None
    if window is None:
        return False, None
    _, end_t = window
    return True, end_t


async def detect_missing_habits(
    pool: Any,
    *,
    user_tz_name: str = "Asia/Dubai",
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    recency_days: int = DEFAULT_RECENCY_DAYS,
    grace_minutes: int = 30,
) -> list[Anomaly]:
    """Day/time habits expected today but not observed yet."""
    if pool is None:
        return []
    tz = ZoneInfo(user_tz_name)
    now_local = datetime.now(tz)
    today_local = now_local.date()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, subject, pattern, confidence, last_observed_at
            FROM habits
            WHERE confidence >= $1
              AND (last_observed_at IS NULL
                   OR last_observed_at >= now() - ($2::int * interval '1 day'))
            """,
            float(confidence_floor),
            int(recency_days),
        )

    out: list[Anomaly] = []
    for row in rows:
        subject = str(row["subject"] or "").strip()
        if not subject or "." not in subject:
            continue
        pattern = _decode_jsonish(row["pattern"])
        expected, end_t = _expected_today(
            pattern, today_local=today_local, now_local=now_local
        )
        if not expected or end_t is None:
            continue
        # Compute end-of-window in local tz, then add grace period.
        end_local = datetime.combine(today_local, end_t, tzinfo=tz)
        if now_local < end_local + timedelta(minutes=grace_minutes):
            continue  # window not closed yet
        agent, _, capability = subject.partition(".")
        async with pool.acquire() as conn:
            seen = await conn.fetchval(
                """
                SELECT 1 FROM event_log
                WHERE agent = $1 AND capability = $2
                  AND ts >= ($3::timestamptz - interval '1 hour')
                  AND ts <= $4::timestamptz
                LIMIT 1
                """,
                agent,
                capability,
                end_local,
                now_local,
            )
        if seen:
            continue
        out.append(
            Anomaly(
                kind="missing_habit",
                summary=(
                    f"Expected '{subject}' today between "
                    f"{pattern.get('time_window_local')} but it hasn't fired."
                ),
                severity="info",
                payload={
                    "anomaly_type": f"missing_habit:{subject}:{today_local.isoformat()}",
                    "subject": subject,
                    "pattern": pattern,
                    "confidence": float(row["confidence"] or 0.0),
                    "habit_id": int(row["id"]),
                    "expected_window": pattern.get("time_window_local"),
                },
            )
        )
    return out


async def detect_missing_followups(
    pool: Any,
    *,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    lookback_hours: int = DEFAULT_SEQUENCE_LOOKBACK_HOURS,
) -> list[Anomaly]:
    """Sequence routines A→B-within-W where A recently fired but B didn't."""
    if pool is None:
        return []
    now_utc = datetime.now(UTC)
    earliest = now_utc - timedelta(hours=lookback_hours)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, steps
            FROM routines
            WHERE source = 'routine_sequence_miner'
            """
        )

    out: list[Anomaly] = []
    for row in rows:
        steps_doc = _decode_jsonish(row["steps"])
        attributes = _decode_jsonish(steps_doc.get("attributes"))
        confidence = float(attributes.get("confidence") or 0.0)
        if confidence < confidence_floor:
            continue
        window_minutes = int(attributes.get("window_minutes") or 30)

        steps_list = steps_doc.get("steps") or []
        if not isinstance(steps_list, list) or len(steps_list) < 2:
            continue
        a_subject = str(steps_list[0].get("trigger") or "").strip()
        b_subject = str(steps_list[1].get("action") or "").strip()
        if not a_subject or not b_subject or "." not in a_subject:
            continue
        a_agent, _, a_cap = a_subject.partition(".")
        b_agent, _, b_cap = b_subject.partition(".")

        # Most recent A that fired more than window_minutes ago.
        async with pool.acquire() as conn:
            a_row = await conn.fetchrow(
                """
                SELECT id, ts FROM event_log
                WHERE agent = $1 AND capability = $2
                  AND ts >= $3::timestamptz
                  AND ts <= ($4::timestamptz - ($5::int * interval '1 minute'))
                ORDER BY ts DESC LIMIT 1
                """,
                a_agent, a_cap, earliest, now_utc, window_minutes,
            )
            if a_row is None:
                continue
            # Did B fire in window after that A?
            b_row = await conn.fetchrow(
                """
                SELECT 1 FROM event_log
                WHERE agent = $1 AND capability = $2
                  AND ts > $3::timestamptz
                  AND ts <= ($3::timestamptz + ($4::int * interval '1 minute'))
                LIMIT 1
                """,
                b_agent, b_cap, a_row["ts"], window_minutes,
            )
        if b_row:
            continue

        out.append(
            Anomaly(
                kind="missing_followup",
                summary=(
                    f"'{a_subject}' fired at "
                    f"{a_row['ts'].isoformat()} but '{b_subject}' didn't "
                    f"follow within {window_minutes} minutes "
                    f"(routine seen {int(confidence * 100)}% of the time)."
                ),
                severity="info",
                payload={
                    "anomaly_type": (
                        f"missing_followup:{a_subject}->{b_subject}:"
                        f"{a_row['id']}"
                    ),
                    "routine_id": int(row["id"]),
                    "routine_name": str(row["name"]),
                    "trigger_subject": a_subject,
                    "expected_followup": b_subject,
                    "trigger_event_id": int(a_row["id"]),
                    "trigger_ts": a_row["ts"].isoformat(),
                    "window_minutes": window_minutes,
                    "confidence": confidence,
                },
            )
        )
    return out


async def detect_missing_routines(
    pool: Any,
    *,
    user_tz_name: str = "Asia/Dubai",
) -> list[Anomaly]:
    """Run both detectors. Catch-all so a failure in one branch doesn't
    silently take down the schedule."""
    out: list[Anomaly] = []
    try:
        out.extend(await detect_missing_habits(pool, user_tz_name=user_tz_name))
    except Exception as exc:  # noqa: BLE001
        logger.warning("detect_missing_habits_failed", error=str(exc))
    try:
        out.extend(await detect_missing_followups(pool))
    except Exception as exc:  # noqa: BLE001
        logger.warning("detect_missing_followups_failed", error=str(exc))
    return out
