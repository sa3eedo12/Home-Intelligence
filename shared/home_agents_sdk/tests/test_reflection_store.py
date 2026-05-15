from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.reflection_store import ReflectionStore


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    # asyncpg's pool.acquire() is synchronous — it returns a PoolAcquireContext.
    pool.acquire = MagicMock(return_value=cm)
    return pool


@pytest.mark.asyncio
async def test_list_recent_events_decodes_rows() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 7,
                "ts": datetime(2026, 1, 1, 7, 30, tzinfo=UTC),
                "agent": "washer",
                "capability": "cycle_complete",
                "summary": "Washer finished",
                "payload": '{"load":"sheets"}',
            }
        ]
    )
    store = ReflectionStore(_pool_with(conn))

    rows = await store.list_recent_events(window_hours=12)

    assert rows[0]["id"] == 7
    assert rows[0]["payload"] == {"load": "sheets"}
    assert rows[0]["ts"].startswith("2026-01-01T07:30:00")
    conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_brief_returns_inserted_id() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=42)
    store = ReflectionStore(_pool_with(conn))

    brief_id = await store.record_brief("headline", {"proposals": []})

    assert brief_id == 42
    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_proposals_filters_status() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": 1,
                "kind": "code_change",
                "title": "Add tests",
                "rationale": "Better confidence",
                "evidence_event_ids": [1, 2],
                "confidence": 0.8,
                "cost_estimate": "small",
                "impact_estimate": "safer deploys",
                "status": "pending",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "resolved_at": None,
                "delivery_channel": None,
                "rejected_at": None,
            }
        ]
    )
    store = ReflectionStore(_pool_with(conn))

    rows = await store.list_proposals(status="pending", limit=10)

    assert rows[0]["title"] == "Add tests"
    assert rows[0]["evidence_event_ids"] == [1, 2]
    assert conn.fetch.await_args.args[-2:] == ("pending", 10)


@pytest.mark.asyncio
async def test_add_update_profile_and_forget_execute_queries() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=9)
    conn.execute = AsyncMock(return_value="OK")
    store = ReflectionStore(_pool_with(conn))

    proposal_id = await store.add_proposal(
        kind="habit_inference",
        title="Prefers coffee at 7",
        evidence_event_ids=[1, 2, 3],
        confidence=0.96,
        status="auto_confirmed",
    )
    await store.update_proposal_status(proposal_id, "dismissed", channel="dashboard")
    await store.upsert_profile("wake_time", "07:00", 0.7, "test")
    await store.forget_profile("wake_time")

    assert proposal_id == 9
    conn.fetchval.assert_awaited_once()
    assert conn.execute.await_count == 3


@pytest.mark.asyncio
async def test_record_delivery_updates_columns() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    store = ReflectionStore(_pool_with(conn))

    await store.record_delivery(
        7,
        channel="github_issue",
        github_issue_url="https://github.com/o/r/issues/7",
        github_pr_url=None,
        error=None,
    )

    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert "delivery_channel = $2" in args[0]
    assert "dispatched_at = now()" in args[0]
    assert "github_issue_url = COALESCE($3, github_issue_url)" in args[0]
    assert "dispatch_error = COALESCE($5, dispatch_error)" in args[0]
    assert args[1:] == (
        7,
        "github_issue",
        "https://github.com/o/r/issues/7",
        None,
        None,
    )


@pytest.mark.asyncio
async def test_unavailable_pool_returns_empty_or_noop() -> None:
    store = ReflectionStore(None)

    assert await store.list_recent_events() == []
    assert await store.list_briefs() == []
    assert await store.list_proposals() == []
    assert await store.list_profile() == []
    assert await store.record_brief("x", {}) == 0
    assert await store.add_proposal(kind="code_change", title="x") == 0
    await store.update_proposal_status(1, "accepted")
    await store.record_delivery(1, channel="github_issue", error="github not configured")
    await store.upsert_profile("wake_time", "07:00", 0.5, "test")
    await store.forget_profile("wake_time")


@pytest.mark.asyncio
async def test_count_proposals_filters_by_status() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=12)
    store = ReflectionStore(_pool_with(conn))

    count = await store.count_proposals(status="pending")

    assert count == 12
    conn.fetchval.assert_awaited_once()
    assert conn.fetchval.await_args.args[-1] == "pending"


@pytest.mark.asyncio
async def test_count_proposals_no_filter_returns_total() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=37)
    store = ReflectionStore(_pool_with(conn))

    count = await store.count_proposals()

    assert count == 37
    assert conn.fetchval.await_args.args[-1] is None


@pytest.mark.asyncio
async def test_count_proposals_returns_zero_on_db_error() -> None:
    """Nav badge can never break navigation — return 0 instead of raising."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=Exception("connection lost"))
    store = ReflectionStore(_pool_with(conn))

    assert await store.count_proposals(status="pending") == 0


@pytest.mark.asyncio
async def test_count_proposals_returns_zero_when_no_pool() -> None:
    store = ReflectionStore(None)
    assert await store.count_proposals(status="pending") == 0


# ── Proposal dismissal-signal feedback loop ──────────────────────────────


@pytest.mark.asyncio
async def test_proposal_dismissal_signal_returns_per_status_breakdown() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {"status": "dismissed", "n": 6},
            {"status": "accepted", "n": 1},
        ]
    )
    store = ReflectionStore(_pool_with(conn))

    signal = await store.proposal_dismissal_signal(kind="code_change", days=14)

    assert signal == {"dismissed": 6, "accepted": 1, "auto_confirmed": 0}
    assert conn.fetch.await_args.args[-2:] == ("code_change", 14)


@pytest.mark.asyncio
async def test_proposal_dismissal_signal_returns_zeros_on_empty_kind() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = ReflectionStore(_pool_with(conn))

    signal = await store.proposal_dismissal_signal(kind="   ", days=14)

    assert signal == {"dismissed": 0, "accepted": 0, "auto_confirmed": 0}
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_proposal_dismissal_signal_returns_zeros_on_db_error() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception("db down"))
    store = ReflectionStore(_pool_with(conn))

    signal = await store.proposal_dismissal_signal(kind="cleanup_action")
    assert signal == {"dismissed": 0, "accepted": 0, "auto_confirmed": 0}


@pytest.mark.asyncio
async def test_proposal_dismissal_signal_clamps_days_window() -> None:
    """Bounded between 1 and 90 days so a malformed call can't query
    infinite history."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    store = ReflectionStore(_pool_with(conn))

    await store.proposal_dismissal_signal(kind="x", days=0)
    assert conn.fetch.await_args.args[-1] == 1
    await store.proposal_dismissal_signal(kind="x", days=9999)
    assert conn.fetch.await_args.args[-1] == 90
