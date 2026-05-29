"""Tests for ChoreStore — template seeding, log + due computation."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.chore_store import (
    DEFAULT_TEMPLATES,
    ChoreStatus,
    ChoreStore,
    _bucket,
)


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    return pool


# ── Bucket labels (pure-logic helper) ────────────────────────────


def test_bucket_overdue_when_past_grace() -> None:
    assert _bucket(3, grace_days=1) == "overdue"
    assert _bucket(2, grace_days=1) == "overdue"


def test_bucket_due_today_for_today_and_within_grace() -> None:
    # grace_days=1 means: 0 days late = due_today, 1 day late = due_today,
    # 2+ days late = overdue.
    assert _bucket(0, grace_days=1) == "due_today"
    assert _bucket(1, grace_days=1) == "due_today"


def test_bucket_soon_for_upcoming_within_2_days() -> None:
    assert _bucket(-1, grace_days=1) == "soon"
    assert _bucket(-2, grace_days=1) == "soon"


def test_bucket_recent_for_further_out() -> None:
    assert _bucket(-3, grace_days=1) == "recent"
    assert _bucket(-7, grace_days=1) == "recent"


# ── No-pool safe defaults ────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_returns_safe_defaults_without_pool() -> None:
    store = ChoreStore(pool=None)
    assert await store.seed_defaults() == 0
    assert await store.list_templates() == []
    assert await store.list_status() == []
    assert await store.upsert_template(name="x", cadence_days=7) is None
    assert await store.log_completion(template_id=1) is None
    assert await store.log_by_name("foo") is None


# ── Seed defaults ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_defaults_only_inserts_when_table_empty() -> None:
    conn = MagicMock()
    # Empty table on first call, so we insert all defaults
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    store = ChoreStore(_pool_with(conn))
    inserted = await store.seed_defaults()
    assert inserted == len(DEFAULT_TEMPLATES)
    # Verify each default's INSERT was issued
    assert conn.execute.await_count == len(DEFAULT_TEMPLATES)


@pytest.mark.asyncio
async def test_seed_defaults_idempotent_when_rows_exist() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=5)  # already populated
    conn.execute = AsyncMock(return_value="INSERT 0 0")
    store = ChoreStore(_pool_with(conn))
    inserted = await store.seed_defaults()
    assert inserted == 0
    conn.execute.assert_not_called()


def test_default_templates_have_required_fields() -> None:
    """Pin the seed list — every template needs name, category,
    cadence_days, and a description."""
    for t in DEFAULT_TEMPLATES:
        assert t["name"]
        assert t["category"]
        assert t["cadence_days"] > 0
        assert t.get("description")


# ── Log completion ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_completion_returns_new_id() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 42})
    store = ChoreStore(_pool_with(conn))
    log_id = await store.log_completion(
        template_id=7, member_id=2, source="telegram", note="vacuumed living room"
    )
    assert log_id == 42
    args = conn.fetchrow.await_args.args
    assert args[1] == 7
    assert args[3] == 2
    assert args[4] == "telegram"


@pytest.mark.asyncio
async def test_log_by_name_fuzzy_matches() -> None:
    """User types 'vacuum' → finds the 'Vacuum the house' template."""
    conn = MagicMock()
    # Template lookup returns the matching id, then log_completion inserts
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 1},                # template match
        {"id": 100},              # log insert
    ])
    store = ChoreStore(_pool_with(conn))
    tid = await store.log_by_name("vacuum")
    assert tid == 1


@pytest.mark.asyncio
async def test_log_by_name_returns_none_when_no_match() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    store = ChoreStore(_pool_with(conn))
    assert await store.log_by_name("nonsensical-chore-xyz") is None


# ── list_status with computed due / overdue ─────────────────────


@pytest.mark.asyncio
async def test_list_status_computes_overdue_for_old_completion() -> None:
    today = date(2026, 5, 29)
    # vacuum done 10 days ago, cadence 7d, grace 2d → 3 days overdue
    long_ago = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 1, "name": "Vacuum the house", "category": "cleaning",
            "cadence_days": 7, "grace_days": 2,
            "auto_detect_kind": "vacuum", "auto_detect_entity": None,
            "description": "Run the vacuum",
            "last_done_at": long_ago, "last_done_by": 2,
        }
    ])
    store = ChoreStore(_pool_with(conn))
    status = await store.list_status(
        now=datetime(2026, 5, 29, 12, tzinfo=UTC)
    )
    assert len(status) == 1
    s = status[0]
    assert s.template_id == 1
    assert s.last_done_at == long_ago
    assert s.next_due_on == date(2026, 5, 26)
    assert s.days_overdue == 3
    assert s.status == "overdue"


@pytest.mark.asyncio
async def test_list_status_treats_never_done_as_due_today() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 2, "name": "Mop", "category": "cleaning",
            "cadence_days": 14, "grace_days": 3,
            "auto_detect_kind": None, "auto_detect_entity": None,
            "description": "x", "last_done_at": None, "last_done_by": None,
        }
    ])
    store = ChoreStore(_pool_with(conn))
    status = await store.list_status(
        now=datetime(2026, 5, 29, tzinfo=UTC)
    )
    assert status[0].status == "due_today"
    assert status[0].days_overdue == 0


@pytest.mark.asyncio
async def test_list_status_filters_recent_when_requested() -> None:
    today = date(2026, 5, 29)
    # cadence 14d, done yesterday → next_due 13 days out → 'recent'
    recent = datetime.combine(today - timedelta(days=1), datetime.min.time(),
                              tzinfo=UTC)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 3, "name": "Mop", "category": "cleaning",
            "cadence_days": 14, "grace_days": 3,
            "auto_detect_kind": None, "auto_detect_entity": None,
            "description": "x", "last_done_at": recent, "last_done_by": 2,
        }
    ])
    store = ChoreStore(_pool_with(conn))
    inc = await store.list_status(now=datetime(2026, 5, 29, 9, tzinfo=UTC),
                                  include_recent=True)
    assert len(inc) == 1
    assert inc[0].status == "recent"
    exc = await store.list_status(now=datetime(2026, 5, 29, 9, tzinfo=UTC),
                                  include_recent=False)
    assert exc == []


@pytest.mark.asyncio
async def test_list_status_within_grace_still_due_today() -> None:
    """If cadence=7 and last done 8 days ago with grace=2, days_overdue=1
    → still due_today, not overdue."""
    last = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 4, "name": "Trash", "category": "trash",
            "cadence_days": 3, "grace_days": 1,
            "auto_detect_kind": None, "auto_detect_entity": None,
            "description": "x",
            "last_done_at": last, "last_done_by": 2,
        }
    ])
    store = ChoreStore(_pool_with(conn))
    status = await store.list_status(
        now=datetime(2026, 5, 25, 9, tzinfo=UTC)
    )
    assert status[0].next_due_on == date(2026, 5, 24)
    assert status[0].days_overdue == 1
    assert status[0].status == "due_today"
