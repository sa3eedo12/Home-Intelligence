from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from home_agents_sdk.event_log import EventLogStore

from tools import events


class _FakeConn:
    def __init__(self) -> None:
        self.fetchrow_args = None
        self.fetch_args = None
        self.rows = [
            {
                "id": 2,
                "ts": datetime(2026, 1, 1, tzinfo=UTC),
                "agent": "washer",
                "capability": "cycle_complete",
                "summary": "Washer completed bedding cycle",
                "payload": '{"load":"sheets"}',
            }
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetchrow(self, _query, *args):
        self.fetchrow_args = args
        return {
            "id": 1,
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "agent": args[1],
            "capability": args[2],
            "summary": args[3],
            "payload": args[4],
        }

    async def fetch(self, _query, *args):
        self.fetch_args = args
        return self.rows


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        return self.conn


class _FakeEmbedder:
    async def embed(self, _text: str) -> list[float]:
        return [0.1] * 4


class _FakeQdrant:
    def __init__(self) -> None:
        self.created = False
        self.upserts = []
        self.searches = []

    async def get_collection(self, _collection: str) -> None:
        raise RuntimeError("missing")

    async def create_collection(self, _collection: str, vectors_config) -> None:
        self.created = True
        self.vector_size = vectors_config.size

    async def upsert(self, collection_name: str, points):
        self.upserts.append((collection_name, points))

    async def search(self, collection_name: str, query_vector, limit: int):
        self.searches.append((collection_name, query_vector, limit))
        return [
            SimpleNamespace(
                score=0.88,
                payload={
                    "event_id": 1,
                    "agent": "washer",
                    "capability": "cycle_complete",
                    "summary": "Washer completed bedding cycle",
                    "payload": {"load": "sheets"},
                },
            )
        ]


@pytest.mark.asyncio
async def test_record_event_persists_and_indexes(monkeypatch) -> None:
    pool = _FakePool()
    qdrant = _FakeQdrant()
    store = EventLogStore(pool=pool, qdrant=qdrant, embedder=_FakeEmbedder())

    async def _fake_store() -> EventLogStore:
        return store

    monkeypatch.setattr(events, "_event_store", _fake_store)

    result = await events.record_event(
        agent="washer",
        capability="cycle_complete",
        summary="Washer completed bedding cycle",
        payload={"load": "sheets"},
        ts="2026-01-01T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["semantic_indexed"] is True
    assert result["event"]["agent"] == "washer"
    assert json.loads(pool.conn.fetchrow_args[4]) == {"load": "sheets"}
    assert qdrant.created is True
    assert qdrant.vector_size == 4
    assert qdrant.upserts[0][0] == "event_log"


@pytest.mark.asyncio
async def test_recall_recent_returns_windowed_events() -> None:
    pool = _FakePool()
    store = EventLogStore(pool=pool)

    result = await store.recall_recent(window_minutes=120, agent="washer")

    assert result["window_minutes"] == 120
    assert result["agent"] == "washer"
    assert result["items"][0]["payload"] == {"load": "sheets"}
    assert pool.conn.fetch_args == (120, "washer", 100)


@pytest.mark.asyncio
async def test_search_events_uses_semantic_collection(monkeypatch) -> None:
    qdrant = _FakeQdrant()
    store = EventLogStore(pool=_FakePool(), qdrant=qdrant, embedder=_FakeEmbedder())

    async def _fake_store() -> EventLogStore:
        return store

    monkeypatch.setattr(events, "_event_store", _fake_store)

    result = await events.search_events("bedding wash", top_k=3)

    assert result["items"][0]["summary"] == "Washer completed bedding cycle"
    assert result["items"][0]["score"] == 0.88
    assert qdrant.searches[0][0] == "event_log"
    assert qdrant.searches[0][2] == 3
