"""Storage SDK for vacuum cleaning-run room coverage inference.

Used by ``agents.household_ops.tools.cleaning_runs`` to persist completed vacuum
runs, confirm the user's feedback, and derive the user's usual cleaned-room set
from recent history. The agent owns inference and user-facing wording; this
module only handles persistence and history aggregation.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from typing import Any

import asyncpg


class CleaningRunsStore:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def insert_run(
        self,
        *,
        entity_id: str | None,
        started_at: datetime | None,
        ended_at: datetime,
        duration_seconds: int | None,
        reported_rooms: list[str],
        expected_rooms: list[str],
        missed_rooms: list[str],
        guessed_status: str | None,
        guessed_reasoning: str | None,
        attributes_at_finish: dict[str, Any],
        event_log_id: int | None = None,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cleaning_runs (
                    event_log_id, entity_id, started_at, ended_at,
                    duration_seconds, reported_rooms, expected_rooms,
                    missed_rooms, guessed_status, guessed_reasoning,
                    attributes_at_finish
                )
                VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7,
                    $8, $9, $10,
                    $11::jsonb
                )
                RETURNING id
                """,
                event_log_id,
                entity_id,
                started_at,
                ended_at,
                duration_seconds,
                reported_rooms,
                expected_rooms,
                missed_rooms,
                guessed_status,
                guessed_reasoning,
                json.dumps(attributes_at_finish or {}),
            )
        return int(row["id"]) if row else None

    async def confirm(
        self,
        run_id: int,
        status: str,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE cleaning_runs
                   SET confirmed_status = $2,
                       confirmed_at = now(),
                       confirmed_by_chat_id = $3,
                       reported_rooms = CASE
                           WHEN $2 = 'full' THEN (
                               SELECT COALESCE(array_agg(room ORDER BY first_pos), ARRAY[]::text[])
                                 FROM (
                                     SELECT room, min(ord) AS first_pos
                                       FROM unnest(
                                           cleaning_runs.reported_rooms
                                           || cleaning_runs.missed_rooms
                                       ) WITH ORDINALITY AS u(room, ord)
                                      WHERE room <> ''
                                      GROUP BY room
                                 ) deduped
                           )
                           ELSE reported_rooms
                       END
                 WHERE id = $1
             RETURNING id, entity_id, reported_rooms, expected_rooms, missed_rooms,
                       guessed_status, confirmed_status, confirmed_at, ended_at
                """,
                run_id,
                status,
                chat_id,
            )
        return dict(row) if row else None

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event_log_id, entity_id, started_at, ended_at,
                       duration_seconds, reported_rooms, expected_rooms,
                       missed_rooms, guessed_status, guessed_reasoning,
                       confirmed_status, confirmed_at, confirmed_by_chat_id,
                       created_at
                  FROM cleaning_runs
                 ORDER BY ended_at DESC
                 LIMIT $1
                """,
                max(1, min(int(limit), 200)),
            )
        return [dict(r) for r in rows]

    async def typical_rooms(self, limit_history: int = 10) -> list[str]:
        """Return rooms that commonly appear in the recent cleaning history.

        A single historical run seeds the pattern. Once multiple runs exist, a
        room must appear in at least half of room-reporting runs and at least two
        runs, which lets occasional one-off rooms fade while repeated rooms drift
        into the expected set.
        """
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT reported_rooms
                  FROM cleaning_runs
                 WHERE cardinality(reported_rooms) > 0
                 ORDER BY ended_at DESC
                 LIMIT $1
                """,
                max(1, min(int(limit_history), 100)),
            )
        return self._typical_rooms_from_rows(rows)

    @classmethod
    def _typical_rooms_from_rows(cls, rows: list[Any]) -> list[str]:
        seen_per_run: list[list[str]] = []
        first_seen: dict[str, int] = {}
        for row in rows:
            rooms = cls._row_value(row, "reported_rooms") or []
            normalised = cls._normalise_rooms(rooms)
            if not normalised:
                continue
            seen_per_run.append(normalised)
            for room in normalised:
                first_seen.setdefault(room, len(first_seen))

        if not seen_per_run:
            return []

        counts: Counter[str] = Counter()
        for rooms in seen_per_run:
            counts.update(set(rooms))

        run_count = len(seen_per_run)
        threshold = 1 if run_count == 1 else max(2, math.ceil(run_count * 0.5))
        return sorted(
            [room for room, count in counts.items() if count >= threshold],
            key=lambda room: (-counts[room], first_seen[room]),
        )

    @staticmethod
    def _row_value(row: Any, key: str) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _normalise_rooms(raw_rooms: Any) -> list[str]:
        if isinstance(raw_rooms, str):
            values: list[Any] = raw_rooms.split(",")
        elif isinstance(raw_rooms, (list, tuple, set)):
            values = list(raw_rooms)
        else:
            return []

        rooms: list[str] = []
        seen: set[str] = set()
        for raw in values:
            room = " ".join(str(raw).strip().casefold().replace("_", " ").split())
            if room and room not in seen:
                rooms.append(room)
                seen.add(room)
        return rooms
