from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.gap_store import KNOWN_FAILURE_REASONS, GapStore


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


@pytest.mark.asyncio
async def test_record_gap_returns_inserted_id() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 42})
    store = GapStore(_pool_with(conn))

    gap_id = await store.record_gap(
        user_text="reduce the bedroom temperature",
        failure_reason="invalid_capability",
        router_pick={"agent": "personal_assistant", "capability": "chat"},
        escalation_path=[{"iter": 1, "tool": "climate_status", "ok": False}],
        user_reply="I couldn't do that — logged for review.",
        member_id=1,
        member_name="Saeed",
    )

    assert gap_id == 42
    conn.fetchrow.assert_awaited_once()
    # Positional args: (query, user_text, member_id, member_name,
    # router_pick, escalation_path, failure_reason, user_reply)
    args = conn.fetchrow.await_args[0]
    assert isinstance(args[4], str)  # router_pick → json string
    assert "personal_assistant" in args[4]
    assert isinstance(args[5], str)  # escalation_path → json string


@pytest.mark.asyncio
async def test_record_gap_fail_open_on_db_error() -> None:
    """If Postgres is briefly down we MUST return None and log, never
    raise — gap recording is best-effort telemetry, the user reply must
    not depend on it."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=Exception("boom"))
    store = GapStore(_pool_with(conn))

    gap_id = await store.record_gap(
        user_text="anything", failure_reason="invalid_capability"
    )

    assert gap_id is None  # didn't raise


@pytest.mark.asyncio
async def test_record_gap_fail_open_no_pool() -> None:
    store = GapStore(pool=None)

    gap_id = await store.record_gap(
        user_text="anything", failure_reason="invalid_capability"
    )

    assert gap_id is None


@pytest.mark.asyncio
async def test_record_gap_unknown_reason_still_inserts() -> None:
    """Unknown failure_reason logs a warning but still inserts —
    instrumentation may add new categories before the constant catches
    up and we must not drop those signals."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 1})
    store = GapStore(_pool_with(conn))

    gap_id = await store.record_gap(
        user_text="x", failure_reason="some_new_category"
    )

    assert gap_id == 1


@pytest.mark.asyncio
async def test_list_unresolved_returns_only_unresolved() -> None:
    """The query has WHERE resolved = FALSE baked in — the test just
    pins the column projection contract."""
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 1,
                "user_text": "reduce bedroom temp",
                "member_id": 1,
                "member_name": "Saeed",
                "router_pick": '{"agent": "personal_assistant"}',
                "escalation_path": "[]",
                "failure_reason": "chat_fallback_for_action_verb",
                "user_reply": None,
                "created_at": datetime(2026, 5, 17, tzinfo=UTC),
            }
        ]
    )
    store = GapStore(_pool_with(conn))

    gaps = await store.list_unresolved(limit=100)

    assert len(gaps) == 1
    assert gaps[0]["id"] == 1
    # JSONB strings decoded to native types so reflector can iterate
    assert gaps[0]["router_pick"] == {"agent": "personal_assistant"}
    assert gaps[0]["escalation_path"] == []
    # Timestamps formatted as ISO strings for safe serialization
    assert gaps[0]["created_at"].startswith("2026-05-17")


@pytest.mark.asyncio
async def test_list_unresolved_clamps_limit() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = GapStore(_pool_with(conn))

    await store.list_unresolved(limit=99999)
    # positional args: (query, limit)
    args = conn.fetch.await_args[0]
    assert args[1] == 1000  # clamped


@pytest.mark.asyncio
async def test_mark_resolved_updates_row() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    store = GapStore(_pool_with(conn))

    ok = await store.mark_resolved(7, proposal_id=42, note="filed as code_change #42")

    assert ok is True


@pytest.mark.asyncio
async def test_mark_resolved_returns_false_when_already_resolved() -> None:
    """UPDATE 0 means the WHERE clause matched no rows — likely a race
    where two workers tried to resolve the same gap. Return False so
    callers can decide whether to log."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    store = GapStore(_pool_with(conn))

    ok = await store.mark_resolved(7)

    assert ok is False


@pytest.mark.asyncio
async def test_count_by_failure_reason_returns_counts() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {"failure_reason": "chat_fallback_for_action_verb", "n": 5},
            {"failure_reason": "invalid_capability", "n": 2},
        ]
    )
    store = GapStore(_pool_with(conn))

    counts = await store.count_by_failure_reason(hours=168)

    assert counts == {
        "chat_fallback_for_action_verb": 5,
        "invalid_capability": 2,
    }


def test_known_failure_reasons_documented() -> None:
    """If you add a new instrumentation point, add its failure_reason
    to KNOWN_FAILURE_REASONS. This test pins the current set so
    additions are explicit and reviewable."""
    assert KNOWN_FAILURE_REASONS == {
        "invalid_capability",
        "dispatch_failed",
        "chat_fallback_for_action_verb",
        "escalator_max_iterations",
        "escalator_all_tools_errored",
        "escalator_no_tool_proposed",
        "chat_refused_action_verb",
    }
