from __future__ import annotations

import json
from typing import Any

import asyncpg

FINAL_STATUSES = {"confirmed", "rejected", "skipped", "expired"}


def _jsonb(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _decode_jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["proposed_action"] = _decode_jsonb(data.get("proposed_action"))
    data["confirmed_action_result"] = _decode_jsonb(data.get("confirmed_action_result"))
    return data


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


class AutoInferencesStore:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def insert(
        self,
        *,
        source_event_log_id: int | None,
        source_kind: str,
        inference: str,
        confidence: float,
        reasoning: str | None = None,
        proposed_action: dict[str, Any] | None = None,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO auto_inferences (
                    source_event_log_id, source_kind, inference, confidence,
                    reasoning, proposed_action
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING id
                """,
                source_event_log_id,
                source_kind,
                inference,
                float(confidence),
                reasoning,
                _jsonb(proposed_action),
            )
        return int(row["id"]) if row else None

    async def get(self, auto_inference_id: int) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, source_event_log_id, source_kind, inference, confidence,
                       reasoning, proposed_action, status, confirmed_action_result,
                       confirmed_at, confirmed_by_chat_id, created_at
                  FROM auto_inferences
                 WHERE id = $1
                """,
                auto_inference_id,
            )
        return _row_to_dict(row) if row else None

    async def confirm(
        self,
        auto_inference_id: int,
        *,
        chat_id: int | None = None,
        action_result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE auto_inferences
                   SET status = 'confirmed',
                       confirmed_action_result = $2::jsonb,
                       confirmed_at = now(),
                       confirmed_by_chat_id = $3
                 WHERE id = $1
                   AND status = 'proposed'
             RETURNING id, source_event_log_id, source_kind, inference, confidence,
                       reasoning, proposed_action, status, confirmed_action_result,
                       confirmed_at, confirmed_by_chat_id, created_at
                """,
                auto_inference_id,
                _jsonb(action_result),
                chat_id,
            )
        return _row_to_dict(row) if row else None

    async def reject(
        self,
        auto_inference_id: int,
        *,
        status: str = "rejected",
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"rejected", "skipped", "expired"}:
            raise ValueError("status must be rejected, skipped, or expired")
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE auto_inferences
                   SET status = $2,
                       confirmed_at = now(),
                       confirmed_by_chat_id = $3
                 WHERE id = $1
                   AND status = 'proposed'
             RETURNING id, source_event_log_id, source_kind, inference, confidence,
                       reasoning, proposed_action, status, confirmed_action_result,
                       confirmed_at, confirmed_by_chat_id, created_at
                """,
                auto_inference_id,
                status,
                chat_id,
            )
        return _row_to_dict(row) if row else None

    async def recent_count_in_window(self, *, hours: int = 1) -> int:
        if not self._ready or self.pool is None:
            return 0
        bounded_hours = _bounded_int(hours, default=1, low=1, high=24 * 7)
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(*)
                  FROM auto_inferences
                 WHERE created_at >= now() - ($1::int * interval '1 hour')
                """,
                bounded_hours,
            )
        return int(value or 0)

    async def recent(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        bounded_limit = _bounded_int(limit, default=20, low=1, high=200)
        if status:
            query = """
                SELECT id, source_event_log_id, source_kind, inference, confidence,
                       reasoning, proposed_action, status, confirmed_action_result,
                       confirmed_at, confirmed_by_chat_id, created_at
                  FROM auto_inferences
                 WHERE status = $1
                 ORDER BY created_at DESC
                 LIMIT $2
            """
            args: tuple[Any, ...] = (status, bounded_limit)
        else:
            query = """
                SELECT id, source_event_log_id, source_kind, inference, confidence,
                       reasoning, proposed_action, status, confirmed_action_result,
                       confirmed_at, confirmed_by_chat_id, created_at
                  FROM auto_inferences
                 ORDER BY created_at DESC
                 LIMIT $1
            """
            args = (bounded_limit,)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [_row_to_dict(row) for row in rows]
