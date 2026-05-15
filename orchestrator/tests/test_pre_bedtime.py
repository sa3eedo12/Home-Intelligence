"""Tests for orchestrator.pre_bedtime — tier'd pre-bedtime wind-down nudge."""
from __future__ import annotations

import json
from datetime import UTC, datetime, time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.pre_bedtime import (
    _compose_message,
    _matched_tier,
    _minutes_until,
    scan_pre_bedtime,
)

# ── Pure helpers ─────────────────────────────────────────────────────────


def test_minutes_until_handles_after_midnight_target() -> None:
    """At 23:30 local with target=00:30 → 60 min until target."""
    # 23:30 Asia/Dubai = 19:30 UTC
    now = datetime(2026, 5, 14, 19, 30, tzinfo=UTC)
    assert _minutes_until(time(0, 30), now=now) == 60


def test_minutes_until_handles_same_day_target() -> None:
    """At 22:00 with target=23:00 → 60 min until target."""
    now = datetime(2026, 5, 14, 18, 0, tzinfo=UTC)  # 22:00 local
    assert _minutes_until(time(23, 0), now=now) == 60


def test_minutes_until_returns_tomorrow_for_passed_target() -> None:
    """At 09:00 with target=00:30 → ~15h30m (target is tomorrow)."""
    now = datetime(2026, 5, 15, 5, 0, tzinfo=UTC)  # 09:00 local
    minutes = _minutes_until(time(0, 30), now=now)
    assert minutes == (15 * 60 + 30)


def test_matched_tier_t90_at_60min_before_bedtime() -> None:
    """60min before 00:30 sleep_time = 23:30 → matches the 90-min tier."""
    from orchestrator.member_windows import MemberWindow

    saeed = MemberWindow(1, "Saeed", time(0, 30), time(9, 0))
    now = datetime(2026, 5, 14, 19, 30, tzinfo=UTC)  # 23:30 local
    assert _matched_tier(saeed, now=now) == 90


def test_matched_tier_t30_at_15min_before_bedtime() -> None:
    """15min before 00:30 → matches the 30-min tier (not 90)."""
    from orchestrator.member_windows import MemberWindow

    saeed = MemberWindow(1, "Saeed", time(0, 30), time(9, 0))
    now = datetime(2026, 5, 14, 20, 15, tzinfo=UTC)  # 00:15 local
    assert _matched_tier(saeed, now=now) == 30


def test_matched_tier_returns_none_outside_all_tiers() -> None:
    """At 15:00 there's no tier active for a 00:30 bedtime."""
    from orchestrator.member_windows import MemberWindow

    saeed = MemberWindow(1, "Saeed", time(0, 30), time(9, 0))
    now = datetime(2026, 5, 14, 11, 0, tzinfo=UTC)  # 15:00 local
    assert _matched_tier(saeed, now=now) is None


def test_compose_message_returns_none_when_nothing_on() -> None:
    assert (
        _compose_message(minutes_left=30, light_count=0, tv_count=0, light_examples=[])
        is None
    )


def test_compose_message_with_lights_only() -> None:
    text = _compose_message(
        minutes_left=15,
        light_count=4,
        tv_count=0,
        light_examples=["Living Room", "Kitchen", "Hallway", "Office"],
    )
    assert text is not None
    assert "4 lights are still on" in text
    assert "Living Room" in text
    assert "Bedtime in" in text


def test_compose_message_with_tv_only() -> None:
    text = _compose_message(minutes_left=60, light_count=0, tv_count=1, light_examples=[])
    assert text is not None
    assert "1 TV playing" in text
    assert "until bedtime" in text


def test_compose_message_with_both() -> None:
    text = _compose_message(
        minutes_left=20,
        light_count=2,
        tv_count=2,
        light_examples=["Lamp A", "Lamp B"],
    )
    assert text is not None
    assert "2 lights on" in text
    assert "2 TVs playing" in text


# ── End-to-end scan ──────────────────────────────────────────────────────


def _member_pool(rows: list[dict[str, Any]]):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.fetchval = AsyncMock(return_value=0)
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _lights_observer(count: int, names: list[str] | None = None):
    obs = MagicMock()
    obs.snapshot = MagicMock(
        return_value={
            "count": count,
            "lights": [
                {"friendly_name": n, "entity_id": f"light.{n.lower()}"}
                for n in (names or [])
            ],
        }
    )
    return obs


