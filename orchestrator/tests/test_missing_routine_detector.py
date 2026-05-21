"""Tests for missing_routine_detector."""
from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator.missing_routine_detector import (
    detect_missing_followups,
    detect_missing_habits,
    detect_missing_routines,
)


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    return pool


@pytest.fixture
def frozen_morning() -> datetime:
    # Friday 2026-05-22 at 10:00 local (Dubai). Past 07:00-08:30 coffee.
    return datetime(2026, 5, 22, 10, 0, tzinfo=ZoneInfo("Asia/Dubai"))


def _patch_now(target: str, value: datetime):
    return patch(target, autospec=True, side_effect=lambda *_a, **_k: value)


# ── detect_missing_habits ───────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_habit_emits_when_expected_window_passed_without_event(
    frozen_morning: datetime,
) -> None:
    """Coffee habit expected mon-fri 07:00-08:30, today is Friday 10:00,
    no coffee event today → emit one missing_habit anomaly."""
    conn = MagicMock()
    # Two fetches per habit: one for the habit list, one for "did it fire"
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 12,
            "subject": "home_automation.coffee_started",
            "pattern": json.dumps({
                "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                "time_window_local": "07:00-08:30",
            }),
            "confidence": 0.82,
            "last_observed_at": datetime(2026, 5, 18, tzinfo=UTC),
        }
    ])
    conn.fetchval = AsyncMock(return_value=None)  # no coffee fired today

    with patch(
        "orchestrator.missing_routine_detector.datetime"
    ) as dt_mock:
        dt_mock.now.return_value = frozen_morning
        dt_mock.combine.side_effect = datetime.combine
        out = await detect_missing_habits(
            _pool_with(conn), user_tz_name="Asia/Dubai", grace_minutes=30
        )
    assert len(out) == 1
    a = out[0]
    assert a.kind == "missing_habit"
    assert "coffee_started" in a.summary
    assert a.payload["subject"] == "home_automation.coffee_started"
    assert a.payload["expected_window"] == "07:00-08:30"
    # Cooldown key includes the date so we re-detect per-day.
    assert "2026-05-22" in a.payload["anomaly_type"]


@pytest.mark.asyncio
async def test_missing_habit_skips_when_not_expected_today(
    frozen_morning: datetime,
) -> None:
    """Weekend-only habit on a Friday → no anomaly."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 13,
            "subject": "home_automation.brunch",
            "pattern": json.dumps({
                "days_of_week": ["sat", "sun"],
                "time_window_local": "10:00-12:00",
            }),
            "confidence": 0.9,
            "last_observed_at": datetime(2026, 5, 18, tzinfo=UTC),
        }
    ])
    conn.fetchval = AsyncMock(return_value=None)
    with patch("orchestrator.missing_routine_detector.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_morning
        dt_mock.combine.side_effect = datetime.combine
        out = await detect_missing_habits(_pool_with(conn))
    assert out == []


@pytest.mark.asyncio
async def test_missing_habit_skips_when_window_still_open(
    frozen_morning: datetime,
) -> None:
    """If we're still inside the expected window we don't claim 'missing' yet."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 14,
            "subject": "home_automation.lunch",
            "pattern": json.dumps({
                "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                "time_window_local": "11:30-13:00",  # 10:00 is before 11:30
            }),
            "confidence": 0.7,
            "last_observed_at": datetime(2026, 5, 18, tzinfo=UTC),
        }
    ])
    conn.fetchval = AsyncMock(return_value=None)
    with patch("orchestrator.missing_routine_detector.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_morning
        dt_mock.combine.side_effect = datetime.combine
        out = await detect_missing_habits(_pool_with(conn))
    assert out == []


@pytest.mark.asyncio
async def test_missing_habit_skips_when_event_already_fired(
    frozen_morning: datetime,
) -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 15,
            "subject": "home_automation.coffee_started",
            "pattern": json.dumps({
                "days_of_week": ["fri"],
                "time_window_local": "07:00-08:30",
            }),
            "confidence": 0.85,
            "last_observed_at": datetime(2026, 5, 18, tzinfo=UTC),
        }
    ])
    conn.fetchval = AsyncMock(return_value=1)  # fired today
    with patch("orchestrator.missing_routine_detector.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_morning
        dt_mock.combine.side_effect = datetime.combine
        out = await detect_missing_habits(_pool_with(conn))
    assert out == []


@pytest.mark.asyncio
async def test_missing_habit_no_pool() -> None:
    assert await detect_missing_habits(None) == []


# ── detect_missing_followups ────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_followup_emits_when_b_didnt_follow_a() -> None:
    """washer.cycle_complete fired 45 min ago, window=30min, no dryer.start
    in those 30min → emit missing_followup."""
    a_ts = datetime.now(UTC) - timedelta(minutes=45)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 7,
            "name": "washer.cycle_complete -> dryer.start",
            "steps": json.dumps({
                "steps": [
                    {"trigger": "washer.cycle_complete"},
                    {"action": "dryer.start"},
                ],
                "attributes": {
                    "confidence": 0.85,
                    "window_minutes": 30,
                },
            }),
        }
    ])

    fetchrow_calls: list[dict] = []

    async def fetchrow(query: str, *args: Any) -> dict | None:
        fetchrow_calls.append({"query": query, "args": args})
        if "ORDER BY ts DESC" in query:
            return {"id": 99, "ts": a_ts}  # A fired 45min ago
        # B lookup → not found
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)

    out = await detect_missing_followups(_pool_with(conn))
    assert len(out) == 1
    a = out[0]
    assert a.kind == "missing_followup"
    assert a.payload["trigger_subject"] == "washer.cycle_complete"
    assert a.payload["expected_followup"] == "dryer.start"
    assert a.payload["window_minutes"] == 30
    assert a.payload["confidence"] == 0.85
    # Two fetchrows: one for A, one for B.
    assert len(fetchrow_calls) == 2


