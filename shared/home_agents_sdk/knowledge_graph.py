from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

from .event_log import row_to_event

_THING_COLUMNS = (
    "id, type, friendly_name, attributes, ha_entity_ids, photo_path, confidence, "
    "learned_at, last_confirmed_at, source"
)
_HABIT_COLUMNS = (
    "id, subject, pattern, frequency, confidence, last_observed_at, source, created_at"
)
_PREFERENCE_COLUMNS = "key, value, confidence, source, updated_at"
_ROUTINE_COLUMNS = "id, name, steps, schedule, last_run_at, source, created_at"

_SELECT_COLUMNS = {
    "things": _THING_COLUMNS,
    "habits": _HABIT_COLUMNS,
    "preferences": _PREFERENCE_COLUMNS,
    "routines": _ROUTINE_COLUMNS,
}
_JSON_FIELDS = {"attributes", "pattern", "value", "steps", "schedule", "payload"}
_TIMESTAMP_FIELDS = {
    "learned_at",
    "last_confirmed_at",
    "last_observed_at",
    "created_at",
    "updated_at",
    "last_run_at",
}
_PATCH_FIELDS = {
    "things": {
        "type",
        "friendly_name",
        "attributes",
        "ha_entity_ids",
        "photo_path",
        "confidence",
        "source",
    },
    "habits": {"subject", "pattern", "frequency", "confidence", "last_observed_at", "source"},
    "preferences": {"value", "confidence", "source"},
    "routines": {"name", "steps", "schedule", "last_run_at", "source"},
}


def _json_arg(value: Any, default: Any = None) -> str:
    if value is None:
        value = default
    return json.dumps(value, default=str)


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _format_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _format_value(_decode_json(val)) for key, val in value.items()}
    if isinstance(value, list):
        return [_format_value(_decode_json(item)) for item in value]
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for field, value in list(data.items()):
        if field in _JSON_FIELDS:
            value = _decode_json(value)
        data[field] = _format_value(value)
    return data


def _command_count(status: str | None) -> int:
    if not status:
        return 0
    try:
        return int(status.rsplit(" ", maxsplit=1)[-1])
    except ValueError:
        return 0


