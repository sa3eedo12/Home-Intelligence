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
    assert "ON CONFLICT DO NOTHING" in query
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
