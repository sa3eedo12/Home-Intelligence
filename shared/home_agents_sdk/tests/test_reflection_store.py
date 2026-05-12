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
    pool.acquire = AsyncMock(return_value=cm)
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
async def test_unavailable_pool_returns_empty_or_noop() -> None:
    store = ReflectionStore(None)

    assert await store.list_recent_events() == []
    assert await store.list_briefs() == []
    assert await store.list_proposals() == []
    assert await store.list_profile() == []
    assert await store.record_brief("x", {}) == 0
    assert await store.add_proposal(kind="code_change", title="x") == 0
    await store.update_proposal_status(1, "accepted")
    await store.upsert_profile("wake_time", "07:00", 0.5, "test")
    await store.forget_profile("wake_time")
