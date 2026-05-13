"""Infer nightly sleep quality from HealthKit sleep rows and bedroom observer events."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from home_agents_sdk import tool
from home_agents_sdk.health_store import HealthStore
from home_agents_sdk.sleep_summaries_store import SleepSummariesStore

_POOL: asyncpg.Pool | None = None

_SLEEP_METRICS = (
    "sleep_asleep",
    "sleep_deep",
    "sleep_rem",
    "sleep_core",
    "sleep_awake",
    "sleep_inBed",
)
_ASLEEP_METRICS = {"sleep_asleep", "sleep_deep", "sleep_rem", "sleep_core"}
_QUALITY_LABELS = ("great", "decent", "restless", "short")
_QUALITY_DISPLAY = {
    "great": "Restful",
    "decent": "Decent",
    "restless": "Restless",
    "short": "Short",
}
_QUALITY_ALIASES = {
    "restful": "great",
    "good": "decent",
    "ok": "decent",
    "okay": "decent",
    "bad": "restless",
    "rough": "restless",
}


async def _pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        url = os.getenv("DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents")
        _POOL = await asyncpg.create_pool(url, min_size=1, max_size=5)
    return _POOL


def _tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("USER_TZ", "Asia/Dubai"))


def _parse_iso(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date(raw: Any) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw.strip():
        return date.fromisoformat(raw.strip()[:10])
    local_now = datetime.now(_tz())
    return local_now.date() - timedelta(days=1)


def _parse_time(raw: Any, default: time) -> time:
    if isinstance(raw, time):
        return raw.replace(tzinfo=None)
    if not isinstance(raw, str) or not raw.strip():
        return default
    text = raw.strip().split("+", maxsplit=1)[0].split(".", maxsplit=1)[0]
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return default
    return time(max(0, min(hour, 23)), max(0, min(minute, 59)))


def _coerce_int(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _coerce_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


async def _member(pool: asyncpg.Pool, member_id: int | None = None) -> dict[str, Any] | None:
    try:
        async with pool.acquire() as conn:
            if member_id is not None:
                row = await conn.fetchrow(
                    """
                    SELECT id, name, role, telegram_chat_id, sleep_time, wake_time
                      FROM household_members
                     WHERE id = $1
                     LIMIT 1
                    """,
                    int(member_id),
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT id, name, role, telegram_chat_id, sleep_time, wake_time
                      FROM household_members
                     WHERE role <> 'pet'
                     ORDER BY CASE role
                                  WHEN 'adult' THEN 0
                                  WHEN 'child' THEN 1
                                  WHEN 'guest' THEN 2
                                  ELSE 3
                              END,
                              id
                     LIMIT 1
                    """
                )
    except Exception:  # noqa: BLE001 - inference should degrade if profile is absent
        return None
    return dict(row) if row else None


