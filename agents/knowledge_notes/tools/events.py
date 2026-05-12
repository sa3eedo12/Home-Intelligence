from __future__ import annotations

from typing import Any

from home_agents_sdk import tool
from home_agents_sdk.event_log import EventLogStore
from home_agents_sdk.telemetry import get_logger

from tools.core import _embedder, _pool, _qdrant

logger = get_logger("knowledge_notes.events")


async def _event_store() -> EventLogStore:
    return EventLogStore(pool=await _pool(), qdrant=_qdrant(), embedder=await _embedder())


@tool("record_event", side_effects=True)
async def record_event(
    agent: str,
    capability: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    try:
        store = await _event_store()
    except Exception as exc:
        logger.warning("event_store_unavailable", error=str(exc))
        return {"ok": False, "error": "event_log_unavailable"}
    return await store.record_event(
        agent=agent,
        capability=capability,
        summary=summary,
        payload=payload,
        ts=ts,
    )


@tool("recall_recent")
async def recall_recent(window_minutes: int = 60, agent: str | None = None) -> dict[str, Any]:
    try:
        store = await _event_store()
    except Exception as exc:
        logger.warning("event_store_unavailable", error=str(exc))
        return {"items": [], "window_minutes": window_minutes, "agent": agent}
    return await store.recall_recent(window_minutes=window_minutes, agent=agent)


@tool("search_events")
async def search_events(query: str, top_k: int = 5) -> dict[str, Any]:
    try:
        store = await _event_store()
    except Exception as exc:
        logger.warning("event_store_unavailable", error=str(exc))
        return {"items": []}
    return await store.search_events(query=query, top_k=top_k)
