"""Time-window helpers that resolve relative to household_members.

Centralised so the policy engine, the late-bedtime check, the morning
brief sender, and any future "fire 30 min before bedtime" job all use
the same midnight-crossing math + the same once-per-day dedup.

The user's complaint that prompted this module:
- Morning brief was hardcoded to send at 07:30, but they wake at 09:00.
- Late bedtime check was hardcoded to fire at 23:30, but they sleep
  at 00:30, so the nudge always missed by 90 minutes.

Both should fire RELATIVE to the user's actual schedule.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Dubai"


@dataclass(slots=True)
class MemberWindow:
    member_id: int | None
    name: str | None
    sleep_time: time
    wake_time: time
    # Weekend overrides extracted from user_profile (parse_wake_or_sleep_time).
    # When set, the helpers below pick the correct variant for today's
    # weekday/weekend. Falls back to sleep_time/wake_time when absent.
    sleep_time_weekend: time | None = None
    wake_time_weekend: time | None = None

    def effective_sleep_time(self, ref: datetime) -> time:
        local = ref.astimezone(_local_tz())
        if local.weekday() >= 5 and self.sleep_time_weekend is not None:
            return self.sleep_time_weekend
        return self.sleep_time

    def effective_wake_time(self, ref: datetime) -> time:
        local = ref.astimezone(_local_tz())
        if local.weekday() >= 5 and self.wake_time_weekend is not None:
            return self.wake_time_weekend
        return self.wake_time


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("TZ", DEFAULT_TZ))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


async def load_member_windows(pool: Any | None) -> list[MemberWindow]:
    """Load (sleep_time, wake_time) for every non-pet household member.

    Members without sleep_time are skipped. Missing wake_time defaults
    to 07:00 — same fallback the TV observer uses, so behavior stays
    consistent across modules.

    Also augments each window with weekend variants parsed from
    user_profile if the user answered something like "I wake up no later
    than 9:00 AM on weekdays, 11:00 AM weekends" — without this, every
    helper used the same time 7 days a week even though the answer
    explicitly mentioned a weekend variant.
    """
    if pool is None:
        return []
    profile_rows: list[Any] = []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, sleep_time, wake_time
                  FROM household_members
                 WHERE sleep_time IS NOT NULL
                   AND role <> 'pet'
                """
            )
    except Exception:
        return []
    # Profile-row enrichment is best-effort; failures must NOT block
    # the core sleep/wake window load above.
    try:
        async with pool.acquire() as conn:
            profile_rows = await conn.fetch(
                """
                SELECT key, value
                  FROM user_profile
                 WHERE key IN ('wake_time', 'sleep_time')
                """
            )
    except Exception:
        profile_rows = []
    # Parse the structured weekend overrides ONCE at the top — currently
    # user_profile is a single-tenant key/value store so the overrides
    # apply to all members. Once profile becomes per-member we'll add a
    # member_id column and join here.
    from .profile_parser import parse_wake_or_sleep_time

    weekend_wake: time | None = None
    weekend_sleep: time | None = None
    for prow in profile_rows:
        # Defensive: row dict may not have 'key'/'value' columns (e.g.
        # when a fake test pool returns the same rows for every fetch).
        try:
            key = prow["key"]
            raw = prow["value"]
        except (KeyError, TypeError):
            continue
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith('"') and text.endswith('"'):
                import json as _json

                try:
                    text = _json.loads(text)
                except Exception:
                    text = text[1:-1]
        else:
            text = str(raw or "")
        parsed = parse_wake_or_sleep_time(text) if text else None
        if not parsed:
            continue
        if key == "wake_time" and parsed.get("weekend") != parsed.get("weekday"):
            weekend_wake = parsed["weekend"]
        if key == "sleep_time" and parsed.get("weekend") != parsed.get("weekday"):
            weekend_sleep = parsed["weekend"]
    out: list[MemberWindow] = []
    for row in rows:
        sleep = row["sleep_time"]
        wake = row["wake_time"] or time(7, 0)
        if isinstance(sleep, time) and isinstance(wake, time):
            out.append(
                MemberWindow(
                    member_id=row.get("id"),
                    name=row.get("name"),
                    sleep_time=sleep,
                    wake_time=wake,
                    sleep_time_weekend=weekend_sleep,
                    wake_time_weekend=weekend_wake,
                )
            )
    return out