async def _observer_events(
    pool: asyncpg.Pool,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, agent, capability, summary, payload
                  FROM event_log
                 WHERE agent = 'observer.sleep'
                   AND capability = ANY($1::text[])
                   AND ts >= $2
                   AND ts <= $3
                 ORDER BY ts ASC, id ASC
                 LIMIT 300
                """,
                ["sleep.likely_asleep", "sleep.likely_awake"],
                start_at,
                end_at,
            )
    except Exception:  # noqa: BLE001 - observer history is best effort
        return []
    return [dict(row) for row in rows]


def _expected_times(night_of: date, member: dict[str, Any] | None) -> tuple[datetime, datetime]:
    zone = _tz()
    sleep_t = _parse_time(member.get("sleep_time") if member else None, time(23, 0))
    wake_t = _parse_time(member.get("wake_time") if member else None, time(7, 0))
    expected_sleep = datetime.combine(night_of, sleep_t, tzinfo=zone)
    wake_day = night_of + timedelta(days=1) if wake_t <= sleep_t else night_of
    expected_wake = datetime.combine(wake_day, wake_t, tzinfo=zone)
    return expected_sleep, expected_wake


def _row_interval(
    row: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    started_at = _parse_iso(row.get("started_at"))
    if started_at is None:
        return None
    ended_at = _parse_iso(row.get("ended_at"))
    if ended_at is None:
        minutes = _coerce_float(row.get("value"))
        if minutes is None:
            return None
        ended_at = started_at + timedelta(minutes=minutes)
    start_utc = max(started_at.astimezone(UTC), window_start.astimezone(UTC))
    end_utc = min(ended_at.astimezone(UTC), window_end.astimezone(UTC))
    if end_utc <= start_utc:
        return None
    return start_utc, end_utc


def _union_minutes(intervals: list[tuple[datetime, datetime]]) -> int:
    if not intervals:
        return 0
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals, key=lambda item: item[0]):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    seconds = sum((end - start).total_seconds() for start, end in merged)
    return int(round(seconds / 60))


def _duration_label(minutes: int | None) -> str:
    if minutes is None or minutes <= 0:
        return "unknown duration"
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _event_kind(event: dict[str, Any]) -> str:
    return str(event.get("capability") or event.get("kind") or "")


def _event_time(event: dict[str, Any]) -> datetime | None:
    payload = _json_dict(event.get("payload"))
    return _parse_iso(payload.get("detected_at")) or _parse_iso(event.get("ts"))


def _nearest_event_time(
    events: list[dict[str, Any]],
    *,
    kind: str,
    target: datetime,
    after: datetime | None = None,
    before: datetime | None = None,
) -> datetime | None:
    candidates: list[datetime] = []
    for event in events:
        if _event_kind(event) != kind:
            continue
        ts = _event_time(event)
        if ts is None:
            continue
        ts = ts.astimezone(UTC)
        if after is not None and ts < after.astimezone(UTC):
            continue
        if before is not None and ts > before.astimezone(UTC):
            continue
        candidates.append(ts)
    if not candidates:
        return None
    target_utc = target.astimezone(UTC)
    return min(candidates, key=lambda ts: abs((ts - target_utc).total_seconds()))


def _last_event_time(
    events: list[dict[str, Any]],
    *,
    kind: str,
    before: datetime,
    after: datetime | None = None,
) -> datetime | None:
    candidates: list[datetime] = []
    for event in events:
        if _event_kind(event) != kind:
            continue
        ts = _event_time(event)
        if ts is None:
            continue
        ts = ts.astimezone(UTC)
        if ts > before.astimezone(UTC):
            continue
        if after is not None and ts < after.astimezone(UTC):
            continue
        candidates.append(ts)
    return max(candidates) if candidates else None


def _interruption_count(
    events: list[dict[str, Any]],
    asleep_at: datetime,
    awake_at: datetime,
) -> int:
    start_cutoff = asleep_at.astimezone(UTC) + timedelta(minutes=20)
    end_cutoff = awake_at.astimezone(UTC) - timedelta(minutes=20)
    if end_cutoff <= start_cutoff:
        return 0
    count = 0
    for event in events:
        if _event_kind(event) != "sleep.likely_awake":
            continue
        ts = _event_time(event)
        if ts is not None and start_cutoff <= ts.astimezone(UTC) <= end_cutoff:
            count += 1
    return count


def _delta_minutes(left: datetime | None, right: datetime | None) -> int | None:
    if left is None or right is None:
        return None
    seconds = abs((left.astimezone(UTC) - right.astimezone(UTC)).total_seconds())
    return int(round(seconds / 60))


def _classify_quality(
    *,
    duration_minutes: int,
    deep_sleep_minutes: int | None,
    interruptions: int,
    observer_delta_minutes: int | None,
    typical_sleep_delta_minutes: int | None,
    typical_wake_delta_minutes: int | None,
    crossed_midnight: bool,
) -> tuple[str, str]:
    reasons: list[str] = [f"total sleep {_duration_label(duration_minutes)}"]
    if not crossed_midnight:
        reasons.append("sleep window did not cross midnight")

    if duration_minutes < 5 * 60:
        return "short", "; ".join([*reasons, "under the 5h short-sleep threshold"])

    restless: list[str] = []
    if interruptions >= 3:
        restless.append(f"{interruptions} bedroom awake events")
    if observer_delta_minutes is not None and observer_delta_minutes > 30:
        restless.append(f"observer wake differed from HealthKit by {observer_delta_minutes} min")
    if typical_sleep_delta_minutes is not None and typical_sleep_delta_minutes > 30:
        restless.append(f"bedtime differed from usual by {typical_sleep_delta_minutes} min")
    if typical_wake_delta_minutes is not None and typical_wake_delta_minutes > 30:
        restless.append(f"wake time differed from usual by {typical_wake_delta_minutes} min")
    if not crossed_midnight and duration_minutes < 7 * 60:
        restless.append("short non-overnight window")
    if restless:
        return "restless", "; ".join([*reasons, *restless])

    if duration_minutes >= 7 * 60 and deep_sleep_minutes is not None and deep_sleep_minutes > 60:
        ratio = deep_sleep_minutes / max(1, duration_minutes)
        reasons.append(f"deep sleep {deep_sleep_minutes} min ({ratio:.0%})")
        return "great", "; ".join(reasons)

    if 5 * 60 <= duration_minutes < 7 * 60 and interruptions == 0:
        return "decent", "; ".join([*reasons, "5-7h with no bedroom interruptions"])

    return "decent", "; ".join([*reasons, "no strong restless signal"])


def _keyboard_for(sleep_summary_id: int, guessed: str) -> list[list[dict[str, str]]]:
    seen = {guessed}
    ordered = [guessed]
    for label in _QUALITY_LABELS:
        if label not in seen:
            ordered.append(label)
            seen.add(label)
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for label in ordered:
        display = _QUALITY_DISPLAY.get(label, label.title())
        text = f"✅ {display}" if label == guessed else display
        row.append({"text": text, "callback": f"sleep:{sleep_summary_id}:{label}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Skip", "callback": f"sleep:{sleep_summary_id}:_skip"}])
    return rows


def _bedtime_keyboard() -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "Yes, wind down", "callback": "sleep:bedtime:wind_down"},
            {"text": "Not tonight", "callback": "sleep:bedtime:_skip"},
        ]
    ]


def _summary_for(duration_minutes: int, quality: str) -> str:
    quality_phrase = {
        "great": "great night",
        "decent": "decent night",
        "restless": "restless night",
        "short": "short night",
    }.get(quality, f"{quality} night")
    duration = _duration_label(duration_minutes)
    return f"🌙 You slept ~{duration}, {quality_phrase}. Restless or restful?"


def _normalise_quality(raw: str) -> str | None:
    value = str(raw or "").strip().casefold().replace(" ", "_")
    value = _QUALITY_ALIASES.get(value, value)
    return value if value in _QUALITY_LABELS else None


async def _health_sleep_rows(
    health: HealthStore,
    *,
    member_id: int | None,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    hours_back = int(
        max(
            36,
            min(
                365 * 24,
                ((datetime.now(UTC) - window_start.astimezone(UTC)).total_seconds() / 3600) + 24,
            ),
        )
    )
    metric_rows = await asyncio.gather(
        *(health.list_recent(metric=metric, hours=hours_back) for metric in _SLEEP_METRICS)
    )
    rows: list[dict[str, Any]] = []
    for group in metric_rows:
        for row in group:
            if not isinstance(row, dict) or row.get("metric") not in _SLEEP_METRICS:
                continue
            row_member_id = _coerce_int(row.get("member_id"))
            if member_id is not None and row_member_id not in (None, int(member_id)):
                continue
            if _row_interval(row, window_start, window_end) is not None:
                rows.append(row)
    return rows


@tool("infer_sleep_summary", side_effects=True)
async def infer_sleep_summary(
    member_id: int | None = None,
    night_of: str | date | None = None,
    detected_at: str | datetime | None = None,
    observer_likely_asleep_at: str | datetime | None = None,
    observer_likely_awake_at: str | datetime | None = None,
    **payload: Any,
) -> dict[str, Any]:
    """Infer last night's sleep quality and return a Telegram-ready keyboard."""
    pool = await _pool()
    member = await _member(pool, _coerce_int(member_id))
    resolved_member_id = _coerce_int(member.get("id") if member else member_id)
    night = _parse_date(night_of)
    expected_sleep, expected_wake = _expected_times(night, member)
    window_start = expected_sleep - timedelta(hours=4)
    window_end = expected_wake + timedelta(hours=4)

    health = HealthStore(pool)
    health_rows = await _health_sleep_rows(
        health,
        member_id=resolved_member_id,
        window_start=window_start,
        window_end=window_end,
    )
    events = await _observer_events(pool, window_start, window_end + timedelta(hours=2))

    sleep_intervals = [
        interval
        for row in health_rows
        if row.get("metric") in _ASLEEP_METRICS
        if (interval := _row_interval(row, window_start, window_end)) is not None
    ]
    used_in_bed_fallback = False
    if not sleep_intervals:
        sleep_intervals = [
            interval
            for row in health_rows
            if row.get("metric") == "sleep_inBed"
            if (interval := _row_interval(row, window_start, window_end)) is not None
        ]
        used_in_bed_fallback = bool(sleep_intervals)

    if not sleep_intervals:
        return {
            "ok": False,
            "summary": "",
            "message": "No HealthKit sleep rows found for last night yet.",
            "notify": False,
            "keyboard": [],
        }

    asleep_at = min(start for start, _end in sleep_intervals)
    awake_at = max(end for _start, end in sleep_intervals)
    duration_minutes = _union_minutes(sleep_intervals)
    deep_intervals = [
        interval
        for row in health_rows
        if row.get("metric") == "sleep_deep"
        if (interval := _row_interval(row, window_start, window_end)) is not None
    ]
    deep_sleep_minutes = _union_minutes(deep_intervals) if deep_intervals else None

    explicit_awake = _parse_iso(observer_likely_awake_at) or _parse_iso(detected_at)
    explicit_asleep = _parse_iso(observer_likely_asleep_at)
    observer_awake_at = explicit_awake or _nearest_event_time(
        events,
        kind="sleep.likely_awake",
        target=awake_at,
        after=asleep_at,
        before=window_end + timedelta(hours=2),
    )
    observer_asleep_at = explicit_asleep or _last_event_time(
        events,
        kind="sleep.likely_asleep",
        before=asleep_at + timedelta(minutes=90),
        after=window_start,
    )
    interruptions = _interruption_count(events, asleep_at, awake_at)
    observer_delta = _delta_minutes(observer_awake_at, awake_at)
    typical_sleep_delta = _delta_minutes(asleep_at, expected_sleep)
    typical_wake_delta = _delta_minutes(awake_at, expected_wake)
    crossed_midnight = asleep_at.astimezone(_tz()).date() != awake_at.astimezone(_tz()).date()

    quality, reasoning = _classify_quality(
        duration_minutes=duration_minutes,
        deep_sleep_minutes=deep_sleep_minutes,
        interruptions=interruptions,
        observer_delta_minutes=observer_delta,
        typical_sleep_delta_minutes=typical_sleep_delta,
        typical_wake_delta_minutes=typical_wake_delta,
        crossed_midnight=crossed_midnight,
    )
    if used_in_bed_fallback:
        reasoning += "; used in-bed rows because asleep stages were unavailable"

    store = SleepSummariesStore(pool)
    sleep_summary_id = await store.insert_summary(
        household_member_id=resolved_member_id,
        night_of=night,
        asleep_at=asleep_at,
        awake_at=awake_at,
        duration_minutes=duration_minutes,
        deep_sleep_minutes=deep_sleep_minutes,
        observer_likely_asleep_at=observer_asleep_at,
        observer_likely_awake_at=observer_awake_at,
        interruptions=interruptions,
        guessed_quality=quality,
        guessed_reasoning=reasoning,
    )
    keyboard = _keyboard_for(sleep_summary_id, quality) if sleep_summary_id is not None else []
    return {
        "ok": True,
        "summary": _summary_for(duration_minutes, quality),
        "quality": quality,
        "reasoning": reasoning,
        "sleep_summary_id": sleep_summary_id,
        "keyboard": keyboard,
        "notify": sleep_summary_id is not None,
        "duration_minutes": duration_minutes,
        "deep_sleep_minutes": deep_sleep_minutes,
        "interruptions": interruptions,
    }


@tool("confirm_sleep_summary", side_effects=True)
async def confirm_sleep_summary(
    sleep_summary_id: int,
    quality: str,
    chat_id: int | None = None,
) -> dict[str, Any]:
    if not isinstance(sleep_summary_id, int) or sleep_summary_id <= 0:
        return {"ok": False, "error": "sleep_summary_id must be a positive integer"}
    confirmed_quality = _normalise_quality(quality)
    if confirmed_quality is None:
        return {"ok": False, "error": "quality must be great, decent, restless, or short"}
    store = SleepSummariesStore(await _pool())
    record = await store.confirm(
        sleep_summary_id,
        confirmed_quality=confirmed_quality,
        chat_id=chat_id,
    )
    if record is None:
        return {"ok": False, "error": "sleep_summary not found"}
    return {"ok": True, "record": json.loads(json.dumps(record, default=str))}


@tool("late_bedtime_check")
async def late_bedtime_check(
    member_id: int | None = None,
    observed_at: str | datetime | None = None,
    grace_minutes: int = 30,
    **payload: Any,
) -> dict[str, Any]:
    pool = await _pool()
    member = await _member(pool, _coerce_int(member_id))
    sleep_t = _parse_time(member.get("sleep_time") if member else None, time(23, 0))
    now = (_parse_iso(observed_at) or datetime.now(_tz())).astimezone(_tz())
    sleep_day = (
        now.date() - timedelta(days=1)
        if now.hour < 12 and sleep_t.hour >= 12
        else now.date()
    )
    expected_sleep = datetime.combine(sleep_day, sleep_t, tzinfo=_tz())
    grace = max(0, min(int(grace_minutes), 180))
    if now < expected_sleep + timedelta(minutes=grace):
        return {"ok": True, "notify": False, "summary": ""}

    events = await _observer_events(pool, expected_sleep - timedelta(hours=12), now)
    signals = payload.get("signals") if isinstance(payload.get("signals"), dict) else {}
    signal_active = signals and (not signals.get("bedroom_lights_off") or not signals.get("tv_off"))
    asleep_since_bedtime = any(
        _event_kind(event) == "sleep.likely_asleep"
        and (event_ts := _event_time(event)) is not None
        and event_ts.astimezone(_tz()) >= expected_sleep
        for event in events
    )
    last_kind = _event_kind(events[-1]) if events else ""
    likely_already_asleep = asleep_since_bedtime or last_kind == "sleep.likely_asleep"
    if likely_already_asleep and not signal_active:
        return {"ok": True, "notify": False, "summary": ""}

    return {
        "ok": True,
        "notify": True,
        "summary": (
            "🌙 It's past your usual bedtime. Want me to dim the lights and turn off the TV?"
        ),
        "keyboard": _bedtime_keyboard(),
        "member_id": _coerce_int(member.get("id") if member else member_id),
    }
