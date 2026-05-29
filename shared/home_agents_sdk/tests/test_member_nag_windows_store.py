"""Tests for MemberNagWindowsStore."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from home_agents_sdk.member_nag_windows_store import (
    DEFAULT_TIMEZONE,
    DEFAULT_WEEKDAY_END,
    DEFAULT_WEEKDAY_START,
    DEFAULT_WEEKEND_END,
    DEFAULT_WEEKEND_START,
    MemberNagWindowsStore,
    _validate,
)


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    return pool


# ── Defaults + validation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_defaults_when_no_pool() -> None:
    store = MemberNagWindowsStore(pool=None)
    prefs = await store.get(member_id=2)
    assert prefs["weekday_start_hour"] == DEFAULT_WEEKDAY_START
    assert prefs["weekday_end_hour"] == DEFAULT_WEEKDAY_END
    assert prefs["timezone"] == DEFAULT_TIMEZONE
    assert prefs["is_default"] is True


@pytest.mark.asyncio
async def test_get_returns_defaults_when_no_row() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    store = MemberNagWindowsStore(_pool_with(conn))
    prefs = await store.get(member_id=2)
    assert prefs["weekday_start_hour"] == DEFAULT_WEEKDAY_START
    assert prefs["is_default"] is True


@pytest.mark.asyncio
async def test_get_returns_stored_row() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "member_id": 2,
        "weekday_start_hour": 18, "weekday_end_hour": 22,
        "weekend_start_hour": 9, "weekend_end_hour": 22,
        "timezone": "Asia/Dubai",
    })
    store = MemberNagWindowsStore(_pool_with(conn))
    prefs = await store.get(member_id=2)
    assert prefs["weekday_start_hour"] == 18
    assert prefs["is_default"] is False


def test_validate_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="end"):
        _validate({"weekday_start_hour": 18, "weekday_end_hour": 17,
                   "weekend_start_hour": 10, "weekend_end_hour": 20})


def test_validate_rejects_out_of_range_hour() -> None:
    with pytest.raises(ValueError):
        _validate({"weekday_start_hour": 25, "weekday_end_hour": 26,
                   "weekend_start_hour": 10, "weekend_end_hour": 20})


# ── set() partial updates ────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_merges_only_provided_fields() -> None:
    """User says 'don't nag me before 6pm weekdays' — only the
    weekday_start should change; everything else keeps its current value."""
    conn = MagicMock()
    # get() returns existing row first
    conn.fetchrow = AsyncMock(side_effect=[
        {"member_id": 2, "weekday_start_hour": 14, "weekday_end_hour": 21,
         "weekend_start_hour": 10, "weekend_end_hour": 21, "timezone": "Asia/Dubai"},
        {"member_id": 2, "weekday_start_hour": 18, "weekday_end_hour": 21,
         "weekend_start_hour": 10, "weekend_end_hour": 21, "timezone": "Asia/Dubai"},
    ])
    store = MemberNagWindowsStore(_pool_with(conn))
    out = await store.set(member_id=2, weekday_start_hour=18)
    assert out["weekday_start_hour"] == 18
    # Other fields unchanged
    assert out["weekday_end_hour"] == 21
    assert out["weekend_start_hour"] == 10
    # The UPSERT got the merged values, not None for the rest
    upsert_args = conn.fetchrow.await_args_list[1].args
    # member_id, ws, we, wes, wee, tz
    assert upsert_args[1] == 2
    assert upsert_args[2] == 18  # new weekday_start
    assert upsert_args[3] == 21  # preserved weekday_end
    assert upsert_args[6] == "Asia/Dubai"


# ── is_nag_allowed_now ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_nag_allowed_weekday_inside_window() -> None:
    """Tuesday 15:00 Dubai, window 14:00-21:00 weekdays → allowed."""
    store = MemberNagWindowsStore(pool=None)  # uses defaults
    tuesday_3pm = datetime(2026, 5, 26, 15, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    assert await store.is_nag_allowed_now(member_id=2, now=tuesday_3pm) is True


@pytest.mark.asyncio
async def test_is_nag_allowed_weekday_before_window() -> None:
    """Tuesday 09:00 Dubai → blocked (default weekday starts 14:00)."""
    store = MemberNagWindowsStore(pool=None)
    tuesday_9am = datetime(2026, 5, 26, 9, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    assert await store.is_nag_allowed_now(member_id=2, now=tuesday_9am) is False


@pytest.mark.asyncio
async def test_is_nag_allowed_weekend_inside_window() -> None:
    """Saturday 11:00 → allowed (default weekend 10:00-21:00)."""
    store = MemberNagWindowsStore(pool=None)
    sat_11am = datetime(2026, 5, 23, 11, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    # 2026-05-23 is a Saturday
    assert sat_11am.weekday() == 5
    assert await store.is_nag_allowed_now(member_id=2, now=sat_11am) is True


@pytest.mark.asyncio
async def test_is_nag_allowed_weekend_too_early() -> None:
    """Sunday 08:00 → blocked (weekend starts 10:00 by default)."""
    store = MemberNagWindowsStore(pool=None)
    sun_8am = datetime(2026, 5, 24, 8, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    assert sun_8am.weekday() == 6
    assert await store.is_nag_allowed_now(member_id=2, now=sun_8am) is False


@pytest.mark.asyncio
async def test_is_nag_allowed_after_window_end() -> None:
    """Wednesday 22:30 → blocked (default end is 21:00)."""
    store = MemberNagWindowsStore(pool=None)
    wed_late = datetime(2026, 5, 27, 22, 30, tzinfo=ZoneInfo("Asia/Dubai"))
    assert await store.is_nag_allowed_now(member_id=2, now=wed_late) is False


@pytest.mark.asyncio
async def test_is_nag_allowed_respects_custom_window() -> None:
    """User set weekday window to 18:00-22:00. 17:00 is blocked, 18:30 OK."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "member_id": 2,
        "weekday_start_hour": 18, "weekday_end_hour": 22,
        "weekend_start_hour": 10, "weekend_end_hour": 22,
        "timezone": "Asia/Dubai",
    })
    store = MemberNagWindowsStore(_pool_with(conn))
    wed_5pm = datetime(2026, 5, 27, 17, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    wed_630pm = datetime(2026, 5, 27, 18, 30, tzinfo=ZoneInfo("Asia/Dubai"))
    assert await store.is_nag_allowed_now(member_id=2, now=wed_5pm) is False
    assert await store.is_nag_allowed_now(member_id=2, now=wed_630pm) is True
