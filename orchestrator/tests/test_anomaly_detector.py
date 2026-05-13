from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.anomaly_detector import (
    Anomaly,
    _detect_bedtime_overrun,
    _detect_coffee_skipped,
    _detect_sleep_missing,
    _detect_vacuum_overdue,
    _detect_washer_overdue,
    detect_anomalies,
    emit_anomalies,
)


def _fake_pool(rows_per_query: list[dict]) -> SimpleNamespace:
    """Build a fake asyncpg pool that returns the queued rows in order."""
    queue = list(rows_per_query)

    async def _fetchrow(*args, **kwargs):
        return queue.pop(0) if queue else None

    conn = SimpleNamespace(fetchrow=_fetchrow)

    class _Acq:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, exc_type, exc, tb):
            return False

    pool = SimpleNamespace(acquire=lambda: _Acq())
    return pool


@pytest.mark.asyncio
async def test_vacuum_overdue_fires_after_7_days() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    last_seen = now - timedelta(days=10)
    pool = _fake_pool([{"last_seen": last_seen}])
    a = await _detect_vacuum_overdue(pool, now=now)
    assert a is not None
    assert "vacuum" in a.summary.lower()
    assert a.payload["days_since"] == 10


@pytest.mark.asyncio
async def test_vacuum_overdue_silent_when_recent() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    last_seen = now - timedelta(days=2)
    pool = _fake_pool([{"last_seen": last_seen}])
    a = await _detect_vacuum_overdue(pool, now=now)
    assert a is None


@pytest.mark.asyncio
async def test_vacuum_overdue_silent_when_never_seen() -> None:
    pool = _fake_pool([{"last_seen": None}])
    a = await _detect_vacuum_overdue(pool, now=datetime(2026, 5, 14, tzinfo=UTC))
    assert a is None


@pytest.mark.asyncio
async def test_washer_overdue_uses_14_day_threshold() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    pool = _fake_pool([{"last_seen": now - timedelta(days=15)}])
    a = await _detect_washer_overdue(pool, now=now)
    assert a is not None
    pool = _fake_pool([{"last_seen": now - timedelta(days=10)}])
    a = await _detect_washer_overdue(pool, now=now)
    assert a is None


@pytest.mark.asyncio
async def test_sleep_missing_fires_after_2_nights() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    pool = _fake_pool([{"last_night": (now - timedelta(days=3)).date()}])
    a = await _detect_sleep_missing(pool, now=now)
    assert a is not None
    pool = _fake_pool([{"last_night": now.date()}])
    a = await _detect_sleep_missing(pool, now=now)
    assert a is None


@pytest.mark.asyncio
async def test_sleep_missing_silent_when_no_summaries_ever() -> None:
    pool = _fake_pool([{"last_night": None}])
    a = await _detect_sleep_missing(pool, now=datetime(2026, 5, 14, tzinfo=UTC))
    assert a is None


@pytest.mark.asyncio
async def test_coffee_skipped_fires_on_weekday_morning_with_history() -> None:
    # 2026-05-14 = Thursday weekday. 12:00 UTC = 16:00 Dubai → past 11am → check
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    # Two queries: count today (returns 0) + last_brew (returns a recent one)
    pool = _fake_pool([
        {"n": 0},
        {"last_brew": now - timedelta(days=1)},
    ])
    a = await _detect_coffee_skipped(pool, now=now, user_tz_name="Asia/Dubai")
    assert a is not None
    assert "coffee" in a.summary.lower()


@pytest.mark.asyncio
async def test_coffee_skipped_silent_on_weekend() -> None:
    # 2026-05-16 = Saturday
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    pool = _fake_pool([{"n": 0}])
    a = await _detect_coffee_skipped(pool, now=now, user_tz_name="Asia/Dubai")
    assert a is None


@pytest.mark.asyncio
async def test_coffee_skipped_silent_when_never_brewed() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    pool = _fake_pool([
        {"n": 0},
        {"last_brew": None},  # never brewed → don't notify (no baseline)
    ])
    a = await _detect_coffee_skipped(pool, now=now, user_tz_name="Asia/Dubai")
    assert a is None


@pytest.mark.asyncio
async def test_bedtime_overrun_fires_when_past_typical_plus_60() -> None:
    # User sleep_time = 22:00 → grace 23:00 → "now" 00:30 local = 20:30 UTC
    # would be in the typical window. Use 21:30 UTC = 01:30 Dubai for the test.
    now = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)
    pool = _fake_pool([
        {"id": 1, "name": "Saeed", "sleep_time": time(22, 0)},
        {"n": 0},  # no recent sleep.likely_asleep
    ])
    a = await _detect_bedtime_overrun(pool, now=now, user_tz_name="Asia/Dubai")
    assert a is not None
    assert "bedtime" in a.summary.lower()


@pytest.mark.asyncio
async def test_bedtime_overrun_silent_if_already_asleep() -> None:
    now = datetime(2026, 5, 14, 21, 30, tzinfo=UTC)
    pool = _fake_pool([
        {"id": 1, "name": "Saeed", "sleep_time": time(22, 0)},
        {"n": 1},  # already detected sleep.likely_asleep
    ])
    a = await _detect_bedtime_overrun(pool, now=now, user_tz_name="Asia/Dubai")
    assert a is None


@pytest.mark.asyncio
async def test_emit_anomalies_respects_cooldown() -> None:
    redis_calls = []

    async def _xadd(stream, fields, **kwargs):
        redis_calls.append(("xadd", stream))

    state = {"vacuum_overdue": False}

    async def _get(key):
        return "1" if state.get(key.split(":", 1)[1], False) else None

    async def _set(key, val, ex=None):
        state[key.split(":", 1)[1]] = True

    redis = SimpleNamespace(xadd=_xadd, get=_get, set=_set)
    a = Anomaly(
        kind="anomaly.detected",
        summary="🧹 Vacuum 8 days",
        severity="notice",
        payload={"anomaly_type": "vacuum_overdue", "days_since": 8},
    )
    sent = await emit_anomalies(anomalies=[a], redis=redis)
    assert sent == 1
    assert state["vacuum_overdue"] is True

    # Second call: should be deduped
    redis_calls.clear()
    sent2 = await emit_anomalies(anomalies=[a], redis=redis)
    assert sent2 == 0
    assert redis_calls == []


@pytest.mark.asyncio
async def test_detect_anomalies_handles_none_pool() -> None:
    result = await detect_anomalies(pool=None)
    assert result == []
