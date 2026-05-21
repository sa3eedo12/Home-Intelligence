"""Tests for KnowledgeGraphStore (property-graph CRUD + traversal)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.knowledge_graph_store import KnowledgeGraphStore


def _pool_with(conn: MagicMock) -> MagicMock:
    """asyncpg pool.acquire() is sync — returns an async context manager."""
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=conn)
    manager.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = manager
    return pool


# ── Node CRUD ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_node_with_external_ref_uses_natural_key() -> None:
    """When external_ref is provided, upsert uses (type, external_ref)
    as the unique key — re-seeding from source tables is idempotent."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 7})
    store = KnowledgeGraphStore(_pool_with(conn))

    node_id = await store.upsert_node(
        type="person",
        label="Saeed",
        attributes={"role": "adult", "sleep_time": "00:30"},
        external_ref="household_members:2",
        source="seeder",
        confidence=1.0,
    )
    assert node_id == 7
    query, *params = conn.fetchrow.await_args.args
    assert "ON CONFLICT (type, external_ref)" in query
    assert "DO UPDATE" in query
    assert params[0] == "person"
    assert params[1] == "Saeed"
    # attributes serialized to JSON string for asyncpg jsonb binding
    assert json.loads(params[2]) == {"role": "adult", "sleep_time": "00:30"}
    assert params[3] == "household_members:2"


@pytest.mark.asyncio
async def test_upsert_node_without_external_ref_always_inserts() -> None:
    """Without external_ref the call always inserts a new row — caller
    is expected to dedup via find_nodes first if that matters."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 42})
    store = KnowledgeGraphStore(_pool_with(conn))

    node_id = await store.upsert_node(type="observation", label="anomaly")
    assert node_id == 42
    query = conn.fetchrow.await_args.args[0]
    assert "ON CONFLICT" not in query


@pytest.mark.asyncio
async def test_get_node_returns_decoded_attributes() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "type": "person",
            "label": "Saeed",
            "attributes": '{"role":"adult"}',
            "external_ref": "household_members:2",
            "source": "seeder",
            "confidence": 1.0,
            "created_at": datetime(2026, 5, 21, tzinfo=UTC),
            "updated_at": datetime(2026, 5, 21, tzinfo=UTC),
        }
    )
    store = KnowledgeGraphStore(_pool_with(conn))
    node = await store.get_node(7)
    assert node is not None
    # JSON string attribute auto-decoded
    assert node["attributes"] == {"role": "adult"}
    # Query filters deleted
    query = conn.fetchrow.await_args.args[0]
    assert "deleted_at IS NULL" in query


@pytest.mark.asyncio
async def test_find_nodes_filters_by_type_and_label() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = KnowledgeGraphStore(_pool_with(conn))

    await store.find_nodes(type="device", label="Office Light")
    query, *params = conn.fetch.await_args.args
    assert "type = $1" in query
    assert "lower(label) = $2" in query
    assert "deleted_at IS NULL" in query
    assert params[0] == "device"
    assert params[1] == "office light"  # case-folded


@pytest.mark.asyncio
async def test_delete_node_soft_deletes_node_and_edges() -> None:
    """Soft delete cascades: node + every connected edge gets
    deleted_at set in the same transaction."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    store = KnowledgeGraphStore(_pool_with(conn))

    ok = await store.delete_node(7)
    assert ok is True
    # Two UPDATEs (node soft-delete + edge soft-delete) inside a tx.
    assert conn.execute.await_count == 2
    queries = [call.args[0] for call in conn.execute.await_args_list]
    assert any("UPDATE kg_nodes SET deleted_at" in q for q in queries)
    assert any("UPDATE kg_edges SET deleted_at" in q for q in queries)


# ── Edge CRUD ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_edge_uses_3tuple_natural_key() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    store = KnowledgeGraphStore(_pool_with(conn))

    edge_id = await store.upsert_edge(
        source_node_id=7,
        target_node_id=12,
        rel_type="OWNS",
        attributes={"since": "2024"},
        source="seeder",
        confidence=0.95,
    )
    assert edge_id == 99
    query, *params = conn.fetchrow.await_args.args
    assert "ON CONFLICT (source_node_id, target_node_id, rel_type)" in query
    assert "DO UPDATE" in query
    assert params[0] == 7
    assert params[1] == 12
    assert params[2] == "OWNS"
    assert json.loads(params[3]) == {"since": "2024"}


