"""Tests for orchestrator.member_windows — the shared time-window helpers
that resolve sleep_time/wake_time per household member.

Covers the bedtime fix the user hit (00:30 sleep_time) and the morning
brief wake-time fix (09:00 wake_time, brief was hardcoded 07:30).
"""
from __future__ import annotations

from datetime import UTC, datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.member_windows import (
    MemberWindow,
    already_fired_today,
    any_member_in_post_wake,
    any_member_in_pre_bedtime,
    in_post_wake_window,
    in_pre_bedtime_window,
    load_member_windows,
    mark_fired_today,
    today_local_key,
)

# ── Pre-bedtime window math ──────────────────────────────────────────────


def test_in_pre_bedtime_window_after_midnight_sleeper() -> None:
    """REGRESSION: 23:30 with sleep_time=00:30 must be inside the 90-min
    pre-bedtime window. The user's actual schedule."""
    saeed = MemberWindow(member_id=1, name="Saeed", sleep_time=time(0, 30), wake_time=time(9, 0))
    # 23:30 Asia/Dubai = 19:30 UTC — 60 minutes before 00:30 (next day)
    now = datetime(2026, 5, 14, 19, 30, tzinfo=UTC)
    assert in_pre_bedtime_window(saeed, minutes_before=90, now=now) is True


def test_in_pre_bedtime_window_excludes_too_early() -> None:
    saeed = MemberWindow(member_id=1, name="Saeed", sleep_time=time(0, 30), wake_time=time(9, 0))
    # 22:30 Asia/Dubai = 18:30 UTC — 120 minutes before 00:30
    now = datetime(2026, 5, 14, 18, 30, tzinfo=UTC)
    assert in_pre_bedtime_window(saeed, minutes_before=90, now=now) is False


def test_in_pre_bedtime_window_excludes_after_bedtime() -> None:
    saeed = MemberWindow(member_id=1, name="Saeed", sleep_time=time(0, 30), wake_time=time(9, 0))
    # 01:30 Asia/Dubai = 21:30 UTC — past bedtime
    now = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)
    assert in_pre_bedtime_window(saeed, minutes_before=90, now=now) is False


def test_in_pre_bedtime_window_traditional_sleeper() -> None:
    """A 23:00 sleeper at 22:00 → 60 min before bedtime → inside."""
    person = MemberWindow(member_id=2, name="Other", sleep_time=time(23, 0), wake_time=time(7, 0))
    # 22:00 Asia/Dubai = 18:00 UTC
    now = datetime(2026, 5, 14, 18, 0, tzinfo=UTC)
    assert in_pre_bedtime_window(person, minutes_before=90, now=now) is True


def test_any_member_returns_first_match() -> None:
    a = MemberWindow(1, "A", time(0, 30), time(9, 0))   # not in window at 22:00 (210min away)
    b = MemberWindow(2, "B", time(23, 0), time(7, 0))   # IS in window at 22:00 (60min before)
    now = datetime(2026, 5, 14, 18, 0, tzinfo=UTC)  # 22:00 local
    matched = any_member_in_pre_bedtime([a, b], minutes_before=90, now=now)
    assert matched is not None
    assert matched.name == "B"


# ── Post-wake window math ────────────────────────────────────────────────


def test_in_post_wake_window_morning_brief_for_late_riser() -> None:
    """REGRESSION: morning brief was sent at hardcoded 07:30 but Saeed
    wakes at 09:00 — brief never landed in the post-wake window. With
    the new helper, a 09:30 check (30 min after 09:00) IS inside."""
    saeed = MemberWindow(member_id=1, name="Saeed", sleep_time=time(0, 30), wake_time=time(9, 0))
    # 09:30 Asia/Dubai = 05:30 UTC
    now = datetime(2026, 5, 15, 5, 30, tzinfo=UTC)
    assert in_post_wake_window(saeed, minutes_after=60, now=now) is True


def test_in_post_wake_window_excludes_before_wake() -> None:
    saeed = MemberWindow(member_id=1, name="Saeed", sleep_time=time(0, 30), wake_time=time(9, 0))
    # 08:30 local = 04:30 UTC — 30 min BEFORE wake
    now = datetime(2026, 5, 15, 4, 30, tzinfo=UTC)
    assert in_post_wake_window(saeed, minutes_after=60, now=now) is False


def test_in_post_wake_window_excludes_after_window() -> None:
    saeed = MemberWindow(member_id=1, name="Saeed", sleep_time=time(0, 30), wake_time=time(9, 0))
    # 10:30 local = 06:30 UTC — 90 min after wake, outside 60-min window
    now = datetime(2026, 5, 15, 6, 30, tzinfo=UTC)
    assert in_post_wake_window(saeed, minutes_after=60, now=now) is False


def test_any_member_in_post_wake_returns_first_match() -> None:
    a = MemberWindow(1, "A", time(0, 30), time(9, 0))   # in window at 09:30
    b = MemberWindow(2, "B", time(23, 0), time(7, 0))   # NOT in window at 09:30 (way past)
    now = datetime(2026, 5, 15, 5, 30, tzinfo=UTC)  # 09:30 local
    matched = any_member_in_post_wake([a, b], minutes_after=60, now=now)
    assert matched is not None
    assert matched.name == "A"


# ── Once-per-day dedup ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_today_local_key_rolls_at_local_midnight() -> None:
    morning = datetime(2026, 5, 15, 5, 0, tzinfo=UTC)  # 09:00 local Asia/Dubai
    next_morning = datetime(2026, 5, 16, 5, 0, tzinfo=UTC)
    k1 = today_local_key("morning_brief.send", now=morning)
    k2 = today_local_key("morning_brief.send", now=next_morning)
    assert k1 != k2
    assert k1.endswith("2026-05-15")
    assert k2.endswith("2026-05-16")


@pytest.mark.asyncio
async def test_already_fired_today_dedup_round_trip() -> None:
    redis = FakeRedis(decode_responses=True)
    now = datetime(2026, 5, 15, 5, 0, tzinfo=UTC)
    assert await already_fired_today(redis, "morning_brief.send", now=now) is False
    await mark_fired_today(redis, "morning_brief.send", now=now)
    assert await already_fired_today(redis, "morning_brief.send", now=now) is True
    # Different prefix is independent
    assert await already_fired_today(redis, "prebed:1:t90", now=now) is False


# ── load_member_windows ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_member_windows_filters_pets_and_missing_sleep_time() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {"id": 1, "name": "Saeed", "sleep_time": time(0, 30), "wake_time": time(9, 0)},
            # SQL filters role='pet' and sleep_time IS NULL — this is just
            # double-checking we map valid rows correctly.
            {"id": 2, "name": "Jude", "sleep_time": time(23, 0), "wake_time": None},
        ]
    )
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    windows = await load_member_windows(pool)

    assert len(windows) == 2
    # Missing wake_time defaults to 07:00
    assert windows[1].wake_time == time(7, 0)


@pytest.mark.asyncio
async def test_load_member_windows_returns_empty_when_no_pool() -> None:
    assert await load_member_windows(None) == []


@pytest.mark.asyncio
async def test_load_member_windows_returns_empty_on_db_error() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception("db down"))
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    assert await load_member_windows(pool) == []
