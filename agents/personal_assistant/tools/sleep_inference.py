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
# Single sleep_asleep / sleep_inBed rows longer than this are almost
# always Health Auto Export "outer envelope" bundles that span multiple
# unrelated sleep sessions rather than a single human-plausible night.
# 14 hours covers oversleep, weekend lie-ins, and recovery naps without
# admitting the 18-24h envelope rows HAE periodically emits.
_MAX_PLAUSIBLE_SLEEP_HOURS = 14
# When a parent interval's union with N>=2 child intervals (same metric)
# matches its own span within this tolerance, treat it as an envelope
# row and drop it in favour of the children. Fajr-night example: parent
# 02:00→09:00 with children 02:00→05:00 + 06:00→09:00 — the children sum
# to 6h, parent claims 7h. The 60-min gap is the Fajr awake period and
# must NOT be counted as sleep.
_ENVELOPE_TOLERANCE_MIN = 5
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


def _default_night_of(member: dict[str, Any] | None) -> date:
    """Pick the correct ``night_of`` for "the sleep that just ended".

    For pre-midnight sleepers (sleep_time >= 12:00 like 22:00 or 23:30),
    "last night" means yesterday — they fell asleep yesterday evening
    and woke up this morning. night_of = today − 1.

    For past-midnight sleepers (sleep_time < 12:00 like 00:30 or 02:00),
    "last night" means today — they fell asleep early THIS morning and
    woke up later THIS morning. night_of = today.

    Without this distinction the cron grabs the wrong 24h window, which
    is how 00:30→09:00 user Saeed's sleep_summaries kept pointing at
    yesterday's daytime data instead of last night's actual sleep.
    """
    local_today = datetime.now(_tz()).date()
    sleep_t = _parse_time(member.get("sleep_time") if member else None, time(23, 0))
    if sleep_t.hour < 12:
        return local_today
    return local_today - timedelta(days=1)


def _interval_covers_children(
    parent: tuple[datetime, datetime],
    children: list[tuple[datetime, datetime]],
    *,
    tolerance_minutes: int = _ENVELOPE_TOLERANCE_MIN,
) -> bool:
    """Return True iff ``parent`` is an outer-envelope of ``children``.

    "Envelope" means: parent contains 2+ children whose union spans
    nearly the same window (start ~= parent.start, end ~= parent.end)
    but with measurable interior gaps. The classic HAE failure mode for
    split-night sleepers (Fajr wake → back to sleep): parent row
    02:00→09:00 with two children 02:00→05:00 + 06:00→09:00. Without
    excluding parent we'd count the 60-min gap as sleep.
    """
    if len(children) < 2:
        return False
    tol = timedelta(minutes=max(0, tolerance_minutes))
    inside = [
        (max(c_start, parent[0]), min(c_end, parent[1]))
        for c_start, c_end in children
        if c_start >= parent[0] - tol and c_end <= parent[1] + tol
    ]
    inside = [(s, e) for s, e in inside if e > s]
    if len(inside) < 2:
        return False
    if abs((min(s for s, _ in inside) - parent[0]).total_seconds()) > tol.total_seconds():
        return False
    if abs((max(e for _, e in inside) - parent[1]).total_seconds()) > tol.total_seconds():
        return False
    # Interior gap is what makes this an "envelope" rather than a single
    # contiguous segment a HealthKit summariser happened to emit.
    merged: list[list[datetime]] = []
    for s, e in sorted(inside, key=lambda item: item[0]):
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        elif e > merged[-1][1]:
            merged[-1][1] = e
    parent_minutes = (parent[1] - parent[0]).total_seconds() / 60
    union_minutes = sum((e - s).total_seconds() for s, e in merged) / 60
    gap_minutes = parent_minutes - union_minutes
    return gap_minutes > tolerance_minutes


