"""Tests for the cross-source correlation engine.

Each correlator runs against a fake asyncpg pool that returns queued result
sets in order. Verifies both the "fires when warranted" and "stays silent
on insufficient or boring data" branches — the latter is critical because
the morning brief shouldn't surface false-positive insights like "your HR
is normal!".
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.correlations import (
    _coffee_then_hr_spike,
    _missing_morning_routine,
    _resting_hr_drift,
    _sleep_vs_late_tv,
    _step_count_trend,
    correlate_recent,
)


def _fake_pool(result_sets: list[list[dict]]) -> SimpleNamespace:
    """Build a fake asyncpg pool whose conn.fetch() returns the queued
    result sets in order. Each call pops the next set."""
    queue = list(result_sets)

    async def _fetch(*args, **kwargs):
        return queue.pop(0) if queue else []

    conn = SimpleNamespace(fetch=_fetch)

    class _Acq:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, exc_type, exc, tb):
            return False

    return SimpleNamespace(acquire=lambda: _Acq())


# ── _resting_hr_drift ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resting_hr_drift_fires_on_5bpm_increase() -> None:
    now = datetime.now(UTC)
    rows: list[dict] = []
    for i in range(10):  # 10 days old → "older" baseline at 60
        rows.append({"value": 60.0, "started_at": now - timedelta(days=10 + i)})
    for i in range(5):  # last 5 days → "recent" at 70
        rows.append({"value": 70.0, "started_at": now - timedelta(days=i)})
    pool = _fake_pool([rows])
    out = await _resting_hr_drift(pool, lookback_days=14)
    assert out is not None
    assert "up 10 bpm" in out["headline"]
    assert out["confidence"] >= 0.85  # >8 bpm → high confidence


@pytest.mark.asyncio
async def test_resting_hr_drift_silent_on_small_change() -> None:
    now = datetime.now(UTC)
    rows = (
        [{"value": 60.0, "started_at": now - timedelta(days=10 + i)} for i in range(5)]
        + [{"value": 62.0, "started_at": now - timedelta(days=i)} for i in range(5)]
    )
    pool = _fake_pool([rows])
    out = await _resting_hr_drift(pool, lookback_days=14)
    assert out is None  # 2 bpm → below 5 bpm threshold


@pytest.mark.asyncio
async def test_resting_hr_drift_silent_on_too_few_samples() -> None:
    pool = _fake_pool([[]])
    assert await _resting_hr_drift(pool, lookback_days=14) is None


# ── _sleep_vs_late_tv ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sleep_vs_late_tv_fires_when_late_tv_correlates_with_short_sleep() -> None:
    now = datetime.now(UTC)
    # 4 nights of late TV → short sleep (5 h = 300 min)
    # 4 nights without late TV → normal sleep (8 h = 480 min)
    sleep_rows: list[dict] = []
    tv_rows: list[dict] = []
    for i in range(4):
        d = now - timedelta(days=i + 1)
        sleep_rows.append({"started_at": d, "value": 300.0})  # 5 h
        tv_rows.append(
            {"night": d.replace(hour=0, minute=0, second=0, microsecond=0),
             "latest_event": time(23, 15)}
        )
    for i in range(4, 8):
        d = now - timedelta(days=i + 1)
        sleep_rows.append({"started_at": d, "value": 480.0})  # 8 h
    pool = _fake_pool([sleep_rows, tv_rows])
    out = await _sleep_vs_late_tv(pool, lookback_days=14)
    assert out is not None
    assert "less" in out["headline"]
    assert "5.0 h" in out["headline"]
    assert "8.0 h" in out["headline"]


@pytest.mark.asyncio
async def test_sleep_vs_late_tv_silent_when_no_late_tv() -> None:
    now = datetime.now(UTC)
    sleep_rows = [{"started_at": now - timedelta(days=i), "value": 480.0} for i in range(5)]
    pool = _fake_pool([sleep_rows, []])
    assert await _sleep_vs_late_tv(pool, lookback_days=14) is None


@pytest.mark.asyncio
async def test_sleep_vs_late_tv_silent_when_diff_under_20min() -> None:
    now = datetime.now(UTC)
    sleep_rows: list[dict] = []
    tv_rows: list[dict] = []
    for i in range(4):
        d = now - timedelta(days=i + 1)
        sleep_rows.append({"started_at": d, "value": 470.0})
        tv_rows.append(
            {"night": d.replace(hour=0, minute=0, second=0, microsecond=0),
             "latest_event": time(23, 15)}
        )
    for i in range(4, 8):
        d = now - timedelta(days=i + 1)
        sleep_rows.append({"started_at": d, "value": 480.0})
    pool = _fake_pool([sleep_rows, tv_rows])
    out = await _sleep_vs_late_tv(pool, lookback_days=14)
    assert out is None  # 10 min diff < 20 min threshold


# ── _coffee_then_hr_spike ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coffee_hr_spike_fires_on_consistent_post_coffee_jump() -> None:
    now = datetime.now(UTC)
    coffee_rows: list[dict] = []
    hr_rows: list[dict] = []
    for i in range(5):
        brew_ts = now - timedelta(days=i + 1, hours=8)
        coffee_rows.append({"ts": brew_ts})
        # 2 readings before at 65, 2 after at 80 → +15 bpm spike
        hr_rows.append({"started_at": brew_ts - timedelta(minutes=15), "value": 65.0})
        hr_rows.append({"started_at": brew_ts - timedelta(minutes=5), "value": 65.0})
        hr_rows.append({"started_at": brew_ts + timedelta(minutes=5), "value": 80.0})
        hr_rows.append({"started_at": brew_ts + timedelta(minutes=20), "value": 80.0})
    pool = _fake_pool([coffee_rows, hr_rows])
    out = await _coffee_then_hr_spike(pool, lookback_days=14)
    assert out is not None
    assert "+15 bpm" in out["headline"]


@pytest.mark.asyncio
async def test_coffee_hr_spike_silent_when_no_coffee() -> None:
    pool = _fake_pool([[]])
    assert await _coffee_then_hr_spike(pool, lookback_days=14) is None


@pytest.mark.asyncio
async def test_coffee_hr_spike_silent_when_no_hr() -> None:
    pool = _fake_pool([[{"ts": datetime.now(UTC)}], []])
    assert await _coffee_then_hr_spike(pool, lookback_days=14) is None


# ── _step_count_trend ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_count_trend_fires_on_30pct_drop() -> None:
    now = datetime.now(UTC)
    rows: list[dict] = []
    # Older 7 days at 10 000 steps
    for i in range(7):
        rows.append({"day": now - timedelta(days=8 + i), "total": 10000.0})
    # Recent 5 days at 6 000 steps (-40%)
    for i in range(5):
        rows.append({"day": now - timedelta(days=i + 1), "total": 6000.0})
    pool = _fake_pool([rows])
    out = await _step_count_trend(pool, lookback_days=14)
    assert out is not None
    assert "↓" in out["headline"]
    assert "40%" in out["headline"]


@pytest.mark.asyncio
async def test_step_count_trend_silent_on_small_change() -> None:
    now = datetime.now(UTC)
    rows = [{"day": now - timedelta(days=i + 1), "total": 10000.0} for i in range(10)]
    pool = _fake_pool([rows])
    assert await _step_count_trend(pool, lookback_days=14) is None


# ── _missing_morning_routine ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wake_drift_fires_when_today_is_much_later() -> None:
    # Usual wake ~07:00, today wake at 09:30 → 150 min late
    rows: list[dict] = [
        {"ended_at": datetime(2026, 5, 14, 9, 30, tzinfo=UTC)},  # today
        {"ended_at": datetime(2026, 5, 13, 7, 5, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 12, 6, 55, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 11, 7, 0, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 10, 7, 10, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 9, 6, 50, tzinfo=UTC)},
    ]
    pool = _fake_pool([rows])
    out = await _missing_morning_routine(pool, lookback_days=14)
    assert out is not None
    assert "later" in out["headline"]


@pytest.mark.asyncio
async def test_wake_drift_silent_when_today_is_normal() -> None:
    rows: list[dict] = [
        {"ended_at": datetime(2026, 5, 14, 7, 5, tzinfo=UTC)},  # today, on time
        {"ended_at": datetime(2026, 5, 13, 7, 5, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 12, 6, 55, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 11, 7, 0, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 10, 7, 10, tzinfo=UTC)},
        {"ended_at": datetime(2026, 5, 9, 6, 50, tzinfo=UTC)},
    ]
    pool = _fake_pool([rows])
    assert await _missing_morning_routine(pool, lookback_days=14) is None


# ── correlate_recent (orchestrator) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_correlate_recent_swallows_individual_failures() -> None:
    """If one correlator throws, the others should still produce results."""

    class BoomPool:
        def acquire(self_inner):
            raise RuntimeError("db down")

    out = await correlate_recent(BoomPool(), lookback_days=14)
    assert out == []  # all failed, but no exception bubbled out


@pytest.mark.asyncio
async def test_correlate_recent_returns_empty_when_no_data() -> None:
    # All five correlators see empty result sets → return []
    pool = _fake_pool([[]] * 20)
    out = await correlate_recent(pool, lookback_days=14)
    assert out == []