class KnowledgeGraph:
    """Postgres-backed registry of household things, habits, preferences, and routines."""

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    async def list_things(self, type: str | None = None) -> list[dict[str, Any]]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_THING_COLUMNS}
                FROM things
                WHERE ($1::text IS NULL OR type = $1::text)
                ORDER BY friendly_name, id
                """,
                type,
            )
        return [_row_to_dict(row) for row in rows]

    async def get_thing(self, thing_id: int) -> dict[str, Any] | None:
        return await self._fetch_one("things", thing_id)

    async def put_thing(
        self,
        *,
        type: str,
        friendly_name: str,
        attributes: dict[str, Any] | None = None,
        ha_entity_ids: list[str] | None = None,
        photo_path: str | None = None,
        confidence: float = 0.0,
        source: str | None = None,
        thing_id: int | None = None,
    ) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            if thing_id is None:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO things(
                        type, friendly_name, attributes, ha_entity_ids, photo_path,
                        confidence, source
                    )
                    VALUES ($1, $2, $3::jsonb, $4::text[], $5, $6, $7)
                    RETURNING {_THING_COLUMNS}
                    """,
                    type,
                    friendly_name,
                    _json_arg(attributes, {}),
                    ha_entity_ids or [],
                    photo_path,
                    confidence,
                    source,
                )
            else:
                row = await conn.fetchrow(
                    f"""
                    UPDATE things
                    SET type = $2,
                        friendly_name = $3,
                        attributes = $4::jsonb,
                        ha_entity_ids = $5::text[],
                        photo_path = $6,
                        confidence = $7,
                        source = $8
                    WHERE id = $1
                    RETURNING {_THING_COLUMNS}
                    """,
                    thing_id,
                    type,
                    friendly_name,
                    _json_arg(attributes, {}),
                    ha_entity_ids or [],
                    photo_path,
                    confidence,
                    source,
                )
        return _row_to_dict(row) if row else None

    async def forget_thing(self, thing_id: int) -> bool:
        return await self._delete_by_id("things", thing_id)

    async def confirm_thing(self, thing_id: int) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE things
                SET last_confirmed_at = now()
                WHERE id = $1
                RETURNING {_THING_COLUMNS}
                """,
                thing_id,
            )
        return _row_to_dict(row) if row else None

    async def list_habits(self, subject: str | None = None) -> list[dict[str, Any]]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_HABIT_COLUMNS}
                FROM habits
                WHERE ($1::text IS NULL OR subject = $1::text)
                ORDER BY subject, id
                """,
                subject,
            )
        return [_row_to_dict(row) for row in rows]

    async def put_habit(
        self,
        *,
        subject: str,
        pattern: dict[str, Any],
        frequency: str | None = None,
        confidence: float = 0.0,
        last_observed_at: str | None = None,
        source: str | None = None,
        habit_id: int | None = None,
    ) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            if habit_id is None:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO habits(
                        subject, pattern, frequency, confidence, last_observed_at, source
                    )
                    VALUES ($1, $2::jsonb, $3, $4, $5::timestamptz, $6)
                    RETURNING {_HABIT_COLUMNS}
                    """,
                    subject,
                    _json_arg(pattern, {}),
                    frequency,
                    confidence,
                    last_observed_at,
                    source,
                )
            else:
                row = await conn.fetchrow(
                    f"""
                    UPDATE habits
                    SET subject = $2,
                        pattern = $3::jsonb,
                        frequency = $4,
                        confidence = $5,
                        last_observed_at = $6::timestamptz,
                        source = $7
                    WHERE id = $1
                    RETURNING {_HABIT_COLUMNS}
                    """,
                    habit_id,
                    subject,
                    _json_arg(pattern, {}),
                    frequency,
                    confidence,
                    last_observed_at,
                    source,
                )
        return _row_to_dict(row) if row else None

    async def forget_habit(self, habit_id: int) -> bool:
        return await self._delete_by_id("habits", habit_id)

    async def confirm_habit(self, habit_id: int) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE habits
                SET last_observed_at = now()
                WHERE id = $1
                RETURNING {_HABIT_COLUMNS}
                """,
                habit_id,
            )
        return _row_to_dict(row) if row else None

    async def list_preferences(self) -> list[dict[str, Any]]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_PREFERENCE_COLUMNS}
                FROM preferences
                ORDER BY key
                """
            )
        return [_row_to_dict(row) for row in rows]

    async def get_preference(self, key: str) -> dict[str, Any] | None:
        return await self._fetch_one("preferences", key)

    async def put_preference(
        self,
        *,
        key: str,
        value: Any,
        confidence: float = 0.0,
        source: str | None = None,
    ) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO preferences(key, value, confidence, source, updated_at)
                VALUES ($1, $2::jsonb, $3, $4, now())
                ON CONFLICT (key)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    confidence = EXCLUDED.confidence,
                    source = EXCLUDED.source,
                    updated_at = now()
                RETURNING {_PREFERENCE_COLUMNS}
                """,
                key,
                _json_arg(value, {}),
                confidence,
                source,
            )
        return _row_to_dict(row) if row else None

    async def forget_preference(self, key: str) -> bool:
        if self.pool is None:
            return False
        async with self.pool.acquire() as conn:
            status = await conn.execute("DELETE FROM preferences WHERE key = $1", key)
        return _command_count(status) > 0

    async def confirm_preference(self, key: str) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE preferences
                SET updated_at = now()
                WHERE key = $1
                RETURNING {_PREFERENCE_COLUMNS}
                """,
                key,
            )
        return _row_to_dict(row) if row else None

    async def list_routines(self) -> list[dict[str, Any]]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_ROUTINE_COLUMNS}
                FROM routines
                ORDER BY name, id
                """
            )
        return [_row_to_dict(row) for row in rows]

    async def put_routine(
        self,
        *,
        name: str,
        steps: list[Any] | dict[str, Any],
        schedule: dict[str, Any] | None = None,
        last_run_at: str | None = None,
        source: str | None = None,
        routine_id: int | None = None,
    ) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            if routine_id is None:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO routines(name, steps, schedule, last_run_at, source)
                    VALUES ($1, $2::jsonb, $3::jsonb, $4::timestamptz, $5)
                    ON CONFLICT (name)
                    DO UPDATE SET
                        steps = EXCLUDED.steps,
                        schedule = EXCLUDED.schedule,
                        last_run_at = EXCLUDED.last_run_at,
                        source = EXCLUDED.source
                    RETURNING {_ROUTINE_COLUMNS}
                    """,
                    name,
                    _json_arg(steps, []),
                    _json_arg(schedule),
                    last_run_at,
                    source,
                )
            else:
                row = await conn.fetchrow(
                    f"""
                    UPDATE routines
                    SET name = $2,
                        steps = $3::jsonb,
                        schedule = $4::jsonb,
                        last_run_at = $5::timestamptz,
                        source = $6
                    WHERE id = $1
                    RETURNING {_ROUTINE_COLUMNS}
                    """,
                    routine_id,
                    name,
                    _json_arg(steps, []),
                    _json_arg(schedule),
                    last_run_at,
                    source,
                )
        return _row_to_dict(row) if row else None

    async def forget_routine(self, routine_id: int) -> bool:
        return await self._delete_by_id("routines", routine_id)

    async def confirm_routine(self, routine_id: int) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE routines
                SET last_run_at = now()
                WHERE id = $1
                RETURNING {_ROUTINE_COLUMNS}
                """,
                routine_id,
            )
        return _row_to_dict(row) if row else None

    async def patch_row(
        self,
        table: str,
        row_id: int | str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.pool is None or table not in _PATCH_FIELDS:
            return None
        updates = {key: value for key, value in fields.items() if key in _PATCH_FIELDS[table]}
        if not updates:
            return await self._fetch_one(table, row_id)

        values: list[Any] = [row_id]
        assignments: list[str] = []
        for index, (field, value) in enumerate(updates.items(), start=2):
            if field in _JSON_FIELDS:
                assignments.append(f"{field} = ${index}::jsonb")
                values.append(_json_arg(value))
            elif field == "ha_entity_ids":
                assignments.append(f"{field} = ${index}::text[]")
                values.append(value or [])
            elif field == "confidence":
                assignments.append(f"{field} = ${index}::real")
                values.append(value)
            elif field in _TIMESTAMP_FIELDS:
                assignments.append(f"{field} = ${index}::timestamptz")
                values.append(value)
            else:
                assignments.append(f"{field} = ${index}")
                values.append(value)

        where = "key = $1" if table == "preferences" else "id = $1"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE {table}
                SET {", ".join(assignments)}
                WHERE {where}
                RETURNING {_SELECT_COLUMNS[table]}
                """,
                *values,
            )
        return _row_to_dict(row) if row else None

    async def evidence_for(self, table: str, row_id: int | str) -> list[dict[str, Any]]:
        if self.pool is None or table not in _SELECT_COLUMNS:
            return []
        async with self.pool.acquire() as conn:
            identifier = await self._identifier_for(conn, table, row_id)
            if not identifier:
                return []
            rows = await conn.fetch(
                """
                SELECT id, ts, agent, capability, summary, payload
                FROM event_log
                WHERE summary ILIKE '%' || $1::text || '%'
                ORDER BY ts DESC
                LIMIT 20
                """,
                identifier,
            )
        return [row_to_event(row) for row in rows]

    async def _fetch_one(self, table: str, row_id: int | str) -> dict[str, Any] | None:
        if self.pool is None or table not in _SELECT_COLUMNS:
            return None
        where = "key = $1" if table == "preferences" else "id = $1"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_SELECT_COLUMNS[table]} FROM {table} WHERE {where}",
                row_id,
            )
        return _row_to_dict(row) if row else None

    async def _delete_by_id(self, table: str, row_id: int) -> bool:
        if self.pool is None:
            return False
        async with self.pool.acquire() as conn:
            status = await conn.execute(f"DELETE FROM {table} WHERE id = $1", row_id)
        return _command_count(status) > 0

    async def _identifier_for(self, conn: Any, table: str, row_id: int | str) -> str | None:
        if table == "things":
            row = await conn.fetchrow(
                "SELECT friendly_name AS identifier FROM things WHERE id = $1",
                row_id,
            )
        elif table == "habits":
            row = await conn.fetchrow(
                "SELECT subject AS identifier FROM habits WHERE id = $1",
                row_id,
            )
        elif table == "preferences":
            row = await conn.fetchrow(
                "SELECT key AS identifier FROM preferences WHERE key = $1",
                row_id,
            )
        elif table == "routines":
            row = await conn.fetchrow(
                "SELECT name AS identifier FROM routines WHERE id = $1",
                row_id,
            )
        else:
            return None
        if not row:
            return None
        return str(dict(row).get("identifier") or "").strip() or None
