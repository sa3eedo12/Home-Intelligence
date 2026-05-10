from __future__ import annotations

import httpx
from redis.asyncio import Redis


async def probe_ollama(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_lemonade(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("id", "") for m in data.get("data", [])]
            return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_postgres(pool) -> dict:
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_redis(redis_url: str) -> dict:
    try:
        client = Redis.from_url(redis_url, decode_responses=True)
        await client.ping()
        await client.aclose()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_qdrant(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/healthz")
            resp.raise_for_status()
            return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
