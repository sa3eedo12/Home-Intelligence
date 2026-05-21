"""Tests for knowledge_graph_seeder.seed_from_baseline."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.knowledge_graph_seeder import seed_from_baseline
from home_agents_sdk.knowledge_graph_store import KnowledgeGraphStore


def _pool_with(conn: MagicMock) -> MagicMock:
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=conn)
    manager.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = manager
    return pool


@pytest.mark.asyncio
async def test_seeder_handles_no_pool() -> None:
    counts = await seed_from_baseline(pool=None)
    assert counts == {
        "persons": 0,
        "devices": 0,
        "preferences": 0,
        "profile_facts": 0,
        "owns_edges": 0,
        "located_edges": 0,
    }


@pytest.mark.asyncio
async def test_seeder_creates_persons_devices_prefs_and_edges() -> None:
    """A representative slice of household_members + things + prefs +
    user_profile produces the right node/edge counts and the OWNS
    edge points from owner person → owned device."""
    # ── Mock conn that returns different rows per query ──
    queries: list[str] = []

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        queries.append(query)
        if "FROM household_members" in query:
            return [
                {
                    "id": 1,
                    "name": "Saeed",
                    "role": "adult",
                    "telegram_chat_id": 123,
                    "sleep_time": None,
                    "wake_time": None,
                    "attributes": '{"timezone":"Europe/London"}',
                }
            ]
        if "FROM things" in query:
            return [
                {
                    "id": 5,
                    "type": "light",
                    "friendly_name": "Office Light",
                    "attributes": '{"area":"Office"}',
                    "ha_entity_ids": ["light.office"],
                    "owner_member_id": 1,
                    "source": "ha",
                },
                {
                    "id": 6,
                    "type": "switch",
                    "friendly_name": "Kitchen Switch",
                    "attributes": '{"area":"Office"}',  # same area to test dedup
                    "ha_entity_ids": ["switch.kitchen"],
                    "owner_member_id": None,
                    "source": None,
                },
            ]
        if "FROM preferences" in query:
            return [{"key": "music.morning", "value": '"jazz"',
                     "confidence": 0.8, "source": "inferred"}]
        if "FROM user_profile" in query:
            return [{"key": "wake_target", "value": '"07:30"',
                     "confidence": 0.9, "source": "telegram"}]
        return []

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fake_fetch)
    pool = _pool_with(conn)

    # ── Stub store: capture upsert calls ──
    store = KnowledgeGraphStore(pool)
    node_calls: list[dict[str, Any]] = []
    edge_calls: list[dict[str, Any]] = []
    next_id = {"n": 100, "e": 500}

    async def upsert_node(**kwargs: Any) -> int:
        node_calls.append(kwargs)
        next_id["n"] += 1
        return next_id["n"]

    async def upsert_edge(**kwargs: Any) -> int:
        edge_calls.append(kwargs)
        next_id["e"] += 1
        return next_id["e"]

    store.upsert_node = upsert_node  # type: ignore[assignment]
    store.upsert_edge = upsert_edge  # type: ignore[assignment]

    counts = await seed_from_baseline(pool, store=store)

    assert counts["persons"] == 1
    assert counts["devices"] == 2
    assert counts["preferences"] == 1
    assert counts["profile_facts"] == 1
    # one owner → one OWNS edge (the second device has no owner)
    assert counts["owns_edges"] == 1
    # both devices share "Office" area → two LOCATED_IN edges; area node
    # is deduped across them (1 area upsert) but each device gets its own edge
    assert counts["located_edges"] == 2

    # Person node was upserted with the right external_ref
    person_call = next(c for c in node_calls if c["type"] == "person")
    assert person_call["external_ref"] == "household_members:1"
    assert person_call["label"] == "Saeed"

    # Device upserts both present
    device_refs = {c["external_ref"] for c in node_calls if c["type"] == "device"}
    assert device_refs == {"things:5", "things:6"}

    # Area dedup: only one "area" upsert was issued for "Office"
    area_calls = [c for c in node_calls if c["type"] == "area"]
    assert len(area_calls) == 1
    assert area_calls[0]["label"] == "Office"

    # The OWNS edge runs source=person(101) → target=device(102) (first device)
    owns_edges = [e for e in edge_calls if e["rel_type"] == "OWNS"]
    assert len(owns_edges) == 1
    assert owns_edges[0]["source_node_id"] == 101  # person was first
    assert owns_edges[0]["target_node_id"] == 102  # first device

    # Both LOCATED_IN edges point at the same area node id
    loc_edges = [e for e in edge_calls if e["rel_type"] == "LOCATED_IN"]
    assert len(loc_edges) == 2
    area_targets = {e["target_node_id"] for e in loc_edges}
    assert len(area_targets) == 1


@pytest.mark.asyncio
async def test_seeder_is_idempotent_via_external_ref() -> None:
    """Calling seed twice with the same source rows must not produce
    duplicate nodes — KnowledgeGraphStore.upsert_node already returns
    the existing id on conflict via (type, external_ref), so calling
    counts simply reflect 'rows processed', not 'rows inserted'.

    This test pins the contract: the seeder uses external_ref for
    every upsert it makes.
    """
    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM household_members" in query:
            return [{"id": 1, "name": "Saeed", "role": "adult",
                     "telegram_chat_id": None, "sleep_time": None,
                     "wake_time": None, "attributes": "{}"}]
        if "FROM things" in query:
            return [{"id": 5, "type": "light", "friendly_name": "X",
                     "attributes": "{}", "ha_entity_ids": [],
                     "owner_member_id": 1, "source": None}]
        if "FROM preferences" in query:
            return [{"key": "k", "value": '"v"',
                     "confidence": 0.5, "source": None}]
        if "FROM user_profile" in query:
            return [{"key": "k", "value": '"v"',
                     "confidence": 0.5, "source": None}]
        return []

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fake_fetch)
    pool = _pool_with(conn)
    store = KnowledgeGraphStore(pool)

    seen_node_calls: list[dict[str, Any]] = []
    seen_edge_calls: list[dict[str, Any]] = []

    async def upsert_node(**kwargs: Any) -> int:
        seen_node_calls.append(kwargs)
        return 1

    async def upsert_edge(**kwargs: Any) -> int:
        seen_edge_calls.append(kwargs)
        return 1

    store.upsert_node = upsert_node  # type: ignore[assignment]
    store.upsert_edge = upsert_edge  # type: ignore[assignment]

    await seed_from_baseline(pool, store=store)
    # Every node upsert carries an external_ref — that's how idempotence
    # is achieved at the store layer.
    for call in seen_node_calls:
        assert call.get("external_ref"), f"missing external_ref in {call!r}"
