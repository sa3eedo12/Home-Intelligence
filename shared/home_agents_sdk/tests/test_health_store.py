from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.health_store import HealthStore


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


@pytest.mark.asyncio
async def test_upsert_metrics_bulk_inserts_and_counts_skips() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=2)
    store = HealthStore(_pool_with(conn))

    result = await store.upsert_metrics(
        [
            {"metric": "steps", "started_at": "2026-05-13T08:00:00Z", "value": 100},
            {"metric": "steps", "started_at": "2026-05-13T09:00:00Z", "value": 120},
            {"metric": "steps"},
        ]
    )

    assert result == {"inserted": 2, "skipped": 1}
    query, payload = conn.fetchval.await_args.args
    # Closes the "34h 35min" duplicate-sleep bug: each (metric, started_at,
    # source) tuple is now an UPSERT keyed on session, with a value-grew
    # guard so older snapshots don't clobber newer ones.
    assert "ON CONFLICT (metric, started_at, source) DO UPDATE" in query
    assert "EXCLUDED.value >= health_metrics.value" in query
    assert "health_metrics" in query
    assert "2026-05-13T08:00:00Z" in payload


@pytest.mark.asyncio
async def test_list_recent_formats_rows() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 1,
                "metric": "heart_rate",
                "started_at": datetime(2026, 5, 13, 8, tzinfo=UTC),
                "ended_at": None,
                "value": 62.0,
                "unit": "bpm",
                "source": "health_auto_export",
                "member_id": 7,
                "metadata": '{"device":"watch"}',
                "raw": {},
                "received_at": datetime(2026, 5, 13, 8, 1, tzinfo=UTC),
            }
        ]
    )
    store = HealthStore(_pool_with(conn))

    rows = await store.list_recent(metric="heart_rate", hours=12)

    assert rows[0]["metadata"] == {"device": "watch"}
    assert rows[0]["started_at"].startswith("2026-05-13T08:00:00")
    assert conn.fetch.await_args.args[-2:] == (12, "heart_rate")


@pytest.mark.asyncio
async def test_aggregate_daily_marks_sum_metrics() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "day": date(2026, 5, 13),
                "count": 3,
                "sum": 12000.0,
                "avg": 4000.0,
                "min": 1000.0,
                "max": 6000.0,
                "unit": "steps",
            }
        ]
    )
    store = HealthStore(_pool_with(conn))

    rows = await store.aggregate_daily("steps", days=7)

    assert rows == [
        {
            "metric": "steps",
            "aggregation": "sum",
            "value": 12000.0,
            "day": "2026-05-13",
            "count": 3,
            "sum": 12000.0,
            "avg": 4000.0,
            "min": 1000.0,
            "max": 6000.0,
            "unit": "steps",
        }
    ]
    assert conn.fetch.await_args.args[-2:] == ("steps", 7)


@pytest.mark.asyncio
async def test_latest_returns_none_when_missing() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    store = HealthStore(_pool_with(conn))

    assert await store.latest("weight") is None
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_pool_is_graceful() -> None:
    store = HealthStore(None)

    assert await store.upsert_metrics([{"metric": "steps", "started_at": "now"}]) == {
        "inserted": 0,
        "skipped": 1,
    }
    assert await store.list_recent() == []
    assert await store.aggregate_daily("steps") == []
    assert await store.latest("steps") is None
    assert await store.summary() == {"total_metrics": 0, "last_received_at": None}


@pytest.mark.asyncio
async def test_aggregate_daily_query_uses_dedup_cte() -> None:
    """REGRESSION: Apple Health sent 3 sleep_asleep rows for one night
    (partial 02:21→04:40 + full 02:21→09:25 + late 06:01→09:25), each
    a snapshot of the SAME sleep. A naive sum reported 17h. The
    aggregate query must use a CTE that dedups by (started_at, ended_at)
    taking max(value) BEFORE summing per day."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = HealthStore(_pool_with(conn))

    await store.aggregate_daily("sleep_asleep", days=7)

    query = conn.fetch.await_args.args[0]
    # CTE name + deduplication semantics must be present
    assert "WITH deduped" in query
    assert "max(value)" in query
    assert "GROUP BY day, started_at, ended_at" in query
    # Session-metric path uses a row_number ranking to pick ONE row
    # per day rather than summing overlapping intervals.
    assert "row_number()" in query
    assert "ranked" in query


@pytest.mark.asyncio
async def test_aggregate_daily_session_metric_picks_max_value_per_day() -> None:
    """REGRESSION: feeding the user's actual three overlapping rows
    (138.95, 423.883, 203.95) — all snapshots of one 7h sleep — should
    return 423.883 (the truthful max-coverage row), NOT their sum (12.8h)
    or two of them summed (10.5h)."""
    conn = MagicMock()
    # Postgres after dedup CTE + row_number ranking returns one row per
    # day with the winning interval's value
    conn.fetch = AsyncMock(
        return_value=[
            {
                "day": date(2026, 5, 15),
                "count": 1,
                "sum": 423.883,
                "avg": 423.883,
                "min": 423.883,
                "max": 423.883,
                "unit": "min",
            }
        ]
    )
    store = HealthStore(_pool_with(conn))

    rows = await store.aggregate_daily("sleep_asleep", days=7)

    # 423.883 min = 7.06h — the actual sleep total
    assert rows[0]["sum"] == 423.883
    assert rows[0]["sum"] / 60 < 8  # certainly not 17h
    assert rows[0]["count"] == 1


@pytest.mark.asyncio
async def test_aggregate_daily_steps_still_sums_all_intervals() -> None:
    """Cumulative metrics like steps must KEEP summing across the day —
    the per-day rank-by-value picks one interval but step intervals
    don't overlap so the existing per-day single-row contract degrades
    to the right value. Use the non-session code path."""
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "day": date(2026, 5, 13),
                "count": 24,
                "sum": 8500.0,
                "avg": 354.16,
                "min": 0.0,
                "max": 1200.0,
                "unit": "count",
            }
        ]
    )
    store = HealthStore(_pool_with(conn))

    await store.aggregate_daily("steps", days=7)
    query = conn.fetch.await_args.args[0]
    # Non-session metrics still SUM across distinct intervals
    assert "sum(value)" in query
    # And do NOT use the row_number ranking
    assert "row_number()" not in query
