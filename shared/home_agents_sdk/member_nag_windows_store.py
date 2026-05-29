"""Per-member nag window preferences.

Quiet hours are weekday/weekend split since work patterns differ. The
default keeps notifications out of typical work hours (before 14:00
weekdays) while still letting morning chore digests land.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg


DEFAULT_WEEKDAY_START = 14
DEFAULT_WEEKDAY_END = 21
DEFAULT_WEEKEND_START = 10
DEFAULT_WEEKEND_END = 21
DEFAULT_TIMEZONE = "Asia/Dubai"


class MemberNagWindowsStore:
    """CRUD for per-member quiet-hours preferences."""

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def get(self, member_id: int) -> dict[str, Any]:
        """Always returns a dict — falls back to defaults if no row."""
        defaults = {
            "member_id": int(member_id),
            "weekday_start_hour": DEFAULT_WEEKDAY_START,
            "weekday_end_hour": DEFAULT_WEEKDAY_END,
            "weekend_start_hour": DEFAULT_WEEKEND_START,
            "weekend_end_hour": DEFAULT_WEEKEND_END,
            "timezone": DEFAULT_TIMEZONE,
            "is_default": True,
        }
        if not self._ready or self.pool is None:
            return defaults
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT member_id, weekday_start_hour, weekday_end_hour,
                       weekend_start_hour, weekend_end_hour, timezone
                FROM member_nag_windows WHERE member_id = $1
                """,
                int(member_id),
            )
        if row is None:
            return defaults
        out = dict(row)
        out["is_default"] = False
        return out

    async def set(
        self,
        member_id: int,
        *,
        weekday_start_hour: int | None = None,
        weekday_end_hour: int | None = None,
        weekend_start_hour: int | None = None,
        weekend_end_hour: int | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Upsert; only provided fields are touched, defaults fill the rest."""
        current = await self.get(member_id)
        merged = {
            "weekday_start_hour": weekday_start_hour if weekday_start_hour is not None
                                  else current["weekday_start_hour"],
            "weekday_end_hour": weekday_end_hour if weekday_end_hour is not None
                                else current["weekday_end_hour"],
            "weekend_start_hour": weekend_start_hour if weekend_start_hour is not None
                                  else current["weekend_start_hour"],
            "weekend_end_hour": weekend_end_hour if weekend_end_hour is not None
                                else current["weekend_end_hour"],
            "timezone": timezone if timezone is not None else current["timezone"],
        }
        _validate(merged)
        if not self._ready or self.pool is None:
            return {**merged, "member_id": int(member_id), "is_default": False}
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO member_nag_windows(
                    member_id, weekday_start_hour, weekday_end_hour,
                    weekend_start_hour, weekend_end_hour, timezone
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (member_id) DO UPDATE SET
                    weekday_start_hour = EXCLUDED.weekday_start_hour,
                    weekday_end_hour = EXCLUDED.weekday_end_hour,
                    weekend_start_hour = EXCLUDED.weekend_start_hour,
                    weekend_end_hour = EXCLUDED.weekend_end_hour,
                    timezone = EXCLUDED.timezone,
                    updated_at = now()
                RETURNING member_id, weekday_start_hour, weekday_end_hour,
                          weekend_start_hour, weekend_end_hour, timezone
                """,
                int(member_id), merged["weekday_start_hour"], merged["weekday_end_hour"],
                merged["weekend_start_hour"], merged["weekend_end_hour"], merged["timezone"],
            )
        out = dict(row) if row else merged
        out["is_default"] = False
        return out

    async def is_nag_allowed_now(
        self, member_id: int, *, now: datetime | None = None
    ) -> bool:
        """True iff the current local time is inside the member's nag window."""
        prefs = await self.get(member_id)
        try:
            tz = ZoneInfo(prefs["timezone"])
        except ZoneInfoNotFoundError:
            tz = ZoneInfo(DEFAULT_TIMEZONE)
        now_local = (now or datetime.now(tz)).astimezone(tz)
        # weekday(): Monday=0 .. Sunday=6. Weekend = Friday+Saturday for UAE.
        # But the more universal expectation is Saturday/Sunday; the user
        # can override via timezone or by setting equal weekday/weekend.
        is_weekend = now_local.weekday() in (5, 6)
        if is_weekend:
            start, end = prefs["weekend_start_hour"], prefs["weekend_end_hour"]
        else:
            start, end = prefs["weekday_start_hour"], prefs["weekday_end_hour"]
        return start <= now_local.hour < end


def _validate(prefs: dict[str, int]) -> None:
    for label, start_key, end_key in (
        ("weekday", "weekday_start_hour", "weekday_end_hour"),
        ("weekend", "weekend_start_hour", "weekend_end_hour"),
    ):
        s, e = prefs[start_key], prefs[end_key]
        if not (0 <= s <= 23 and 0 <= e <= 24):
            raise ValueError(f"{label} hours must be 0-24")
        if e <= s:
            raise ValueError(f"{label} end ({e}) must be > start ({s})")