def _strip_envelope_rows(
    rows: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop HAE outer-envelope rows that the inference layer can't trust.

    Two passes:
      1. Drop any single row whose RAW interval (unclipped started_at →
         ended_at) exceeds ``_MAX_PLAUSIBLE_SLEEP_HOURS`` — these are
         HAE bundling bugs (e.g. a 23h row covering two unrelated days).
         The clipped interval can hide this if one end falls outside
         the analysis window.
      2. Within each metric, drop any row whose clipped interval is an
         envelope around 2+ smaller clipped rows (see
         ``_interval_covers_children``). This catches Fajr-night cases
         where HealthKit also reports the union.

    Returns ``(kept_rows, dropped_rows)`` so callers can surface the
    dropped data in reasoning text for transparency.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    intervals: dict[int, tuple[datetime, datetime]] = {}

    def _raw_span_hours(row: dict[str, Any]) -> float | None:
        started_at = _parse_iso(row.get("started_at"))
        if started_at is None:
            return None
        ended_at = _parse_iso(row.get("ended_at"))
        if ended_at is None:
            minutes = _coerce_float(row.get("value"))
            if minutes is None:
                return None
            ended_at = started_at + timedelta(minutes=minutes)
        return (ended_at - started_at).total_seconds() / 3600

    for idx, row in enumerate(rows):
        raw_hours = _raw_span_hours(row)
        if raw_hours is not None and raw_hours > _MAX_PLAUSIBLE_SLEEP_HOURS:
            dropped.append(row)
            continue
        interval = _row_interval(row, window_start, window_end)
        if interval is None:
            kept.append(row)
            continue
        intervals[len(kept)] = interval
        kept.append(row)

    by_metric: dict[str, list[tuple[int, tuple[datetime, datetime]]]] = {}
    for idx, row in enumerate(kept):
        if idx not in intervals:
            continue
        metric = str(row.get("metric") or "")
        if not metric:
            continue
        by_metric.setdefault(metric, []).append((idx, intervals[idx]))

    envelope_indices: set[int] = set()
    for groups in by_metric.values():
        if len(groups) < 3:
            continue
        for parent_idx, parent_interval in groups:
            children = [
                child_interval
                for child_idx, child_interval in groups
                if child_idx != parent_idx
            ]
            if _interval_covers_children(parent_interval, children):
                envelope_indices.add(parent_idx)

    if envelope_indices:
        new_kept: list[dict[str, Any]] = []
        for idx, row in enumerate(kept):
            if idx in envelope_indices:
                dropped.append(row)
            else:
                new_kept.append(row)
        kept = new_kept
    return kept, dropped


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


# Bedtime drift detection — how far off can configured sleep_time be
# from the observed median before we propose updating it. Two hours is
# generous enough that one-off late nights or recovery sleep don't
# trigger spurious nudges, but tight enough that a real schedule shift
# gets caught within a week.
_BEDTIME_DRIFT_THRESHOLD_MIN = 60
_BEDTIME_DRIFT_MIN_NIGHTS = 4
_BEDTIME_DRIFT_LOOKBACK_NIGHTS = 7


def _median_bedtime(
    asleep_at_times: list[datetime], *, anchor: time
) -> time | None:
    """Compute a stable median bedtime from a list of asleep_at timestamps.

    Crossing midnight makes naive averaging fail (00:30 averaged with
    23:30 = 12:00, which is nonsense). We anchor each timestamp to a
    rolling clock relative to ``anchor`` (the configured sleep_time)
    and average in the anchored space, then convert back.
    """
    if not asleep_at_times:
        return None
    zone = _tz()
    anchor_minutes = anchor.hour * 60 + anchor.minute
    samples: list[int] = []
    for ts in asleep_at_times:
        local = ts.astimezone(zone)
        clock = local.hour * 60 + local.minute
        delta = clock - anchor_minutes
        if delta > 12 * 60:
            delta -= 24 * 60
        elif delta < -12 * 60:
            delta += 24 * 60
        samples.append(delta)
    samples.sort()
    n = len(samples)
    median_delta = (samples[n // 2] + samples[~n // 2]) // 2
    median_clock = (anchor_minutes + median_delta) % (24 * 60)
    return time(median_clock // 60, median_clock % 60)


async def _recent_confirmed_or_observed_bedtimes(
    pool: asyncpg.Pool,
    *,
    member_id: int,
    lookback_nights: int = _BEDTIME_DRIFT_LOOKBACK_NIGHTS,
) -> list[datetime]:
    """Pull the last N nights' asleep_at timestamps for drift analysis."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT asleep_at
                  FROM sleep_summaries
                 WHERE household_member_id = $1
                   AND asleep_at IS NOT NULL
                 ORDER BY night_of DESC
                 LIMIT $2
                """,
                member_id,
                int(lookback_nights),
            )
    except Exception:  # noqa: BLE001
        return []
    return [row["asleep_at"] for row in rows if row.get("asleep_at")]


async def _propose_bedtime_update_if_drifted(
    pool: asyncpg.Pool,
    *,
    member: dict[str, Any] | None,
) -> int | None:
    """Closes proposal #53. If the observed bedtime over the last
    ``_BEDTIME_DRIFT_LOOKBACK_NIGHTS`` consistently differs from the
    configured ``sleep_time`` by more than the threshold, file a
    suggested_action proposal asking the user to confirm an update.

    Best-effort: any failure logs + returns None — sleep inference must
    never fail because the drift check failed.
    """
    if member is None:
        return None
    member_id = _coerce_int(member.get("id"))
    if member_id is None:
        return None
    configured_sleep_t = member.get("sleep_time")
    if not isinstance(configured_sleep_t, time):
        return None

    try:
        from home_agents_sdk.reflection_store import ReflectionStore
    except Exception:  # noqa: BLE001
        return None

    samples = await _recent_confirmed_or_observed_bedtimes(
        pool, member_id=member_id
    )
    if len(samples) < _BEDTIME_DRIFT_MIN_NIGHTS:
        return None

    anchor = configured_sleep_t
    median_t = _median_bedtime(samples, anchor=anchor)
    if median_t is None:
        return None

    median_clock = median_t.hour * 60 + median_t.minute
    anchor_clock = anchor.hour * 60 + anchor.minute
    delta = median_clock - anchor_clock
    if delta > 12 * 60:
        delta -= 24 * 60
    elif delta < -12 * 60:
        delta += 24 * 60
    if abs(delta) < _BEDTIME_DRIFT_THRESHOLD_MIN:
        return None

    # Don't spam: only emit if there isn't already a pending proposal
    # of this kind for this member within the last 14 days.
    try:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                """
                SELECT 1
                  FROM proposals
                 WHERE for_member_id = $1
                   AND status = 'pending'
                   AND title ILIKE 'Update bedtime%'
                   AND created_at >= now() - interval '14 days'
                 LIMIT 1
                """,
                member_id,
            )
    except Exception:  # noqa: BLE001
        existing = None
    if existing:
        return None

    direction = "later" if delta > 0 else "earlier"
    member_name = str(member.get("name") or "you")
    title = (
        f"Update bedtime for {member_name} to ~{median_t.strftime('%H:%M')} "
        f"({abs(delta)} min {direction} than configured)"
    )
    rationale = (
        f"Configured household_members.sleep_time = "
        f"{anchor.strftime('%H:%M')} for {member_name}. Observed median "
        f"bedtime over the last {len(samples)} nights is "
        f"{median_t.strftime('%H:%M')} ({direction} by {abs(delta)} min). "
        f"Pre-bedtime nudges, late_bedtime_check, and the morning brief "
        f"all key off sleep_time so they're firing at the wrong moment. "
        f"Accept to UPDATE household_members SET sleep_time = "
        f"'{median_t.strftime('%H:%M')}' WHERE id = {member_id}."
    )

    store = ReflectionStore(pool)
    try:
        return await store.add_proposal(
            kind="suggested_action",
            title=title,
            rationale=rationale,
            confidence=0.85,
            impact_estimate="aligns sleep-related nudges with actual schedule",
            for_member_id=member_id,
        )
    except Exception:  # noqa: BLE001
        return None


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
    night = _parse_date(night_of) if night_of is not None else _default_night_of(member)
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
    # Drop HAE outer-envelope bundles (e.g. a 23h sleep_asleep row that
    # actually covers two unrelated days' sleep) and >14h implausible
    # singletons. Without this the union of intervals double-counts.
    health_rows, dropped_envelope_rows = _strip_envelope_rows(
        health_rows, window_start, window_end
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
    if dropped_envelope_rows:
        reasoning += (
            f"; ignored {len(dropped_envelope_rows)} envelope/over-long HAE row(s)"
        )

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
    # Best-effort: after each nightly summary, also check if the
    # configured bedtime has drifted from observed reality. Surfaces
    # as a one-tap suggested_action proposal — see proposal #53.
    try:
        await _propose_bedtime_update_if_drifted(pool, member=member)
    except Exception:  # noqa: BLE001
        pass
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
    history = await store.recent(limit=14)
    same = sum(1 for r in history if (r.get("confirmed_quality") or "") == confirmed_quality)
    was_correction = confirmed_quality != (record.get("guessed_quality") or "")
    learning = _sleep_learning(confirmed_quality, same, len(history), was_correction=was_correction)
    return {
        "ok": True,
        "record": json.loads(json.dumps(record, default=str)),
        "learning": learning,
    }


def _sleep_learning(
    quality: str, same: int, total: int, *, was_correction: bool
) -> str:
    if was_correction:
        return f"Got it — your nights have been more {quality} than I thought."
    if same >= 4:
        return (
            f"Saved as {quality}. {same}/{total} of recent nights were {quality} — "
            "I'm getting your sleep pattern."
        )
    if same >= 1:
        return f"Saved as {quality}. ({same} other {quality} night(s) recently.)"
    return f"Saved as {quality}."


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
