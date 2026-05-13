"""Storage SDK for ``cycle_loads`` (appliance cycle → guessed/confirmed load).

Used by ``agents.household_ops.tools.cycle_loads`` to persist inferences and
look up confirmation history for future guesses. The actual inference logic
lives in the household_ops agent — this module only handles persistence.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg


class CycleLoadsStore:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def insert_guess(
        self,
        *,
        appliance: str,
        entity_id: str | None,
        started_at: datetime | None,
        ended_at: datetime,
        duration_seconds: int | None,
        program: str | None,
        brand: str | None,
        attributes_at_finish: dict[str, Any],
        guessed_label: str | None,
        guessed_confidence: float | None,
        guessed_reasoning: str | None,
        event_log_id: int | None = None,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cycle_loads (
                    event_log_id, appliance, entity_id, started_at, ended_at,
                    duration_seconds, program, brand, attributes_at_finish,
                    guessed_label, guessed_confidence, guessed_reasoning
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9::jsonb,
                    $10, $11, $12
                )
                RETURNING id
                """,
                event_log_id,
                appliance,
                entity_id,
                started_at,
                ended_at,
                duration_seconds,
                program,
                brand,
                json.dumps(attributes_at_finish or {}),
                guessed_label,
                guessed_confidence,
                guessed_reasoning,
            )
        return int(row["id"]) if row else None

    async def confirm(
        self,
        cycle_load_id: int,
        *,
        confirmed_label: str,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE cycle_loads
                   SET confirmed_label = $2,
                       confirmed_at = now(),
                       confirmed_by_chat_id = $3
                 WHERE id = $1
             RETURNING id, appliance, entity_id, guessed_label, confirmed_label,
                       confirmed_at, ended_at
                """,
                cycle_load_id,
                confirmed_label,
                chat_id,
            )
        return dict(row) if row else None

    async def recent(
        self,
        *,
        appliance: str | None = None,
        limit: int = 20,
        only_confirmed: bool = False,
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        clauses: list[str] = []
        args: list[Any] = []
        if appliance is not None:
            args.append(appliance)
            clauses.append(f"appliance = ${len(args)}")
        if only_confirmed:
            clauses.append("confirmed_label IS NOT NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(int(limit), 200)))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, appliance, entity_id, started_at, ended_at,
                       duration_seconds, program, brand,
                       guessed_label, guessed_confidence, guessed_reasoning,
                       confirmed_label, confirmed_at
                  FROM cycle_loads
                  {where}
                  ORDER BY ended_at DESC
                  LIMIT ${len(args)}
                """,
                *args,
            )
        return [dict(r) for r in rows]

    async def confirmed_label_history(
        self, *, appliance: str, limit: int = 20
    ) -> list[str]:
        """Return the most recent N confirmed labels for an appliance.

        Used to bias inference toward the user's habitual cycle types.
        """
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT confirmed_label
                  FROM cycle_loads
                 WHERE appliance = $1
                   AND confirmed_label IS NOT NULL
                 ORDER BY confirmed_at DESC NULLS LAST
                 LIMIT $2
                """,
                appliance,
                max(1, min(int(limit), 100)),
            )
        return [r["confirmed_label"] for r in rows if r["confirmed_label"]]
