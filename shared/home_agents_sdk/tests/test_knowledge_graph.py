from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.knowledge_graph import KnowledgeGraph


def _pool_with(conn: MagicMock) -> MagicMock:
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=conn)
    manager.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = manager
    return pool


@pytest.mark.asyncio
async def test_list_things_filters_by_type() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 1,
                "type": "appliance.washer",
                "friendly_name": "Washer",
                "attributes": '{"brand":"LG"}',
                "ha_entity_ids": ["sensor.washer"],
                "photo_path": None,
                "confidence": 0.8,
                "learned_at": datetime(2026, 1, 1, tzinfo=UTC),
                "last_confirmed_at": None,
                "source": "test",
            }
        ]
    )
    graph = KnowledgeGraph(pool=_pool_with(conn))

    items = await graph.list_things(type="appliance.washer")

    assert items[0]["attributes"] == {"brand": "LG"}
    assert items[0]["learned_at"] == "2026-01-01T00:00:00+00:00"
    conn.fetch.assert_awaited_once()
    assert conn.fetch.await_args.args[1] == "appliance.washer"


@pytest.mark.asyncio
async def test_put_thing_inserts_json_and_arrays() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 2,
            "type": "vehicle.car",
            "friendly_name": "Family car",
            "attributes": {"make": "Tesla"},
            "ha_entity_ids": ["device_tracker.car"],
            "photo_path": "/data/car.jpg",
            "confidence": 0.7,
            "learned_at": None,
            "last_confirmed_at": None,
            "source": "user",
        }
    )
    graph = KnowledgeGraph(pool=_pool_with(conn))

    row = await graph.put_thing(
        type="vehicle.car",
        friendly_name="Family car",
        attributes={"make": "Tesla"},
        ha_entity_ids=["device_tracker.car"],
        photo_path="/data/car.jpg",
        confidence=0.7,
        source="user",
    )

    assert row and row["friendly_name"] == "Family car"
    args = conn.fetchrow.await_args.args
    assert json.loads(args[3]) == {"make": "Tesla"}
    assert args[4] == ["device_tracker.car"]


@pytest.mark.asyncio
async def test_confirm_and_forget_thing() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 1,
            "type": "appliance.washer",
            "friendly_name": "Washer",
            "attributes": {},
            "ha_entity_ids": [],
            "photo_path": None,
            "confidence": 0.9,
            "learned_at": None,
            "last_confirmed_at": datetime(2026, 1, 2, tzinfo=UTC),
            "source": "user",
        }
    )
    conn.execute = AsyncMock(return_value="DELETE 1")
    graph = KnowledgeGraph(pool=_pool_with(conn))

    confirmed = await graph.confirm_thing(1)
    deleted = await graph.forget_thing(1)

    assert confirmed and confirmed["last_confirmed_at"] == "2026-01-02T00:00:00+00:00"
    assert deleted is True
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_preferences_upsert_and_noop_without_pool() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "key": "lights.after_sunset",
            "value": '{"color":"warm"}',
            "confidence": 0.6,
            "source": "user",
            "updated_at": datetime(2026, 1, 3, tzinfo=UTC),
        }
    )
    graph = KnowledgeGraph(pool=_pool_with(conn))

    pref = await graph.put_preference(
        key="lights.after_sunset",
        value={"color": "warm"},
        confidence=0.6,
        source="user",
    )

    assert pref and pref["value"] == {"color": "warm"}
    assert await KnowledgeGraph(pool=None).put_preference(key="x", value={}) is None
    assert await KnowledgeGraph(pool=None).list_preferences() == []


@pytest.mark.asyncio
async def test_evidence_for_uses_friendly_identifier() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"identifier": "Washer"})
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 9,
                "ts": datetime(2026, 1, 4, tzinfo=UTC),
                "agent": "home_automation",
                "capability": "recent_appliance_activity",
                "summary": "Washer completed a bedding cycle",
                "payload": '{"entity":"sensor.washer"}',
            }
        ]
    )
    graph = KnowledgeGraph(pool=_pool_with(conn))

    evidence = await graph.evidence_for("things", 1)

    assert evidence[0]["summary"] == "Washer completed a bedding cycle"
    assert evidence[0]["payload"] == {"entity": "sensor.washer"}
    assert conn.fetch.await_args.args[1] == "Washer"