@pytest.mark.asyncio
async def test_scan_emits_when_in_window_with_lights_on() -> None:
    """At 23:30 local with 5 lights on, sleep_time=00:30 → emit a 90-min nudge."""
    pool = _member_pool(
        [{"id": 1, "name": "Saeed", "sleep_time": time(0, 30), "wake_time": time(9, 0)}]
    )
    redis = FakeRedis(decode_responses=True)
    reflection_store = AsyncMock()
    reflection_store.add_proposal = AsyncMock(return_value=99)
    lights = _lights_observer(5, ["Living Room", "Kitchen", "Hallway", "Office", "Garage"])

    now = datetime(2026, 5, 14, 19, 30, tzinfo=UTC)  # 23:30 local
    result = await scan_pre_bedtime(
        reflection_store=reflection_store,
        redis=redis,
        pool=pool,
        lights_observer=lights,
        now=now,
    )

    assert result["emitted"] == 1
    proposal = result["proposals"][0]
    assert proposal["tier"] == 90
    assert proposal["proposal_id"] == 99
    assert "5 lights are still on" in proposal["title"]
    reflection_store.add_proposal.assert_awaited_once()
    # Notification went out
    rows = await redis.xrange("notify.outbound")
    assert len(rows) == 1
    payload = json.loads(rows[0][1]["payload"])
    assert payload["topic"] == "sleep.pre_bedtime"
    assert payload["severity"] == "notice"


@pytest.mark.asyncio
async def test_scan_does_not_re_emit_within_same_tier_same_day() -> None:
    """Scheduler runs every 15 min — within one tier window we should emit
    AT MOST once per day. Second call inside the same tier no-ops."""
    pool = _member_pool(
        [{"id": 1, "name": "Saeed", "sleep_time": time(0, 30), "wake_time": time(9, 0)}]
    )
    redis = FakeRedis(decode_responses=True)
    reflection_store = AsyncMock()
    reflection_store.add_proposal = AsyncMock(return_value=99)
    lights = _lights_observer(5, ["A", "B", "C", "D", "E"])

    now1 = datetime(2026, 5, 14, 19, 30, tzinfo=UTC)
    now2 = datetime(2026, 5, 14, 19, 45, tzinfo=UTC)  # 15 min later, still in 90-tier
    r1 = await scan_pre_bedtime(
        reflection_store=reflection_store, redis=redis, pool=pool,
        lights_observer=lights, now=now1,
    )
    r2 = await scan_pre_bedtime(
        reflection_store=reflection_store, redis=redis, pool=pool,
        lights_observer=lights, now=now2,
    )

    assert r1["emitted"] == 1
    assert r2["emitted"] == 0
    reflection_store.add_proposal.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_skips_when_house_is_already_winding_down() -> None:
    """If lights+TV count is 0 we shouldn't pester — but also don't
    keep re-checking every 15 min within the same tier."""
    pool = _member_pool(
        [{"id": 1, "name": "Saeed", "sleep_time": time(0, 30), "wake_time": time(9, 0)}]
    )
    redis = FakeRedis(decode_responses=True)
    reflection_store = AsyncMock()
    lights = _lights_observer(0)

    now = datetime(2026, 5, 14, 19, 30, tzinfo=UTC)
    result = await scan_pre_bedtime(
        reflection_store=reflection_store, redis=redis, pool=pool,
        lights_observer=lights, now=now,
    )

    assert result["emitted"] == 0
    reflection_store.add_proposal.assert_not_awaited()
    # But the dedup flag was set so we don't re-check this tier today
    keys = await redis.keys("prebed:1:t90:*")
    assert len(keys) == 1


@pytest.mark.asyncio
async def test_scan_skips_outside_all_tiers() -> None:
    """At 15:00 local there's no tier active for a 00:30 sleeper."""
    pool = _member_pool(
        [{"id": 1, "name": "Saeed", "sleep_time": time(0, 30), "wake_time": time(9, 0)}]
    )
    redis = FakeRedis(decode_responses=True)
    reflection_store = AsyncMock()
    lights = _lights_observer(5, ["A", "B", "C", "D", "E"])

    now = datetime(2026, 5, 14, 11, 0, tzinfo=UTC)  # 15:00 local
    result = await scan_pre_bedtime(
        reflection_store=reflection_store, redis=redis, pool=pool,
        lights_observer=lights, now=now,
    )

    assert result["emitted"] == 0
    reflection_store.add_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_returns_no_member_windows_when_household_empty() -> None:
    pool = _member_pool([])
    redis = FakeRedis(decode_responses=True)
    reflection_store = AsyncMock()

    result = await scan_pre_bedtime(
        reflection_store=reflection_store, redis=redis, pool=pool,
        lights_observer=None,
        now=datetime(2026, 5, 14, 19, 30, tzinfo=UTC),
    )

    assert result["emitted"] == 0
    assert result["skipped"] == "no_member_windows"
