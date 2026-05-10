from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from qdrant_client import AsyncQdrantClient, models
from redis.asyncio import Redis


class KVStore:
    def __init__(self, client: Redis) -> None:
        self.client = client

    async def get(self, key: str) -> str | None:
        value = await self.client.get(key)
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value if value is not None else None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        await self.client.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self.client.delete(key)


class EpisodicStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def write(self, table: str, payload: dict[str, Any]) -> None:
        if table not in {"alerts", "reminders", "workflows"}:
            raise ValueError("Unsupported table")
        columns = ", ".join(payload.keys())
        placeholders = ", ".join(f"${i}" for i in range(1, len(payload) + 1))
        values = list(payload.values())
        safe_table = {"alerts": "alerts", "reminders": "reminders", "workflows": "workflows"}[table]
        query = f"INSERT INTO {safe_table} ({columns}) VALUES ({placeholders})"
        async with self.pool.acquire() as conn:
            await conn.execute(query, *values)

    async def query(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(row) for row in rows]


class SemanticStore:
    def __init__(self, client: AsyncQdrantClient, embedder: Any) -> None:
        self.client = client
        self.embedder = embedder

    async def upsert(self, collection: str, text: str, metadata: dict[str, Any]) -> str:
        vector = await self.embedder.embed(text)
        point_id = str(uuid.uuid4())
        await self.client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(id=point_id, vector=vector, payload={"text": text, **metadata})
            ],
        )
        return point_id

    async def search(self, collection: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        vector = await self.embedder.embed(query)
        hits = await self.client.search(
            collection_name=collection, query_vector=vector, limit=top_k
        )
        return [{"id": hit.id, "score": hit.score, "payload": hit.payload} for hit in hits]


class Memory:
    def __init__(
        self,
        redis_client: Redis,
        pg_pool: asyncpg.Pool,
        qdrant_client: AsyncQdrantClient,
        embedder: Any,
    ) -> None:
        self.kv = KVStore(redis_client)
        self.episodic = EpisodicStore(pg_pool)
        self.semantic = SemanticStore(qdrant_client, embedder)
