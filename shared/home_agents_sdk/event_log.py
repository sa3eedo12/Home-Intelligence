from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg
from qdrant_client import AsyncQdrantClient, models

from .telemetry import get_logger

EVENT_LOG_COLLECTION = "event_log"
EVENT_LOG_VECTOR_SIZE = 1024
RECENT_EVENT_LIMIT = 100
_POINT_NAMESPACE = uuid.UUID("f13d53d6-0d8d-4c89-a029-21c5f64cf0f0")

logger = get_logger("home_agents_sdk.event_log")


def _jsonable_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    try:
        return json.loads(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return {"raw": str(payload)}


def _decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {"raw": raw}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    if raw is None:
        return {}
    return {"raw": str(raw)}


def _format_ts(raw: Any) -> str | None:
    if isinstance(raw, datetime):
        return raw.isoformat()
    if raw is None:
        return None
    return str(raw)


def row_to_event(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data.get("id"),
        "ts": _format_ts(data.get("ts")),
        "agent": data.get("agent"),
        "capability": data.get("capability"),
        "summary": data.get("summary"),
        "payload": _decode_payload(data.get("payload")),
    }


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def event_text(
    *,
    agent: str,
    capability: str,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> str:
    clean_payload = _jsonable_payload(payload)
    text = f"{agent}.{capability}: {summary.strip()}"
    if clean_payload:
        text += " | " + json.dumps(clean_payload, ensure_ascii=False, default=str)
    return text[:1000]


class EventLogStore:
    """Postgres + Qdrant backed episodic event log.

    Postgres is the source of truth. Qdrant indexing is best-effort so event
    recording still succeeds when semantic memory is temporarily unavailable.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        qdrant: AsyncQdrantClient | None = None,
        embedder: Any | None = None,
    ) -> None:
        self.pool = pool
        self.qdrant = qdrant
        self.embedder = embedder

    async def record_event(
        self,
        *,
        agent: str,
        capability: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        clean_payload = _jsonable_payload(payload)
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO event_log(ts, agent, capability, summary, payload)
                    VALUES (COALESCE($1::timestamptz, now()), $2, $3, $4, $5::jsonb)
                    RETURNING id, ts, agent, capability, summary, payload
                    """,
                    ts,
                    agent,
                    capability,
                    summary,
                    json.dumps(clean_payload, default=str),
                )
        except Exception as exc:
            logger.warning("event_log_record_failed", error=str(exc))
            return {"ok": False, "error": "event_log_unavailable"}

        event = row_to_event(row)
        semantic_indexed = await self._index_event(event, clean_payload)
        return {"ok": True, "event": event, "semantic_indexed": semantic_indexed}

    async def recall_recent(
        self,
        *,
        window_minutes: int = 60,
        agent: str | None = None,
    ) -> dict[str, Any]:
        bounded_window = _bounded_int(
            window_minutes,
            default=60,
            low=1,
            high=7 * 24 * 60,
        )
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, ts, agent, capability, summary, payload
                    FROM event_log
                    WHERE ts >= now() - ($1::int * interval '1 minute')
                      AND ($2::text IS NULL OR agent = $2::text)
                    ORDER BY ts DESC
                    LIMIT $3
                    """,
                    bounded_window,
                    agent,
                    RECENT_EVENT_LIMIT,
                )
        except Exception as exc:
            logger.warning("event_log_recall_failed", error=str(exc))
            return {"items": [], "window_minutes": bounded_window, "agent": agent}
        return {
            "items": [row_to_event(row) for row in rows],
            "window_minutes": bounded_window,
            "agent": agent,
        }

    async def search_events(self, *, query: str, top_k: int = 5) -> dict[str, Any]:
        if not query.strip() or self.qdrant is None or self.embedder is None:
            return {"items": []}
        limit = _bounded_int(top_k, default=5, low=1, high=20)
        try:
            vector = await self.embedder.embed(query)
            hits = await self.qdrant.search(
                EVENT_LOG_COLLECTION,
                query_vector=vector,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("event_log_search_failed", error=str(exc))
            return {"items": []}

        items: list[dict[str, Any]] = []
        for hit in hits:
            payload = dict(hit.payload or {})
            payload["score"] = float(hit.score)
            if "payload" in payload:
                payload["payload"] = _decode_payload(payload.get("payload"))
            items.append(payload)
        return {"items": items}

    async def _index_event(self, event: dict[str, Any], payload: dict[str, Any]) -> bool:
        if self.qdrant is None or self.embedder is None:
            return False
        try:
            text = event_text(
                agent=str(event.get("agent") or ""),
                capability=str(event.get("capability") or ""),
                summary=str(event.get("summary") or ""),
                payload=payload,
            )
            vector = await self.embedder.embed(text)
            await self._ensure_collection(len(vector) or EVENT_LOG_VECTOR_SIZE)
            await self.qdrant.upsert(
                EVENT_LOG_COLLECTION,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid5(_POINT_NAMESPACE, str(event.get("id")))),
                        vector=vector,
                        payload={
                            "event_id": event.get("id"),
                            "ts": event.get("ts"),
                            "agent": event.get("agent"),
                            "capability": event.get("capability"),
                            "summary": event.get("summary"),
                            "payload": payload,
                            "text": text,
                        },
                    )
                ],
            )
            return True
        except Exception as exc:
            logger.warning("event_log_semantic_index_failed", error=str(exc))
            return False

    async def _ensure_collection(self, vector_size: int) -> None:
        if self.qdrant is None:
            return
        try:
            await self.qdrant.get_collection(EVENT_LOG_COLLECTION)
        except Exception:
            await self.qdrant.create_collection(
                EVENT_LOG_COLLECTION,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE,
                ),
            )
