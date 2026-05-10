from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import asyncpg
from home_agents_sdk import tool
from home_agents_sdk.embeddings import Embedder
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.npu import NPUClient
from qdrant_client import AsyncQdrantClient, models

_POOL: asyncpg.Pool | None = None
_QDRANT: AsyncQdrantClient | None = None


async def _pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        database_url = os.getenv(
            "DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents"
        )
        _POOL = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    return _POOL


def _qdrant() -> AsyncQdrantClient:
    global _QDRANT
    if _QDRANT is None:
        _QDRANT = AsyncQdrantClient(url=os.getenv("QDRANT_URL", "http://qdrant:6333"))
    return _QDRANT


async def _embedder() -> Embedder:
    pool = await _pool()
    return Embedder(
        npu=NPUClient(os.getenv("LEMONADE_URL", "http://lemonade:8000")),
        llm=OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434")),
        pool=pool,
        npu_model=os.getenv("EMBED_MODEL", "bge-m3-int8"),
    )


async def _ensure_collection() -> None:
    client = _qdrant()
    try:
        await client.get_collection("media_metadata")
    except Exception:
        await client.create_collection(
            "media_metadata",
            vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
        )


def _recent_exclusion(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("title", "")).strip().lower() for r in rows if r.get("title")}


@tool("recently_watched")
async def recently_watched(limit: int = 30) -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            (
                "SELECT kind, title, status, rated, watched_at "
                "FROM media_history ORDER BY watched_at DESC LIMIT $1"
            ),
            limit,
        )
    return {"items": [dict(r) for r in rows]}


@tool("mark_watched", side_effects=True)
async def mark_watched(kind: str, title: str, rated: int | None = None) -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO media_history(kind, title, status, rated) VALUES ($1, $2, 'watched', $3)",
            kind,
            title,
            rated,
        )
    return {"ok": True, "title": title}


async def _upsert_media(items: list[dict[str, Any]]) -> int:
    await _ensure_collection()
    emb = await _embedder()
    points = []
    for idx, item in enumerate(items):
        text = f"{item.get('kind', '')} {item.get('title', '')} {item.get('summary', '')}".strip()
        vector = await emb.embed(text)
        unique_source = (
            f"{item.get('kind', '')}|{item.get('title', '')}|"
            f"{item.get('year', '')}|{item.get('source', '')}|{idx}"
        )
        point_id = f"media-{hashlib.sha256(unique_source.encode()).hexdigest()[:24]}"
        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "kind": item.get("kind", "movie"),
                    "title": item.get("title", ""),
                    "year": item.get("year"),
                    "summary": item.get("summary", ""),
                    "source": item.get("source", "library"),
                },
            )
        )
    if points:
        await _qdrant().upsert("media_metadata", points=points)
    return len(points)


@tool("library_index", side_effects=True)
async def library_index(path: str | None = None) -> dict[str, Any]:
    root = path or os.getenv("MEDIA_LIBRARY_PATH", "/data/media")
    items: list[dict[str, Any]] = []
    if root.lower().endswith(".json") and os.path.exists(root):  # noqa: ASYNC240
        with open(root, encoding="utf-8") as f:  # noqa: ASYNC230
            payload = json.load(f)
        for item in payload.get("items", []):
            items.append(item)
    elif os.path.isdir(root):  # noqa: ASYNC240
        for base, _dirs, files in os.walk(root):
            for name in files:
                if os.path.splitext(name)[1].lower() in {".mkv", ".mp4", ".avi", ".mov"}:
                    full_path = os.path.join(base, name)
                    items.append(
                        {
                            "kind": "movie",
                            "title": os.path.splitext(name)[0],
                            "source": full_path,
                        }
                    )
    else:
        return {"ok": False, "error": f"path not found: {root}"}

    upserted = await _upsert_media(items)
    return {"ok": True, "upserted": upserted}


@tool("library_search")
async def library_search(query: str, top_k: int = 5) -> dict[str, Any]:
    emb = await _embedder()
    vector = await emb.embed(query)
    hits = await _qdrant().search("media_metadata", query_vector=vector, limit=top_k)
    return {"items": [{"score": h.score, **(h.payload or {})} for h in hits]}


@tool("recommend")
async def recommend(
    mood: str = "relaxing",
    media_type: str = "movie",
    n: int = 3,
) -> dict[str, Any]:
    recent = await recently_watched(limit=30)
    excluded = _recent_exclusion(recent["items"])

    try:
        results = await library_search(f"{media_type} {mood}", top_k=20)
        picks = [
            item
            for item in results.get("items", [])
            if str(item.get("title", "")).strip().lower() not in excluded
        ][:n]
        if picks:
            return {"items": picks, "source": "library"}
    except Exception:
        pass

    fallbacks = [
        {"kind": media_type, "title": "Dune", "year": 2021},
        {"kind": media_type, "title": "Arrival", "year": 2016},
        {"kind": media_type, "title": "The Martian", "year": 2015},
    ]
    filtered = [item for item in fallbacks if item["title"].lower() not in excluded][:n]
    return {"items": filtered, "source": "fallback"}


@tool("discover")
async def discover(media_type: str = "movie", mood: str = "fun", n: int = 3) -> dict[str, Any]:
    discoveries = [
        {"kind": media_type, "title": "Everything Everywhere All at Once"},
        {"kind": media_type, "title": "Spider-Man: Across the Spider-Verse"},
        {"kind": media_type, "title": "Interstellar"},
    ]
    return {"items": discoveries[:n], "mood": mood}
