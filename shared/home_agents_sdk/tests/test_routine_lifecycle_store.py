"""Tests for RoutineLifecycleStore."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.routine_lifecycle_store import (
    DEFAULT_PROMOTION_THRESHOLD,
    RoutineLifecycleStore,
)


def _conn_with_tx() -> MagicMock:
    """Build a mock asyncpg connection that supports `async with
    conn.transaction()`."""
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    return conn


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    return pool


# ── No-pool path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_returns_safe_defaults_without_pool() -> None:
    store = RoutineLifecycleStore(pool=None)
    assert await store.list_suggested() == []
    assert await store.list_active() == []
    assert await store.list_dismissed() == []
    assert await store.history(routine_id=1) == []
    assert await store.record_action(1, "confirm") is None
    assert await store.stats() == {"suggested": 0, "active": 0, "dismissed": 0}


# ── Action validation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_action_rejects_unknown_action() -> None:
    store = RoutineLifecycleStore(_pool_with(_conn_with_tx()))
    with pytest.raises(ValueError):
        await store.record_action(1, "burn")


# ── Promotion flow ──────────────────────────────────────────────


def _make_action_recorder(*, confirms: int, dismiss_ts=None, override_ts=None):
    """Build a fetchrow/fetchval side-effect simulating an audit log
    with the given history shape."""
    state = {"after_insert_confirms": confirms}

    async def fetchrow(query: str, *args: Any) -> dict | None:
        q = " ".join(query.split())
        if "FROM routines WHERE id = $1 FOR UPDATE" in q:
            return {"id": args[0], "status": "suggested"}
        if "action = 'dismiss'" in q:
            return ({"created_at": dismiss_ts} if dismiss_ts else None)
        if "action = 'override'" in q:
            return ({"created_at": override_ts} if override_ts else None)
        # The final UPDATE ... RETURNING
        if "UPDATE routines" in q and "RETURNING" in q:
            new_status, confirmed_count = args[1], args[2]
            return {
                "id": args[0],
                "name": "x -> y",
                "status": new_status,
                "confirmed_count": confirmed_count,
                "promoted_at": datetime.now(UTC) if new_status == "active" else None,
                "dismissed_at": datetime.now(UTC) if new_status == "dismissed" else None,
                "updated_at": datetime.now(UTC),
            }
        return None

    async def fetchval(query: str, *args: Any) -> Any:
        return state["after_insert_confirms"]

    return fetchrow, fetchval


@pytest.mark.asyncio
async def test_confirms_below_threshold_stay_suggested() -> None:
    conn = _conn_with_tx()
    fetchrow, fetchval = _make_action_recorder(confirms=2)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    store = RoutineLifecycleStore(_pool_with(conn))
    result = await store.record_action(7, "confirm", source="dashboard")
    assert result is not None
    assert result["status"] == "suggested"
    assert result["confirmed_count"] == 2
    assert result["promoted_at"] is None


@pytest.mark.asyncio
async def test_confirms_at_threshold_promote_to_active() -> None:
    conn = _conn_with_tx()
    fetchrow, fetchval = _make_action_recorder(confirms=DEFAULT_PROMOTION_THRESHOLD)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    store = RoutineLifecycleStore(_pool_with(conn))
    result = await store.record_action(7, "confirm")
    assert result is not None
    assert result["status"] == "active"
    assert result["confirmed_count"] == DEFAULT_PROMOTION_THRESHOLD
    assert result["promoted_at"] is not None


@pytest.mark.asyncio
async def test_dismiss_wins_over_prior_confirms() -> None:
    """Even with 5 confirms in history, a later dismiss demotes."""
    conn = _conn_with_tx()
    now = datetime.now(UTC)
    fetchrow, fetchval = _make_action_recorder(
        confirms=5, dismiss_ts=now,
    )
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    store = RoutineLifecycleStore(_pool_with(conn))
    result = await store.record_action(7, "dismiss")
    assert result is not None
    assert result["status"] == "dismissed"
    assert result["confirmed_count"] == 0
    assert result["dismissed_at"] is not None


@pytest.mark.asyncio
async def test_override_after_dismiss_resets_to_suggested() -> None:
    """User: 'wait actually keep this one' → override AFTER dismiss
    re-opens it, counting only confirms after the override."""
    conn = _conn_with_tx()
    now = datetime.now(UTC)
    dismiss_ts = now - timedelta(hours=2)
    override_ts = now - timedelta(hours=1)
    fetchrow, fetchval = _make_action_recorder(
        confirms=0,  # no confirms after override yet
        dismiss_ts=dismiss_ts,
        override_ts=override_ts,
    )
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    store = RoutineLifecycleStore(_pool_with(conn))
    result = await store.record_action(7, "override")
    assert result is not None
    assert result["status"] == "suggested"
    assert result["confirmed_count"] == 0
    assert result["promoted_at"] is None
    assert result["dismissed_at"] is None


@pytest.mark.asyncio
async def test_dismiss_after_override_still_wins() -> None:
    """dismiss > override means dismissed wins."""
    conn = _conn_with_tx()
    now = datetime.now(UTC)
    dismiss_ts = now
    override_ts = now - timedelta(hours=1)
    fetchrow, fetchval = _make_action_recorder(
        confirms=10, dismiss_ts=dismiss_ts, override_ts=override_ts,
    )
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    store = RoutineLifecycleStore(_pool_with(conn))
    result = await store.record_action(7, "dismiss")
    assert result is not None
    assert result["status"] == "dismissed"


@pytest.mark.asyncio
async def test_record_action_returns_none_for_unknown_routine() -> None:
    conn = _conn_with_tx()

    async def fetchrow(query: str, *args: Any) -> dict | None:
        if "FOR UPDATE" in query:
            return None
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock(return_value="")

    store = RoutineLifecycleStore(_pool_with(conn))
    assert await store.record_action(999, "confirm") is None


# ── Listing helpers ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_suggested_uses_status_filter() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = RoutineLifecycleStore(_pool_with(conn))
    await store.list_suggested(limit=10)
    query = conn.fetch.await_args.args[0]
    assert "WHERE status = 'suggested'" in query


@pytest.mark.asyncio
async def test_stats_returns_zero_filled_dict() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"status": "suggested", "n": 4},
        {"status": "active", "n": 1},
    ])
    store = RoutineLifecycleStore(_pool_with(conn))
    stats = await store.stats()
    assert stats == {"suggested": 4, "active": 1, "dismissed": 0}


@pytest.mark.asyncio
async def test_history_returns_audit_rows() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 1, "action": "confirm", "source": "dashboard",
            "note": None, "created_at": datetime.now(UTC),
        }
    ])
    store = RoutineLifecycleStore(_pool_with(conn))
    out = await store.history(7)
    assert len(out) == 1
    assert out[0]["action"] == "confirm"
