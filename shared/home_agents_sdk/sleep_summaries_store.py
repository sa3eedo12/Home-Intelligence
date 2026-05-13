"""Storage SDK for nightly sleep quality summaries."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import asyncpg


class SleepSummariesStore:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def insert_summary(
        self,
        *,
        household_member_id: int | None,
        night_of: date,
        asleep_at: datetime | None,
        awake_at: datetime | None,
        duration_minutes: int | None,
        deep_sleep_minutes: int | None,
        observer_likely_asleep_at: datetime | None,
        observer_likely_awake_at: datetime | None,
        interruptions: int,
        guessed_quality: str | None,
        guessed_reasoning: str | None,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sleep_summaries (
                    household_member_id, night_of, asleep_at, awake_at,
                    duration_minutes, deep_sleep_minutes,
                    observer_likely_asleep_at, observer_likely_awake_at,
                    interruptions, guessed_quality, guessed_reasoning
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (household_member_id, night_of) DO UPDATE SET
                    asleep_at = EXCLUDED.asleep_at,
                    awake_at = EXCLUDED.awake_at,
                    duration_minutes = EXCLUDED.duration_minutes,
                    deep_sleep_minutes = EXCLUDED.deep_sleep_minutes,
                    observer_likely_asleep_at = EXCLUDED.observer_likely_asleep_at,
                    observer_likely_awake_at = EXCLUDED.observer_likely_awake_at,
                    interruptions = EXCLUDED.interruptions,
                    guessed_quality = EXCLUDED.guessed_quality,
                    guessed_reasoning = EXCLUDED.guessed_reasoning
                RETURNING id
                """,
                household_member_id,
                night_of,
                asleep_at,
                awake_at,
                duration_minutes,
                deep_sleep_minutes,
                observer_likely_asleep_at,
                observer_likely_awake_at,
                max(0, int(interruptions or 0)),
                guessed_quality,
                guessed_reasoning,
            )
        return int(row["id"]) if row else None

    async def confirm(
        self,
        sleep_summary_id: int,
        *,
        confirmed_quality: str,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE sleep_summaries
                   SET confirmed_quality = $2,
                       confirmed_at = now(),
                       confirmed_by_chat_id = $3
                 WHERE id = $1
             RETURNING id, household_member_id, night_of, guessed_quality,
                       confirmed_quality, confirmed_at, duration_minutes
                """,
                sleep_summary_id,
                confirmed_quality,
                chat_id,
            )
        return dict(row) if row else None

    async def recent(
        self,
        *,
        household_member_id: int | None = None,
        limit: int = 20,
        only_confirmed: bool = False,
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        clauses: list[str] = []
        args: list[Any] = []
        if household_member_id is not None:
            args.append(int(household_member_id))
            clauses.append(f"household_member_id = ${len(args)}")
        if only_confirmed:
            clauses.append("confirmed_quality IS NOT NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(int(limit), 200)))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, household_member_id, night_of, asleep_at, awake_at,
                       duration_minutes, deep_sleep_minutes,
                       observer_likely_asleep_at, observer_likely_awake_at,
                       interruptions, guessed_quality, guessed_reasoning,
                       confirmed_quality, confirmed_at, confirmed_by_chat_id,
                       created_at
                  FROM sleep_summaries
                  {where}
                  ORDER BY night_of DESC, created_at DESC
                  LIMIT ${len(args)}
                """,
                *args,
            )
        return [dict(row) for row in rows]
