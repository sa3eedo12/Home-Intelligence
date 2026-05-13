"""Storage SDK for TV/monitor left-on detections and confirmations."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg


class TvLeftOnStore:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def insert(
        self,
        *,
        entity_id: str,
        friendly_name: str | None,
        on_since: datetime | None,
        detected_at: datetime,
        on_hours: float | None,
        reason: str | None,
        suggested_action: str | None,
        event_log_id: int | None = None,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO tv_left_on (
                    event_log_id, entity_id, friendly_name, on_since, detected_at,
                    on_hours, reason, suggested_action
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                event_log_id,
                entity_id,
                friendly_name,
                on_since,
                detected_at,
                on_hours,
                reason,
                suggested_action,
            )
        return int(row["id"]) if row else None

    async def confirm(
        self,
        tv_left_on_id: int,
        *,
        action: str,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE tv_left_on
                   SET confirmed_action = $2,
                       confirmed_at = now(),
                       confirmed_by_chat_id = $3
                 WHERE id = $1
             RETURNING id, entity_id, friendly_name, on_since, detected_at,
                       on_hours, reason, suggested_action, confirmed_action,
                       confirmed_at, confirmed_by_chat_id
                """,
                tv_left_on_id,
                action,
                chat_id,
            )
        return dict(row) if row else None

    async def recent(
        self,
        *,
        limit: int = 20,
        only_unconfirmed: bool = False,
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        where = "WHERE confirmed_action IS NULL" if only_unconfirmed else ""
        bounded_limit = max(1, min(int(limit), 200))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, event_log_id, entity_id, friendly_name, on_since,
                       detected_at, on_hours, reason, suggested_action,
                       confirmed_action, confirmed_at, confirmed_by_chat_id,
                       created_at
                  FROM tv_left_on
                  {where}
                  ORDER BY detected_at DESC
                  LIMIT $1
                """,
                bounded_limit,
            )
        return [dict(row) for row in rows]

    async def enable_auto_off_at_bedtime(
        self,
        *,
        entity_id: str,
        friendly_name: str | None = None,
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        attrs = {"auto_off_at_bedtime": True}
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE things
                   SET attributes = COALESCE(attributes, '{}'::jsonb) || $2::jsonb,
                       last_confirmed_at = now(),
                       source = COALESCE(source, 'tv_left_on')
                 WHERE $1 = ANY(ha_entity_ids)
                    OR attributes->>'entity_id' = $1
             RETURNING id, type, friendly_name, attributes, ha_entity_ids
                """,
                entity_id,
                json.dumps(attrs),
            )
            if row is not None:
                return dict(row)

            thing_type = "device.monitor" if "monitor" in entity_id.casefold() else "device.tv"
            row = await conn.fetchrow(
                """
                INSERT INTO things(
                    type, friendly_name, attributes, ha_entity_ids, confidence, source
                )
                VALUES ($1, $2, $3::jsonb, ARRAY[$4]::text[], 0.4, 'tv_left_on')
                RETURNING id, type, friendly_name, attributes, ha_entity_ids
                """,
                thing_type,
                friendly_name or entity_id,
                json.dumps({**attrs, "entity_id": entity_id}),
                entity_id,
            )
        return dict(row) if row else None
