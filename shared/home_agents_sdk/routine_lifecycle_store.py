"""Routine lifecycle store — suggested → confirmed × N → active, or dismissed.

Wraps the ``routines`` + ``routine_confirmations`` tables. Used by the
dashboard (Phase 6) to expose accept / dismiss buttons and by the
nightly miner to skip re-suggesting already-dismissed routines.

Promotion rule (defaults — tunable per-call):

- 3 ``confirm`` actions  → promote to ``active`` (set promoted_at)
- 1 ``dismiss`` action  → demote to ``dismissed`` (set dismissed_at);
  takes precedence over any prior confirms because user intent matters
- ``override`` action     → reset to ``suggested``; used when user wants
  to un-dismiss something they killed earlier

Counts come from routine_confirmations, not from routines.confirmed_count,
so the lifecycle stays correct even if the cache column drifts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg


DEFAULT_PROMOTION_THRESHOLD = 3
_VALID_ACTIONS = {"confirm", "dismiss", "override"}


class RoutineLifecycleStore:
    """suggested → active|dismissed promotion + audit log."""

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def list_suggested(self, limit: int = 50) -> list[dict[str, Any]]:
        """All routines awaiting user action — dashboard tile."""
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, steps, schedule, source, status,
                       confirmed_count, created_at, updated_at,
                       promoted_at, dismissed_at
                FROM routines
                WHERE status = 'suggested'
                ORDER BY created_at DESC
                LIMIT $1
                """,
                int(limit),
            )
        return [dict(r) for r in rows]

    async def list_active(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, steps, schedule, source, status,
                       confirmed_count, created_at, updated_at,
                       promoted_at, dismissed_at
                FROM routines
                WHERE status = 'active'
                ORDER BY promoted_at DESC NULLS LAST, updated_at DESC
                LIMIT $1
                """,
                int(limit),
            )
        return [dict(r) for r in rows]

    async def list_dismissed(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, source, status, dismissed_at, updated_at
                FROM routines
                WHERE status = 'dismissed'
                ORDER BY dismissed_at DESC NULLS LAST
                LIMIT $1
                """,
                int(limit),
            )
        return [dict(r) for r in rows]

    async def history(
        self, routine_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, action, source, note, created_at
                FROM routine_confirmations
                WHERE routine_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                int(routine_id), int(limit),
            )
        return [dict(r) for r in rows]

    async def record_action(
        self,
        routine_id: int,
        action: str,
        *,
        source: str | None = None,
        note: str | None = None,
        promotion_threshold: int = DEFAULT_PROMOTION_THRESHOLD,
    ) -> dict[str, Any] | None:
        """Insert the action then re-derive the routine's lifecycle state.

        Returns the routine's new state (or None on validation/error)
        so the caller can show the user what happened without a second
        query.
        """
        if not self._ready or self.pool is None:
            return None
        if action not in _VALID_ACTIONS:
            raise ValueError(
                f"action must be one of {_VALID_ACTIONS}, got {action!r}"
            )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                routine = await conn.fetchrow(
                    "SELECT id, status FROM routines WHERE id = $1 FOR UPDATE",
                    int(routine_id),
                )
                if routine is None:
                    return None

                await conn.execute(
                    """
                    INSERT INTO routine_confirmations(
                        routine_id, action, source, note
                    )
                    VALUES ($1, $2, $3, $4)
                    """,
                    int(routine_id), action, source, note,
                )

                # Re-derive the lifecycle from the audit log so the cache
                # column can never lie. Latest dismiss wins; then count
                # confirms after the most recent override (so the user
                # can reset the counter by overriding).
                dismiss_row = await conn.fetchrow(
                    """
                    SELECT created_at FROM routine_confirmations
                    WHERE routine_id = $1 AND action = 'dismiss'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    int(routine_id),
                )
                override_row = await conn.fetchrow(
                    """
                    SELECT created_at FROM routine_confirmations
                    WHERE routine_id = $1 AND action = 'override'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    int(routine_id),
                )

                if dismiss_row is not None and (
                    override_row is None
                    or dismiss_row["created_at"] > override_row["created_at"]
                ):
                    new_status = "dismissed"
                    confirms = 0
                else:
                    # Count confirms AFTER the latest override (or all if
                    # no override has ever happened).
                    if override_row is not None:
                        confirms = await conn.fetchval(
                            """
                            SELECT count(*)::int FROM routine_confirmations
                            WHERE routine_id = $1 AND action = 'confirm'
                              AND created_at > $2
                            """,
                            int(routine_id),
                            override_row["created_at"],
                        )
                    else:
                        confirms = await conn.fetchval(
                            """
                            SELECT count(*)::int FROM routine_confirmations
                            WHERE routine_id = $1 AND action = 'confirm'
                            """,
                            int(routine_id),
                        )
                    confirms = int(confirms or 0)
                    new_status = (
                        "active" if confirms >= int(promotion_threshold)
                        else "suggested"
                    )

                set_clauses = [
                    "status = $2",
                    "confirmed_count = $3",
                    "updated_at = now()",
                ]
                if new_status == "active":
                    set_clauses.append("promoted_at = COALESCE(promoted_at, now())")
                    set_clauses.append("dismissed_at = NULL")
                elif new_status == "dismissed":
                    set_clauses.append("dismissed_at = now()")
                else:  # suggested (reset path)
                    set_clauses.append("promoted_at = NULL")
                    set_clauses.append("dismissed_at = NULL")
                updated = await conn.fetchrow(
                    f"""
                    UPDATE routines
                    SET {', '.join(set_clauses)}
                    WHERE id = $1
                    RETURNING id, name, status, confirmed_count, promoted_at,
                              dismissed_at, updated_at
                    """,
                    int(routine_id), new_status, int(confirms),
                )
        return dict(updated) if updated else None

    async def stats(self) -> dict[str, int]:
        if not self._ready or self.pool is None:
            return {"suggested": 0, "active": 0, "dismissed": 0}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT status, count(*)::int AS n FROM routines
                GROUP BY status
                """
            )
        return {
            "suggested": 0, "active": 0, "dismissed": 0,
            **{r["status"]: r["n"] for r in rows},
        }
