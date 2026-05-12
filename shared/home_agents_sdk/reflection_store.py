from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from .telemetry import get_logger

logger = get_logger("home_agents_sdk.reflection_store")


PROPOSAL_KINDS = {
    "code_change",
    "habit_inference",
    "preference_inference",
    "routine_inference",
    "cleanup_action",
    "household_inference",
}
ACTION_PROPOSAL_KINDS = {"suggested_action", "auto_action"}
_ALLOWED_PROPOSAL_KINDS = PROPOSAL_KINDS | ACTION_PROPOSAL_KINDS
PROPOSAL_STATUSES = {"pending", "accepted", "dismissed", "expired", "auto_confirmed"}


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _format_ts(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in (
        "ts",
        "generated_at",
        "sent_at",
        "created_at",
        "resolved_at",
        "rejected_at",
        "dispatched_at",
    ):
        if key in data:
            data[key] = _format_ts(data.get(key))
    for key in ("payload", "body_json", "value"):
        if key in data:
            data[key] = _decode_json(data.get(key))
    if "evidence_event_ids" in data and data["evidence_event_ids"] is None:
        data["evidence_event_ids"] = []
    return data


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _bounded_limit(limit: int, *, default: int, high: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, high))


class ReflectionStore:
    def __init__(self, pool: Any | None) -> None:
        self.pool = pool

    @asynccontextmanager
    async def _connection(self, operation: str):
        if self.pool is None:
            logger.warning("reflection_store_unavailable", operation=operation, reason="no_pool")
            yield None
            return
        try:
            async with self.pool.acquire() as conn:
                yield conn
        except Exception as exc:
            logger.warning("reflection_store_unavailable", operation=operation, error=str(exc))
            yield None

    async def list_recent_events(self, window_hours: int = 24) -> list[dict[str, Any]]:
        try:
            hours = max(1, min(int(window_hours), 24 * 14))
        except (TypeError, ValueError):
            hours = 24
        async with self._connection("list_recent_events") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(
                    """
                    SELECT e.id, e.ts, e.agent, e.capability, e.summary, e.payload
                    FROM event_log e
                    WHERE e.ts >= now() - ($1::int * interval '1 hour')
                    ORDER BY e.ts DESC
                    LIMIT 500
                    """,
                    hours,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="list_recent_events",
                    error=str(exc),
                )
                return []
        return [_row_dict(row) for row in rows]

    async def record_brief(self, summary: str, body: dict[str, Any]) -> int:
        async with self._connection("record_brief") as conn:
            if conn is None:
                return 0
            try:
                brief_id = await conn.fetchval(
                    """
                    INSERT INTO morning_brief(summary, body_json)
                    VALUES ($1, $2::jsonb)
                    RETURNING id
                    """,
                    summary,
                    _json_dumps(body),
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="record_brief",
                    error=str(exc),
                )
                return 0
        return int(brief_id or 0)

    async def list_briefs(self, limit: int = 30) -> list[dict[str, Any]]:
        bounded = _bounded_limit(limit, default=30, high=200)
        async with self._connection("list_briefs") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(
                    """
                    SELECT id, generated_at, summary, body_json, sent_at
                    FROM morning_brief
                    ORDER BY generated_at DESC
                    LIMIT $1
                    """,
                    bounded,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="list_briefs",
                    error=str(exc),
                )
                return []
        return [_row_dict(row) for row in rows]

    async def list_proposals(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        bounded = _bounded_limit(limit, default=50, high=500)
        async with self._connection("list_proposals") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(
                    """
                    SELECT id, kind, title, rationale, evidence_event_ids, confidence,
                           cost_estimate, impact_estimate, status, created_at, resolved_at,
                           delivery_channel, rejected_at, for_member_id, github_issue_url,
                           github_pr_url, dispatched_at, dispatch_error
                    FROM proposals
                    WHERE ($1::text IS NULL OR status = $1::text)
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    status,
                    bounded,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="list_proposals",
                    error=str(exc),
                )
                return []
        return [_row_dict(row) for row in rows]

    async def add_proposal(
        self,
        *,
        kind: str,
        title: str,
        rationale: str | None = None,
        evidence_event_ids: list[int] | None = None,
        confidence: float = 0.0,
        cost_estimate: str | None = None,
        impact_estimate: str | None = None,
        status: str = "pending",
        delivery_channel: str | None = None,
        for_member_id: int | None = None,
    ) -> int:
        if kind not in _ALLOWED_PROPOSAL_KINDS:
            logger.warning("reflection_store_invalid_proposal_kind", kind=kind)
            return 0
        if status not in PROPOSAL_STATUSES:
            logger.warning("reflection_store_invalid_proposal_status", status=status)
            return 0
        event_ids = [int(event_id) for event_id in (evidence_event_ids or [])]
        async with self._connection("add_proposal") as conn:
            if conn is None:
                return 0
            try:
                proposal_id = await conn.fetchval(
                    """
                    INSERT INTO proposals(
                        kind, title, rationale, evidence_event_ids, confidence,
                        cost_estimate, impact_estimate, status, delivery_channel,
                        for_member_id, resolved_at
                    )
                    VALUES ($1, $2, $3, $4::int[], $5, $6, $7, $8, $9, $10,
                            CASE WHEN $8 IN ('accepted', 'dismissed', 'expired', 'auto_confirmed')
                                 THEN now() ELSE NULL END)
                    RETURNING id
                    """,
                    kind,
                    title,
                    rationale,
                    event_ids,
                    float(confidence),
                    cost_estimate,
                    impact_estimate,
                    status,
                    delivery_channel,
                    for_member_id,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="add_proposal",
                    error=str(exc),
                )
                return 0
        return int(proposal_id or 0)

    async def update_proposal_status(
        self, proposal_id: int, status: str, channel: str | None = None
    ) -> None:
        if status not in PROPOSAL_STATUSES:
            logger.warning("reflection_store_invalid_proposal_status", status=status)
            return
        async with self._connection("update_proposal_status") as conn:
            if conn is None:
                return
            try:
                await conn.execute(
                    """
                    UPDATE proposals
                    SET status = $2,
                        delivery_channel = COALESCE($3, delivery_channel),
                        resolved_at = CASE
                            WHEN $2 IN ('accepted', 'dismissed', 'expired', 'auto_confirmed')
                            THEN COALESCE(resolved_at, now())
                            ELSE resolved_at
                        END,
                        rejected_at = CASE
                            WHEN $2 = 'dismissed' THEN COALESCE(rejected_at, now())
                            ELSE rejected_at
                        END
                    WHERE id = $1
                    """,
                    int(proposal_id),
                    status,
                    channel,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="update_proposal_status",
                    error=str(exc),
                )

    async def record_delivery(
        self,
        proposal_id: int,
        *,
        channel: str,
        github_issue_url: str | None = None,
        github_pr_url: str | None = None,
        error: str | None = None,
    ) -> None:
        if channel not in {"clipboard", "github_issue", "copilot_dispatch"}:
            logger.warning("reflection_store_invalid_delivery_channel", channel=channel)
            return
        async with self._connection("record_delivery") as conn:
            if conn is None:
                return
            try:
                await conn.execute(
                    """
                    UPDATE proposals
                    SET delivery_channel = $2,
                        dispatched_at = now(),
                        github_issue_url = COALESCE($3, github_issue_url),
                        github_pr_url = COALESCE($4, github_pr_url),
                        dispatch_error = COALESCE($5, dispatch_error)
                    WHERE id = $1
                    """,
                    int(proposal_id),
                    channel,
                    github_issue_url,
                    github_pr_url,
                    error,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="record_delivery",
                    error=str(exc),
                )

    async def upsert_profile(
        self, key: str, value: Any, confidence: float, source: str
    ) -> None:
        clean_key = key.strip()
        if not clean_key:
            return
        async with self._connection("upsert_profile") as conn:
            if conn is None:
                return
            try:
                await conn.execute(
                    """
                    INSERT INTO user_profile(key, value, confidence, source, updated_at)
                    VALUES ($1, $2::jsonb, $3, $4, now())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        confidence = EXCLUDED.confidence,
                        source = EXCLUDED.source,
                        updated_at = now()
                    """,
                    clean_key,
                    _json_dumps(value),
                    float(confidence),
                    source,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="upsert_profile",
                    error=str(exc),
                )

    async def list_profile(self) -> list[dict[str, Any]]:
        async with self._connection("list_profile") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(
                    """
                    SELECT key, value, confidence, source, last_confirmed_at, updated_at
                    FROM user_profile
                    ORDER BY key ASC
                    """
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="list_profile",
                    error=str(exc),
                )
                return []
        return [_row_dict(row) for row in rows]

    async def forget_profile(self, key: str) -> None:
        clean_key = key.strip()
        if not clean_key:
            return
        async with self._connection("forget_profile") as conn:
            if conn is None:
                return
            try:
                await conn.execute("DELETE FROM user_profile WHERE key = $1", clean_key)
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="forget_profile",
                    error=str(exc),
                )
