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
    "proactive_suggestion",
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

    async def list_recent_events(
        self,
        window_hours: int = 24,
        *,
        exclude_agents: tuple[str, ...] = (
            # Internal noise: dashboard_curator runs every 60s, observer.* events
            # are summarized elsewhere, data_science / __orchestrator__ housekeeping
            # don't add information for the reflector. Filtering at the SQL level
            # keeps the reflector's evidence set small and signal-rich.
            "dashboard_curator",
            "data_science",
            "__orchestrator__",
        ),
        exclude_capabilities: tuple[str, ...] = (
            "summarize_activity",
            "summarize_alerts",
            "agent_card",
            "reflector.run",
            "advisor.run",
        ),
    ) -> list[dict[str, Any]]:
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
                      AND NOT (
                        e.agent = ANY($2::text[])
                        OR e.agent LIKE 'observer.%'
                        OR e.agent LIKE '__orchestrator__%'
                      )
                      AND NOT (e.capability = ANY($3::text[]))
                      -- Suppress no-op "completed_successfully in 0 ms" heartbeats:
                      -- these are reactive triggers that fired but returned no
                      -- inference (e.g. a presence event that wasn't a return).
                      -- They drown out real evidence in pattern mining.
                      AND NOT (
                        e.summary LIKE '%completed successfully in 0 ms'
                        AND coalesce(e.payload->>'evidence', '') = ''
                      )
                    ORDER BY e.ts DESC
                    LIMIT 500
                    """,
                    hours,
                    list(exclude_agents),
                    list(exclude_capabilities),
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

    async def count_proposals(self, status: str | None = None) -> int:
        """Return how many proposals match ``status`` (or all if None).

        Used by the dashboard nav to render an at-a-glance "X to confirm"
        badge without paying the cost of fetching every row. Returns 0 on
        any DB error so a flaky pool can never break navigation.
        """
        async with self._connection("count_proposals") as conn:
            if conn is None:
                return 0
            try:
                value = await conn.fetchval(
                    """
                    SELECT COUNT(*)::int
                    FROM proposals
                    WHERE ($1::text IS NULL OR status = $1::text)
                    """,
                    status,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="count_proposals",
                    error=str(exc),
                )
                return 0
        return int(value or 0)

    async def proposal_dismissal_signal(
        self, *, kind: str, days: int = 14
    ) -> dict[str, int]:
        """Return {dismissed, accepted, auto_confirmed} counts for ``kind``
        in the last ``days`` days.

        Used by the proposal feedback loop: if the user keeps dismissing
        proposals of a given kind, the reflector should back off on
        emitting more of them. Same shape as
        ``AutoInferencesStore.correction_counts`` so the calling code
        looks symmetric across the two layers.
        """
        empty = {"dismissed": 0, "accepted": 0, "auto_confirmed": 0}
        if not kind or not kind.strip():
            return dict(empty)
        async with self._connection("proposal_dismissal_signal") as conn:
            if conn is None:
                return dict(empty)
            try:
                rows = await conn.fetch(
                    """
                    SELECT status, count(*)::int AS n
                      FROM proposals
                     WHERE kind = $1
                       AND status IN ('dismissed', 'accepted', 'auto_confirmed')
                       AND COALESCE(resolved_at, created_at) >= now()
                           - ($2::int * interval '1 day')
                     GROUP BY status
                    """,
                    kind.strip(),
                    max(1, min(days, 90)),
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="proposal_dismissal_signal",
                    error=str(exc),
                )
                return dict(empty)
        out = dict(empty)
        for row in rows:
            status = str(row["status"])
            if status in out:
                out[status] = int(row["n"])
        return out

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

    async def list_unrefined_code_change_proposals(
        self, *, max_age_days: int = 7, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List pending code_change proposals that haven't been
        refined yet. Used by the nightly reflector's refine phase to
        find candidates for 35B reprocessing.

        Limited to last max_age_days because refining ancient
        proposals isn't useful — if the user hasn't acted on a
        7-day-old proposal, refining it won't help. limit caps the
        nightly cost (each refinement is one 35B call).
        """
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 50
        async with self._connection("list_unrefined") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(
                    """
                    SELECT id, kind, title, rationale, confidence,
                           cost_estimate, impact_estimate, created_at
                    FROM proposals
                    WHERE status = 'pending'
                      AND kind = 'code_change'
                      AND refined_at IS NULL
                      AND created_at > now() - ($1 || ' days')::interval
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    str(max_age_days),
                    limit,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="list_unrefined_code_change_proposals",
                    error=str(exc),
                )
                return []
            return [_row_dict(r) for r in rows]

    async def refine_proposal(
        self,
        proposal_id: int,
        *,
        new_title: str | None = None,
        new_rationale: str | None = None,
        new_confidence: float | None = None,
        refinement_notes: str | None = None,
    ) -> bool:
        """Apply a 35B-produced refinement to an existing proposal.
        Preserves the original_rationale once (idempotent — won't
        overwrite if refine_proposal is called twice). Always stamps
        refined_at so the row is skipped on the next nightly pass.

        Returns True iff the row was updated."""
        async with self._connection("refine_proposal") as conn:
            if conn is None:
                return False
            try:
                result = await conn.execute(
                    """
                    UPDATE proposals
                    SET title = COALESCE($2, title),
                        rationale = COALESCE($3, rationale),
                        confidence = COALESCE($4, confidence),
                        original_rationale = COALESCE(original_rationale, rationale),
                        refinement_notes = $5,
                        refined_at = now()
                    WHERE id = $1
                      AND refined_at IS NULL
                    """,
                    int(proposal_id),
                    new_title,
                    new_rationale,
                    new_confidence,
                    refinement_notes,
                )
            except Exception as exc:
                logger.warning(
                    "reflection_store_query_failed",
                    operation="refine_proposal",
                    proposal_id=proposal_id,
                    error=str(exc),
                )
                return False
            return result.startswith("UPDATE ") and result != "UPDATE 0"

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
