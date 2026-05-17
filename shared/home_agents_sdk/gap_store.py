"""Capability gap log.

Every time the router can't fulfill a user request — invalid capability,
dispatch raised, escalator exhausted iterations without resolution, chat
fell back for an action-verb prompt — we record a row here. The nightly
reflector mines unresolved gaps, clusters them by failure pattern, and
produces structured code_change proposals via ReflectionStore.

Architectural contract:
- WRITER: orchestrator router.py + escalator.py (synchronous, fail-open
  on DB unavailability so user replies don't depend on this).
- READER: orchestrator reflector.py nightly; surfaces on /admin/proposals.
- RESOLUTION: mark_resolved when reflector files a proposal that
  addresses the gap, OR when a human dismisses from the dashboard.

The fail-open guarantee is critical — if Postgres is briefly down, the
user gets an honest reply but the gap is silently dropped (logged as
warning so we know we lost telemetry). Better than blocking the reply.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from .telemetry import get_logger

logger = get_logger("home_agents_sdk.gap_store")


# Open-ended on purpose — new instrumentation will invent new categories.
# We document the known ones here so consumers have a stable vocabulary.
KNOWN_FAILURE_REASONS = {
    # router.py: classifier returned an agent/capability that isn't registered
    "invalid_capability",
    # router.py: registry.dispatch raised
    "dispatch_failed",
    # router.py: classifier picked personal_assistant.chat for an action verb
    # ("turn off", "reduce", "set", "increase", "open", "close", "play", etc.)
    "chat_fallback_for_action_verb",
    # escalator.py: ReAct loop hit max iterations without producing a tool call
    "escalator_max_iterations",
    # escalator.py: every tool the escalator tried returned an error
    "escalator_all_tools_errored",
    # escalator.py: 8b couldn't even propose a tool from the catalog
    "escalator_no_tool_proposed",
    # chat.py: chat tool detected an action verb and refused to fabricate
    "chat_refused_action_verb",
}


def _format_ts(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("created_at", "resolved_at"):
        if key in data:
            data[key] = _format_ts(data.get(key))
    for key in ("router_pick", "escalation_path"):
        if key in data:
            data[key] = _decode_json(data.get(key))
    return data


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class GapStore:
    def __init__(self, pool: Any | None) -> None:
        self.pool = pool

    @asynccontextmanager
    async def _connection(self, operation: str):
        if self.pool is None:
            logger.warning("gap_store_unavailable", operation=operation, reason="no_pool")
            yield None
            return
        try:
            async with self.pool.acquire() as conn:
                yield conn
        except Exception as exc:
            logger.warning("gap_store_unavailable", operation=operation, error=str(exc))
            yield None

    async def record_gap(
        self,
        *,
        user_text: str,
        failure_reason: str,
        router_pick: dict[str, Any] | None = None,
        escalation_path: list[dict[str, Any]] | None = None,
        user_reply: str | None = None,
        member_id: int | None = None,
        member_name: str | None = None,
    ) -> int | None:
        """Insert a gap row. Returns the inserted id, or None if the DB
        was unavailable. Callers should not depend on the return value
        because we fail-open — the gap is best-effort telemetry, not a
        request precondition."""
        if failure_reason not in KNOWN_FAILURE_REASONS:
            logger.warning(
                "gap_store_unknown_failure_reason",
                failure_reason=failure_reason,
            )
        async with self._connection("record_gap") as conn:
            if conn is None:
                return None
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO capability_gaps (
                        user_text, member_id, member_name,
                        router_pick, escalation_path,
                        failure_reason, user_reply
                    ) VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7)
                    RETURNING id
                    """,
                    user_text,
                    member_id,
                    member_name,
                    _json_dumps(router_pick) if router_pick is not None else None,
                    _json_dumps(escalation_path) if escalation_path is not None else None,
                    failure_reason,
                    user_reply,
                )
            except Exception as exc:
                logger.warning("gap_store_insert_failed", error=str(exc))
                return None
            inserted_id = int(row["id"]) if row else None
            logger.info(
                "capability_gap_recorded",
                id=inserted_id,
                failure_reason=failure_reason,
                user_text_preview=user_text[:120],
            )
            return inserted_id

    async def list_unresolved(self, limit: int = 200) -> list[dict[str, Any]]:
        """For the nightly reflector — get all unresolved gaps for
        clustering and proposal generation."""
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 200
        async with self._connection("list_unresolved") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(
                    """
                    SELECT id, user_text, member_id, member_name,
                           router_pick, escalation_path,
                           failure_reason, user_reply,
                           created_at
                    FROM capability_gaps
                    WHERE resolved = FALSE
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    limit,
                )
            except Exception as exc:
                logger.warning("gap_store_list_failed", error=str(exc))
                return []
            return [_row_dict(r) for r in rows]

    async def list_recent(
        self,
        *,
        limit: int = 50,
        include_resolved: bool = True,
    ) -> list[dict[str, Any]]:
        """For the admin dashboard — recent gaps, resolved or not."""
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        async with self._connection("list_recent") as conn:
            if conn is None:
                return []
            try:
                if include_resolved:
                    rows = await conn.fetch(
                        """
                        SELECT id, user_text, member_id, member_name,
                               router_pick, escalation_path,
                               failure_reason, user_reply,
                               resolved, proposal_id, resolution_note,
                               created_at, resolved_at
                        FROM capability_gaps
                        ORDER BY created_at DESC
                        LIMIT $1
                        """,
                        limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT id, user_text, member_id, member_name,
                               router_pick, escalation_path,
                               failure_reason, user_reply,
                               resolved, proposal_id, resolution_note,
                               created_at, resolved_at
                        FROM capability_gaps
                        WHERE resolved = FALSE
                        ORDER BY created_at DESC
                        LIMIT $1
                        """,
                        limit,
                    )
            except Exception as exc:
                logger.warning("gap_store_list_recent_failed", error=str(exc))
                return []
            return [_row_dict(r) for r in rows]

    async def mark_resolved(
        self,
        gap_id: int,
        *,
        proposal_id: int | None = None,
        note: str | None = None,
    ) -> bool:
        """Mark a gap resolved — used by the reflector when it files a
        proposal that should address it, and by the dashboard when a
        human dismisses a gap."""
        async with self._connection("mark_resolved") as conn:
            if conn is None:
                return False
            try:
                result = await conn.execute(
                    """
                    UPDATE capability_gaps
                    SET resolved = TRUE,
                        proposal_id = $2,
                        resolution_note = $3,
                        resolved_at = now()
                    WHERE id = $1 AND resolved = FALSE
                    """,
                    int(gap_id),
                    proposal_id,
                    note,
                )
            except Exception as exc:
                logger.warning(
                    "gap_store_mark_resolved_failed",
                    gap_id=gap_id,
                    error=str(exc),
                )
                return False
            # asyncpg's execute returns "UPDATE n" — anything > 0 means
            # we actually updated a row (not a no-op on an already
            # resolved gap).
            updated = result.startswith("UPDATE ") and result != "UPDATE 0"
            if updated:
                logger.info("capability_gap_resolved", id=gap_id, proposal_id=proposal_id)
            return updated

    async def count_by_failure_reason(self, *, hours: int = 168) -> dict[str, int]:
        """For the reflector's prompt context — how many of each
        failure_reason in the last week. Default 7 days."""
        try:
            hours = max(1, min(int(hours), 8760))
        except (TypeError, ValueError):
            hours = 168
        async with self._connection("count_by_failure_reason") as conn:
            if conn is None:
                return {}
            try:
                rows = await conn.fetch(
                    """
                    SELECT failure_reason, COUNT(*) AS n
                    FROM capability_gaps
                    WHERE created_at > now() - ($1 || ' hours')::interval
                    GROUP BY failure_reason
                    ORDER BY n DESC
                    """,
                    str(hours),
                )
            except Exception as exc:
                logger.warning("gap_store_count_failed", error=str(exc))
                return {}
            return {r["failure_reason"]: int(r["n"]) for r in rows}
