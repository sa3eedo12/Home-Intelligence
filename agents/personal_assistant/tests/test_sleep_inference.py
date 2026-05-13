from __future__ import annotations

from datetime import UTC, date, datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest
from home_agents_sdk.sleep_summaries_store import SleepSummariesStore

from tools import sleep_inference as sleep


class _Acquire:
    def __init__(self, conn: MagicMock) -> None:
        self.conn = conn

    async def __aenter__(self) -> MagicMock:
        return self.conn

    async def __aexit__(self, *_args) -> None:  # noqa: ANN002
        return None


def _pool_with(conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(conn))
    return pool


def test_quality_classification_uses_duration_deep_sleep_and_restlessness() -> None:
    assert sleep._classify_quality(
        duration_minutes=299,
        deep_sleep_minutes=90,
        interruptions=0,
        observer_delta_minutes=0,
        typical_sleep_delta_minutes=0,
        typical_wake_delta_minutes=0,
        crossed_midnight=True,
    )[0] == "short"
    assert sleep._classify_quality(
        duration_minutes=435,
        deep_sleep_minutes=75,
        interruptions=0,
        observer_delta_minutes=5,
        typical_sleep_delta_minutes=10,
        typical_wake_delta_minutes=10,
        crossed_midnight=True,
    )[0] == "great"
    assert sleep._classify_quality(
        duration_minutes=390,
        deep_sleep_minutes=None,
        interruptions=0,
        observer_delta_minutes=5,
        typical_sleep_delta_minutes=10,
        typical_wake_delta_minutes=10,
        crossed_midnight=True,
    )[0] == "decent"
    assert sleep._classify_quality(
        duration_minutes=460,
        deep_sleep_minutes=70,
        interruptions=3,
        observer_delta_minutes=5,
        typical_sleep_delta_minutes=10,
        typical_wake_delta_minutes=10,
        crossed_midnight=True,
    )[0] == "restless"


def test_keyboard_layout_puts_guess_first_and_uses_sleep_callbacks() -> None:
    keyboard = sleep._keyboard_for(42, "decent")

    assert keyboard[0][0] == {"text": "✅ Decent", "callback": "sleep:42:decent"}
    callbacks = [button["callback"] for row in keyboard for button in row]
    assert "sleep:42:great" in callbacks
    assert "sleep:42:restless" in callbacks
    assert callbacks[-1] == "sleep:42:_skip"


@pytest.mark.asyncio
async def test_infer_sleep_summary_persists_health_and_observer_summary(monkeypatch) -> None:
    monkeypatch.setenv("USER_TZ", "UTC")
    pool = MagicMock()

    async def _fake_pool() -> MagicMock:
        return pool

    async def _fake_member(_pool, member_id=None):  # noqa: ANN001
        assert member_id is None
        return {"id": 7, "sleep_time": time(23, 0), "wake_time": time(7, 0)}

    async def _fake_observer_events(_pool, _start, _end):  # noqa: ANN001
        return [
            {
                "capability": "sleep.likely_asleep",
                "ts": datetime(2026, 5, 12, 22, 58, tzinfo=UTC),
                "payload": {"detected_at": "2026-05-12T22:58:00+00:00"},
            },
            {
                "capability": "sleep.likely_awake",
                "ts": datetime(2026, 5, 13, 7, 12, tzinfo=UTC),
                "payload": {"detected_at": "2026-05-13T07:12:00+00:00"},
            },
        ]

    rows_by_metric = {
        "sleep_core": [
            {
                "metric": "sleep_core",
                "started_at": "2026-05-12T23:00:00+00:00",
                "ended_at": "2026-05-13T06:00:00+00:00",
                "value": 420,
                "member_id": 7,
            }
        ],
        "sleep_deep": [
            {
                "metric": "sleep_deep",
                "started_at": "2026-05-13T06:00:00+00:00",
                "ended_at": "2026-05-13T07:10:00+00:00",
                "value": 70,
                "member_id": 7,
            }
        ],
    }

    class _FakeHealthStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            pass

        async def list_recent(self, metric: str | None = None, hours: int = 24):
            assert hours >= 36
            return rows_by_metric.get(metric or "", [])

    stores = []

    class _FakeSummaryStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            self.inserted = None
            stores.append(self)

        async def insert_summary(self, **kwargs):  # noqa: ANN003
            self.inserted = kwargs
            return 88

    monkeypatch.setattr(sleep, "_pool", _fake_pool)
    monkeypatch.setattr(sleep, "_member", _fake_member)
    monkeypatch.setattr(sleep, "_observer_events", _fake_observer_events)
    monkeypatch.setattr(sleep, "HealthStore", _FakeHealthStore)
    monkeypatch.setattr(sleep, "SleepSummariesStore", _FakeSummaryStore)

    result = await sleep.infer_sleep_summary(night_of="2026-05-12")

    assert result["ok"] is True
    assert result["quality"] == "great"
    assert result["sleep_summary_id"] == 88
    assert "~8h 10m" in result["summary"]
    assert result["keyboard"][0][0]["callback"] == "sleep:88:great"
    inserted = stores[0].inserted
    assert inserted["household_member_id"] == 7
    assert inserted["night_of"] == date(2026, 5, 12)
    assert inserted["duration_minutes"] == 490
    assert inserted["deep_sleep_minutes"] == 70
    assert inserted["observer_likely_awake_at"] == datetime(2026, 5, 13, 7, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sleep_summaries_store_insert_uses_upsert() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 5})
    store = SleepSummariesStore(_pool_with(conn))

    summary_id = await store.insert_summary(
        household_member_id=7,
        night_of=date(2026, 5, 12),
        asleep_at=datetime(2026, 5, 12, 23, tzinfo=UTC),
        awake_at=datetime(2026, 5, 13, 7, tzinfo=UTC),
        duration_minutes=480,
        deep_sleep_minutes=80,
        observer_likely_asleep_at=None,
        observer_likely_awake_at=None,
        interruptions=0,
        guessed_quality="great",
        guessed_reasoning="test",
    )

    query = conn.fetchrow.await_args.args[0]
    assert summary_id == 5
    assert "ON CONFLICT" in query
    assert "sleep_summaries" in query
