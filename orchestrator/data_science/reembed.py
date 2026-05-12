from __future__ import annotations

import uuid
from typing import Any

from home_agents_sdk.event_log import EVENT_LOG_COLLECTION, EVENT_LOG_VECTOR_SIZE, event_text
from home_agents_sdk.telemetry import get_logger
from qdrant_client import models

from .common import SingleFlightJob, current_embedding_model, decode_json, format_ts, maybe_await

_POINT_NAMESPACE = uuid.UUID("f13d53d6-0d8d-4c89-a029-21c5f64cf0f0")
logger = get_logger("orchestrator.data_science.reembed")


class ReembedJob(SingleFlightJob):
    def __init__(
        self,
        pool: Any,
        qdrant: Any,
        embedder: Any,
        batch_size: int = 50,
        event_log_store: Any | None = None,
    ) -> None:
        super().__init__(job_name="reembed", pool=pool, event_log_store=event_log_store)
        self.qdrant = qdrant
        self.embedder = embedder
        self.batch_size = max(1, int(batch_size or 50))

    @property
    def current_model(self) -> str:
        return current_embedding_model(self.embedder)

    async def run(self) -> dict[str, Any]:
        return await self._run_singleflight(self._run)

    async def _run(self) -> dict[str, Any]:
        current_model = self.current_model
        if self.pool is None:
            return {
                "status": "skipped",
                "reason": "postgres_unavailable",
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "current_model": current_model,
            }
        if self.qdrant is None or self.embedder is None:
            return {
                "status": "skipped",
                "reason": "semantic_index_unavailable",
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "current_model": current_model,
            }

        processed = 0
        errors = 0
        skipped = await self._already_current_count(current_model)

        while True:
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT id, ts, agent, capability, summary, payload, embedding_model
                        FROM event_log
                        WHERE embedding_model IS DISTINCT FROM $1
                        ORDER BY id
                        LIMIT $2
                        """,
                        current_model,
                        self.batch_size,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("reembed_select_failed", error=str(exc))
                return {
                    "status": "skipped",
                    "reason": "postgres_unavailable",
                    "processed": processed,
                    "skipped": skipped,
                    "errors": errors + 1,
                    "current_model": current_model,
                }

            if not rows:
                break

            ids: list[int] = []
            points: list[models.PointStruct] = []
            for row in rows:
                data = dict(row)
                payload = decode_json(data.get("payload"), {})
                try:
                    text = event_text(
                        agent=str(data.get("agent") or ""),
                        capability=str(data.get("capability") or ""),
                        summary=str(data.get("summary") or ""),
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                    )
                    vector = await self.embedder.embed(text)
                    points.append(
                        models.PointStruct(
                            id=str(uuid.uuid5(_POINT_NAMESPACE, str(data.get("id")))),
                            vector=vector,
                            payload={
                                "event_id": data.get("id"),
                                "ts": format_ts(data.get("ts")),
                                "agent": data.get("agent"),
                                "capability": data.get("capability"),
                                "summary": data.get("summary"),
                                "payload": payload,
                                "text": text,
                                "embedding_model": current_model,
                            },
                        )
                    )
                    ids.append(int(data.get("id")))
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    logger.warning("reembed_event_failed", event_id=data.get("id"), error=str(exc))

            if not points:
                break

            try:
                await self._ensure_collection(len(points[0].vector or []) or EVENT_LOG_VECTOR_SIZE)
                await maybe_await(self.qdrant.upsert(EVENT_LOG_COLLECTION, points=points))
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE event_log
                        SET embedding_model = $1
                        WHERE id = ANY($2::int[])
                        """,
                        current_model,
                        ids,
                    )
                processed += len(ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning("reembed_batch_upsert_failed", error=str(exc))
                errors += len(ids)
                break

            if len(rows) < self.batch_size:
                break

        return {
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "current_model": current_model,
        }

    async def _already_current_count(self, current_model: str) -> int:
        try:
            async with self.pool.acquire() as conn:
                value = await conn.fetchval(
                    """
                    SELECT count(*)
                    FROM event_log
                    WHERE embedding_model IS NOT DISTINCT FROM $1
                    """,
                    current_model,
                )
            return int(value or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reembed_skip_count_failed", error=str(exc))
            return 0

    async def _ensure_collection(self, vector_size: int) -> None:
        get_collection = getattr(self.qdrant, "get_collection", None)
        create_collection = getattr(self.qdrant, "create_collection", None)
        if not callable(get_collection) or not callable(create_collection):
            return
        try:
            await maybe_await(get_collection(EVENT_LOG_COLLECTION))
        except Exception:
            await maybe_await(
                create_collection(
                    EVENT_LOG_COLLECTION,
                    vectors_config=models.VectorParams(
                        size=vector_size,
                        distance=models.Distance.COSINE,
                    ),
                )
            )
