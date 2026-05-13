"""Storage SDK for presence return-context guesses and confirmations."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import asyncpg


class PresenceReturnsStore:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def insert_return(
        self,
        *,
        household_member_id: int | None = None,
        entity_id: str | None,
        person: str | None,
        left_at: datetime | None,
        returned_at: datetime,
        away_minutes: int | None,
        guessed_context: str | None,
        guessed_confidence: float | None,
        guessed_reasoning: str | None,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO presence_returns (
                    household_member_id, entity_id, person, left_at, returned_at,
                    away_minutes, guessed_context, guessed_confidence, guessed_reasoning
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                household_member_id,
                entity_id,
                person,
                left_at,
                returned_at,
                away_minutes,
                guessed_context,
                guessed_confidence,
                guessed_reasoning,
            )
        return int(row["id"]) if row else None

    async def confirm(
        self,
        presence_return_id: int,
        context: str,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE presence_returns
                   SET confirmed_context = $2,
                       confirmed_at = now(),
                       confirmed_by_chat_id = $3
                 WHERE id = $1
             RETURNING id, household_member_id, entity_id, person, left_at,
                       returned_at, away_minutes, guessed_context,
                       confirmed_context, confirmed_at
                """,
                presence_return_id,
                context,
                chat_id,
            )
        return dict(row) if row else None

    async def recent(self, person: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        clauses: list[str] = []
        args: list[Any] = []
        if person:
            args.append(person)
            clauses.append(f"person = ${len(args)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(int(limit), 200)))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, household_member_id, entity_id, person, left_at,
                       returned_at, away_minutes, guessed_context,
                       guessed_confidence, guessed_reasoning, confirmed_context,
                       confirmed_at, confirmed_by_chat_id, created_at
                  FROM presence_returns
                  {where}
                 ORDER BY returned_at DESC
                 LIMIT ${len(args)}
                """,
                *args,
            )
        return [dict(r) for r in rows]

    async def last_left_at(self, entity_id: str | None) -> datetime | None:
        if not entity_id or not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ts, payload->>'since' AS since
                  FROM event_log
                 WHERE capability = 'presence.changed'
                   AND payload->>'entity_id' = $1
                   AND payload->>'state' = 'not_home'
                 ORDER BY ts DESC
                 LIMIT 1
                """,
                entity_id,
            )
        if not row:
            return None
        return _parse_datetime(row["since"]) or _parse_datetime(row["ts"])

    async def confirmed_context_history(
        self,
        person: str | None,
        limit_days: int = 30,
    ) -> list[tuple[int, int, str]]:
        if not self._ready or self.pool is None:
            return []
        bounded_days = max(1, min(int(limit_days), 365))
        user_tz = os.getenv("USER_TZ", "Asia/Dubai")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT EXTRACT(HOUR FROM returned_at AT TIME ZONE $3::text)::int
                           AS hour_of_day,
                       EXTRACT(DOW FROM returned_at AT TIME ZONE $3::text)::int
                           AS day_of_week,
                       confirmed_context
                  FROM presence_returns
                 WHERE confirmed_context IS NOT NULL
                   AND ($1::text IS NULL OR person = $1::text)
                   AND confirmed_at >= now() - ($2::int * interval '1 day')
                 ORDER BY confirmed_at DESC NULLS LAST
                 LIMIT 200
                """,
                person,
                bounded_days,
                user_tz,
            )
        return [
            (int(r["hour_of_day"]), int(r["day_of_week"]), str(r["confirmed_context"]))
            for r in rows
            if r["confirmed_context"]
        ]


def _parse_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