@pytest.mark.asyncio
async def test_find_edges_filters_compose_correctly() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = KnowledgeGraphStore(_pool_with(conn))

    await store.find_edges(source_node_id=7, rel_type="OWNS")
    query, *params = conn.fetch.await_args.args
    assert "source_node_id = $1" in query
    assert "rel_type = $2" in query
    assert "deleted_at IS NULL" in query
    assert params[0] == 7
    assert params[1] == "OWNS"


# ── Traversal ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_neighbors_out_returns_targets() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 12,
                "type": "device",
                "label": "HAN",
                "attributes": "{}",
                "external_ref": "things:5",
                "source": None,
                "confidence": 1.0,
                "created_at": datetime(2026, 5, 21, tzinfo=UTC),
                "updated_at": datetime(2026, 5, 21, tzinfo=UTC),
            }
        ]
    )
    store = KnowledgeGraphStore(_pool_with(conn))

    targets = await store.neighbors(7, rel_type="OWNS", direction="out")
    assert len(targets) == 1
    assert targets[0]["label"] == "HAN"
    query = conn.fetch.await_args.args[0]
    assert "n.id = e.target_node_id" in query
    assert "e.source_node_id = $1" in query
    assert "e.rel_type = $2" in query


@pytest.mark.asyncio
async def test_neighbors_in_returns_sources() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = KnowledgeGraphStore(_pool_with(conn))

    await store.neighbors(12, rel_type="OWNS", direction="in")
    query = conn.fetch.await_args.args[0]
    assert "n.id = e.source_node_id" in query
    assert "e.target_node_id = $1" in query


@pytest.mark.asyncio
async def test_neighbors_both_returns_either_direction() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = KnowledgeGraphStore(_pool_with(conn))

    await store.neighbors(7, direction="both")
    query = conn.fetch.await_args.args[0]
    # "both" includes either direction + excludes the self-node from results.
    assert "(n.id = e.target_node_id OR n.id = e.source_node_id)" in query
    assert "n.id <> $1" in query


@pytest.mark.asyncio
async def test_neighbors_rejects_invalid_direction() -> None:
    store = KnowledgeGraphStore(_pool_with(MagicMock()))
    with pytest.raises(ValueError):
        await store.neighbors(7, direction="diagonal")


@pytest.mark.asyncio
async def test_who_owns_delegates_to_incoming_owns_edges() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = KnowledgeGraphStore(_pool_with(conn))

    await store.who_owns(12)
    query = conn.fetch.await_args.args[0]
    # incoming OWNS edges on the device = persons who own it
    assert "n.id = e.source_node_id" in query


@pytest.mark.asyncio
async def test_located_in_returns_single_area_or_none() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 30,
                "type": "area",
                "label": "Office",
                "attributes": "{}",
                "external_ref": None,
                "source": None,
                "confidence": 1.0,
                "created_at": datetime(2026, 5, 21, tzinfo=UTC),
                "updated_at": datetime(2026, 5, 21, tzinfo=UTC),
            }
        ]
    )
    store = KnowledgeGraphStore(_pool_with(conn))
    area = await store.located_in(12)
    assert area is not None
    assert area["label"] == "Office"

    # No edges → None
    conn.fetch = AsyncMock(return_value=[])
    assert await store.located_in(12) is None


# ── Stats ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_returns_counts_by_type() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[5, 8])  # node count, edge count
    conn.fetch = AsyncMock(
        return_value=[
            {"type": "person", "n": 2},
            {"type": "device", "n": 3},
        ]
    )
    store = KnowledgeGraphStore(_pool_with(conn))

    stats = await store.stats()
    assert stats == {
        "nodes": 5,
        "edges": 8,
        "by_type": {"person": 2, "device": 3},
    }


# ── No-pool path (degrades gracefully) ───────────────────────────


@pytest.mark.asyncio
async def test_store_returns_safe_defaults_without_pool() -> None:
    """All public methods must handle pool=None without raising."""
    store = KnowledgeGraphStore(pool=None)

    assert await store.upsert_node(type="x", label="y") is None
    assert await store.get_node(1) is None
    assert await store.find_nodes(type="x") == []
    assert await store.delete_node(1) is False
    assert await store.upsert_edge(
        source_node_id=1, target_node_id=2, rel_type="X"
    ) is None
    assert await store.find_edges() == []
    assert await store.delete_edge(1) is False
    assert await store.neighbors(1) == []
    assert await store.who_owns(1) == []
    assert await store.located_in(1) is None
    assert await store.stats() == {"nodes": 0, "edges": 0, "by_type": {}}