def _local_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(_local_tz())


def _on_today(target: time, ref: datetime) -> datetime:
    return datetime(
        ref.year, ref.month, ref.day, target.hour, target.minute, tzinfo=ref.tzinfo
    )


def in_pre_bedtime_window(
    window: MemberWindow,
    *,
    minutes_before: int = 90,
    now: datetime | None = None,
) -> bool:
    """True if NOW is within ``minutes_before`` of this member's sleep_time.

    The window ENDS at sleep_time itself — once the user is past their
    bedtime they're either already asleep or in the post-bedtime grace
    handled separately (the existing late_bedtime_check capability).

    Handles after-midnight bedtimes correctly: at 23:35 with a 00:30
    sleep_time, the window starts at 23:00 (00:30 minus 90 min) and ends
    at 00:30 — so 23:35 is inside.
    """
    local_now = _local_now(now)
    sleep_today = _on_today(window.effective_sleep_time(local_now), local_now)
    # If sleep_time is before the current hour by a wide margin, the
    # next sleep is tomorrow — compare against tomorrow's slot.
    if sleep_today < local_now - timedelta(hours=12):
        sleep_today = sleep_today + timedelta(days=1)
    window_start = sleep_today - timedelta(minutes=minutes_before)
    return window_start <= local_now < sleep_today


def in_post_wake_window(
    window: MemberWindow,
    *,
    minutes_after: int = 60,
    now: datetime | None = None,
) -> bool:
    """True if NOW is within ``minutes_after`` of this member's wake_time.

    Used by the morning brief sender so the brief lands shortly after
    the user actually wakes up, instead of at a hardcoded 07:30.
    Respects weekend overrides via window.effective_wake_time.
    """
    local_now = _local_now(now)
    wake_today = _on_today(window.effective_wake_time(local_now), local_now)
    if wake_today > local_now + timedelta(hours=12):
        wake_today = wake_today - timedelta(days=1)
    return wake_today <= local_now < wake_today + timedelta(minutes=minutes_after)


def any_member_in_pre_bedtime(
    windows: list[MemberWindow],
    *,
    minutes_before: int = 90,
    now: datetime | None = None,
) -> MemberWindow | None:
    for w in windows:
        if in_pre_bedtime_window(w, minutes_before=minutes_before, now=now):
            return w
    return None


def any_member_in_post_wake(
    windows: list[MemberWindow],
    *,
    minutes_after: int = 60,
    now: datetime | None = None,
) -> MemberWindow | None:
    for w in windows:
        if in_post_wake_window(w, minutes_after=minutes_after, now=now):
            return w
    return None


def today_local_key(prefix: str, now: datetime | None = None) -> str:
    """A redis-key suffix like ``prefix:2026-05-15`` that rolls at local
    midnight. Used for once-per-day dedup (set with EX = 36h, the next
    day's check naturally ignores yesterday's flag)."""
    local_now = _local_now(now)
    return f"{prefix}:{local_now.date().isoformat()}"


async def already_fired_today(
    redis: Any, prefix: str, *, now: datetime | None = None
) -> bool:
    key = today_local_key(prefix, now=now)
    return bool(await redis.exists(key))


async def mark_fired_today(
    redis: Any, prefix: str, *, ttl_seconds: int = 36 * 3600, now: datetime | None = None
) -> None:
    """Set today's dedup flag with a 36h TTL — outlives one calendar day,
    auto-cleans before the next-next day's check."""
    key = today_local_key(prefix, now=now)
    await redis.set(key, "1", ex=ttl_seconds)


__all__ = [
    "MemberWindow",
    "load_member_windows",
    "in_pre_bedtime_window",
    "in_post_wake_window",
    "any_member_in_pre_bedtime",
    "any_member_in_post_wake",
    "today_local_key",
    "already_fired_today",
    "mark_fired_today",
    "_local_now",
    "_on_today",
]


def today_local_date(now: datetime | None = None) -> date:
    return _local_now(now).date()