@pytest.mark.asyncio
async def test_missing_followup_skips_when_b_did_follow() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 7,
            "name": "washer.cycle_complete -> dryer.start",
            "steps": json.dumps({
                "steps": [
                    {"trigger": "washer.cycle_complete"},
                    {"action": "dryer.start"},
                ],
                "attributes": {"confidence": 0.85, "window_minutes": 30},
            }),
        }
    ])
    a_ts = datetime.now(UTC) - timedelta(minutes=45)

    async def fetchrow(query: str, *args: Any) -> dict | None:
        if "ORDER BY ts DESC" in query:
            return {"id": 99, "ts": a_ts}
        return {"present": 1}  # B did follow

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    out = await detect_missing_followups(_pool_with(conn))
    assert out == []


@pytest.mark.asyncio
async def test_missing_followup_skips_low_confidence_routine() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 7,
            "name": "x -> y",
            "steps": json.dumps({
                "steps": [{"trigger": "x.a"}, {"action": "y.b"}],
                "attributes": {"confidence": 0.40, "window_minutes": 30},
            }),
        }
    ])
    conn.fetchrow = AsyncMock(return_value=None)
    out = await detect_missing_followups(_pool_with(conn), confidence_floor=0.60)
    assert out == []
    # Bailed out before querying event_log.
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_missing_followup_skips_when_a_never_fired_recently() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 7,
            "name": "x -> y",
            "steps": json.dumps({
                "steps": [{"trigger": "x.a"}, {"action": "y.b"}],
                "attributes": {"confidence": 0.85, "window_minutes": 30},
            }),
        }
    ])
    conn.fetchrow = AsyncMock(return_value=None)  # no A
    out = await detect_missing_followups(_pool_with(conn))
    assert out == []


@pytest.mark.asyncio
async def test_missing_followup_no_pool() -> None:
    assert await detect_missing_followups(None) == []


# ── orchestration wrapper ───────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_missing_routines_runs_both_branches_resiliently() -> None:
    """If detect_missing_habits raises, detect_missing_followups should
    still get called, and vice versa."""
    with patch(
        "orchestrator.missing_routine_detector.detect_missing_habits",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ), patch(
        "orchestrator.missing_routine_detector.detect_missing_followups",
        new=AsyncMock(return_value=[]),
    ) as followups:
        out = await detect_missing_routines(MagicMock())
    followups.assert_awaited_once()
    assert out == []
