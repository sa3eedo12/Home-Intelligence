"""Tests for HealthGoalsStore — CRUD, progress, milestones, nag bookkeeping."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.health_goals_store import (
    VALID_EVENT_KINDS,
    VALID_LABELS,
    VALID_STATUSES,
    GoalSnapshot,
    HealthGoalsStore,
    _coerce_date,
    _decode_goal_row,
    _decode_progress_row,
    workout_required_today,
)


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    return pool


def _txn_conn() -> MagicMock:
    """A conn mock that also supports `async with conn.transaction()`."""
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn)
    conn.execute = AsyncMock(return_value="OK")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)
    return conn


# ── No-pool defaults ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_returns_safe_defaults_when_no_pool() -> None:
    store = HealthGoalsStore(pool=None)
    assert await store.create(member_id=2, title="x", description="y") is None
    assert await store.get(1) is None
    assert await store.list_active() == []
    assert await store.list_all_for_member(2) == []
    assert await store.list_milestones(1) == []
    assert await store.get_progress(1) is None
    assert await store.recent_progress(1) == []
    assert await store.excuses_this_week(1) == 0
    assert await store.recent_events(1) == []
    # void methods just return
    await store.update_plan(1, plan_text="x")
    await store.set_quiet_until(1, until=None)
    await store.excuse_today(1)
    await store.record_nag(1)


# ── create + audit ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_writes_goal_and_event_audit() -> None:
    conn = _txn_conn()
    conn.fetchrow = AsyncMock(return_value={"id": 7})
    store = HealthGoalsStore(_pool_with(conn))
    goal_id = await store.create(
        member_id=2,
        title="Work out 4x/week",
        description="Stay consistent on 4 workouts a week through Q2",
        metric_links=[{"metric": "workout", "target_per_week": 4}],
        workout_budget={"required_per_week": 4, "flexible_rest_per_week": 2,
                         "days_preferred": ["sun", "tue", "thu", "sat"]},
        plan_text="Hit four sessions weekly; Monday and Friday are flex rest days.",
        target_date=date(2026, 9, 1),
    )
    assert goal_id == 7
    # Two writes: the insert + the audit event
    assert conn.fetchrow.await_count == 1
    assert conn.execute.await_count == 1
    insert_args = conn.fetchrow.await_args.args
    # member_id, title, description, metric_links_json, workout_budget_json, plan_text, start_date, target_date
    assert insert_args[1] == 2
    assert insert_args[2] == "Work out 4x/week"
    parsed_links = json.loads(insert_args[4])
    assert parsed_links == [{"metric": "workout", "target_per_week": 4}]
    parsed_budget = json.loads(insert_args[5])
    assert parsed_budget["required_per_week"] == 4
    event_args = conn.execute.await_args.args
    assert event_args[1] == 7
    assert event_args[2] == 2


@pytest.mark.asyncio
async def test_create_handles_missing_workout_budget() -> None:
    conn = _txn_conn()
    conn.fetchrow = AsyncMock(return_value={"id": 8})
    store = HealthGoalsStore(_pool_with(conn))
    goal_id = await store.create(
        member_id=2, title="Sleep more",
        description="Get to bed before midnight",
        metric_links=[{"metric": "sleep_asleep", "direction": "up"}],
    )
    assert goal_id == 8
    insert_args = conn.fetchrow.await_args.args
    # workout_budget arg should be None, not a JSON 'null' string
    assert insert_args[5] is None


# ── status transitions ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_status_rejects_unknown() -> None:
    store = HealthGoalsStore(pool=None)
    with pytest.raises(ValueError):
        await store.set_status(1, "weird")


@pytest.mark.asyncio
async def test_set_status_emits_correct_event_kind() -> None:
    conn = _txn_conn()
    store = HealthGoalsStore(_pool_with(conn))
    await store.set_status(7, "paused", note="vacation")
    # update + insert
    assert conn.execute.await_count == 2
    event_args = conn.execute.await_args_list[1].args
    assert event_args[2] == "paused"
    assert event_args[3] == "vacation"


@pytest.mark.asyncio
async def test_set_status_active_maps_to_resumed_event() -> None:
    conn = _txn_conn()
    store = HealthGoalsStore(_pool_with(conn))
    await store.set_status(7, "active")
    event_args = conn.execute.await_args_list[1].args
    assert event_args[2] == "resumed"


# ── log_event ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_event_rejects_unknown_kind() -> None:
    store = HealthGoalsStore(pool=None)
    with pytest.raises(ValueError):
        await store.log_event(7, "fake_kind")


@pytest.mark.asyncio
async def test_log_event_accepts_all_known_kinds() -> None:
    conn = _txn_conn()
    store = HealthGoalsStore(_pool_with(conn))
    for kind in VALID_EVENT_KINDS:
        await store.log_event(7, kind, member_id=2, note=f"k={kind}")
    assert conn.execute.await_count == len(VALID_EVENT_KINDS)


# ── upsert_progress validation ───────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_progress_rejects_invalid_label() -> None:
    store = HealthGoalsStore(pool=None)
    with pytest.raises(ValueError):
        await store.upsert_progress(
            7, day=date(2026, 5, 29), metric_snapshots={},
            on_track_score=80, on_track_label="bogus",
            workout_required=True, workout_completed=False,
            rest_day_excused=False,
        )


@pytest.mark.asyncio
async def test_upsert_progress_writes_jsonb_snapshot() -> None:
    conn = _txn_conn()
    store = HealthGoalsStore(_pool_with(conn))
    await store.upsert_progress(
        7, day=date(2026, 5, 29),
        metric_snapshots={"workouts_this_week": 3, "weight_kg": 89.5},
        on_track_score=72, on_track_label="on_track",
        workout_required=True, workout_completed=True,
        rest_day_excused=False, note="great session",
    )
    args = conn.execute.await_args.args
    payload = json.loads(args[3])
    assert payload == {"workouts_this_week": 3, "weight_kg": 89.5}


# ── excuse_today + excuses_this_week ─────────────────────────────


@pytest.mark.asyncio
async def test_excuse_today_marks_progress_and_logs_event() -> None:
    conn = _txn_conn()
    store = HealthGoalsStore(_pool_with(conn))
    await store.excuse_today(7, note="rough day at work")
    # progress upsert + event insert
    assert conn.execute.await_count == 2
    event_args = conn.execute.await_args_list[1].args
    assert event_args[2] == "rough day at work"


@pytest.mark.asyncio
async def test_excuses_this_week_returns_count() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=2)
    store = HealthGoalsStore(_pool_with(conn))
    assert await store.excuses_this_week(7) == 2


# ── record_nag ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_nag_inserts_progress_and_event() -> None:
    conn = _txn_conn()
    store = HealthGoalsStore(_pool_with(conn))
    await store.record_nag(7)
    assert conn.execute.await_count == 2
    # Last call inserts the audit event
    event_args = conn.execute.await_args_list[1].args
    assert event_args[1] == 7


# ── workout_required_today helper ─────────────────────────────────


def test_workout_required_today_with_no_budget_is_false() -> None:
    assert workout_required_today({}, today=date(2026, 5, 29)) is False
    assert workout_required_today({"workout_budget": None},
                                  today=date(2026, 5, 29)) is False
    assert workout_required_today({"workout_budget": {}},
                                  today=date(2026, 5, 29)) is False


def test_workout_required_today_with_days_preferred() -> None:
    """2026-05-29 is a Friday."""
    friday = date(2026, 5, 29)
    assert friday.weekday() == 4  # 4 = fri
    goal = {"workout_budget": {"days_preferred": ["fri", "sun"]}}
    assert workout_required_today(goal, today=friday) is True
    saturday = friday + timedelta(days=1)
    assert workout_required_today(goal, today=saturday) is False


def test_workout_required_today_handles_full_day_names() -> None:
    """The LLM may use 'monday' instead of 'mon'. Helper truncates."""
    monday = date(2026, 6, 1)
    assert monday.weekday() == 0
    goal = {"workout_budget": {"days_preferred": ["Monday", "Wednesday"]}}
    assert workout_required_today(goal, today=monday) is True


def test_workout_required_today_with_empty_days_pref_defaults_true() -> None:
    """When days_preferred is missing/empty but budget exists, the
    daily compute job uses weekly quota; helper returns True so that
    the day is at least considered."""
    goal = {"workout_budget": {"required_per_week": 4}}
    assert workout_required_today(goal, today=date(2026, 5, 29)) is True


# ── Decode helpers ───────────────────────────────────────────────


def test_decode_goal_row_parses_json_strings() -> None:
    row = {
        "id": 1, "member_id": 2, "title": "x", "description": "y",
        "metric_links": '[{"metric": "weight", "target": 88}]',
        "workout_budget": '{"required_per_week": 4}',
        "plan_text": None, "plan_generated_at": None,
        "start_date": date(2026, 5, 29), "target_date": None,
        "status": "active", "quiet_until": None,
        "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
    }
    out = _decode_goal_row(row)
    assert out is not None
    assert out["metric_links"] == [{"metric": "weight", "target": 88}]
    assert out["workout_budget"] == {"required_per_week": 4}


def test_decode_goal_row_handles_none_metric_links() -> None:
    row = {
        "id": 1, "member_id": 2, "title": "x", "description": "y",
        "metric_links": None, "workout_budget": None,
        "plan_text": None, "plan_generated_at": None,
        "start_date": date.today(), "target_date": None,
        "status": "active", "quiet_until": None,
        "created_at": None, "updated_at": None,
    }
    out = _decode_goal_row(row)
    assert out is not None
    assert out["metric_links"] == []
    assert out["workout_budget"] is None


def test_decode_progress_row_parses_snapshot() -> None:
    row = {
        "goal_id": 1, "day": date.today(),
        "metric_snapshots": '{"workouts_this_week": 2}',
        "on_track_score": 60, "on_track_label": "slipping",
        "workout_required": True, "workout_completed": False,
        "rest_day_excused": False, "nags_sent_today": 1,
        "last_nag_at": datetime.now(UTC), "note": "",
        "updated_at": datetime.now(UTC),
    }
    out = _decode_progress_row(row)
    assert out is not None
    assert out["metric_snapshots"] == {"workouts_this_week": 2}


def test_coerce_date_handles_str_date_datetime() -> None:
    assert _coerce_date("2026-05-29") == date(2026, 5, 29)
    assert _coerce_date(date(2026, 5, 29)) == date(2026, 5, 29)
    assert _coerce_date(datetime(2026, 5, 29, 12, 0)) == date(2026, 5, 29)
    with pytest.raises(ValueError):
        _coerce_date(42)


# ── Constants are exhaustive ─────────────────────────────────────


def test_valid_statuses_cover_all_known_states() -> None:
    assert {"active", "achieved", "paused", "abandoned"} <= VALID_STATUSES


def test_valid_labels_cover_all_known_progress_labels() -> None:
    assert {"on_track", "slipping", "regressing", "achieved", "paused"} <= VALID_LABELS


# ── Live-PG smoke test (only runs if PG_TEST_URL is set) ────────


@pytest.mark.asyncio
async def test_create_sql_round_trip_against_real_pg() -> None:
    """Catches asyncpg parameter-type-inference regressions like
    'could not determine data type of parameter $6'. Skips when no
    Postgres is reachable; CI is mock-only so the burden falls on
    manual runs against the live schema."""
    import os
    pg_url = os.environ.get("PG_TEST_URL")
    if not pg_url:
        pytest.skip("PG_TEST_URL not set")
    import asyncpg  # type: ignore[import-not-found]

    pool = await asyncpg.create_pool(pg_url, min_size=1, max_size=1)
    try:
        store = HealthGoalsStore(pool)
        async with pool.acquire() as conn:
            member_id = await conn.fetchval(
                "SELECT id FROM household_members ORDER BY id LIMIT 1"
            )
        if not member_id:
            pytest.skip("no household_members row to anchor FK on")
        goal_id = await store.create(
            member_id=int(member_id),
            title="SQL smoke test",
            description="Round-trip the create() statement.",
            metric_links=[{"metric": "workout", "target_per_week": 3}],
            workout_budget={"required_per_week": 3,
                             "days_preferred": ["mon", "wed", "fri"]},
            plan_text="Test plan text.",
            target_date=date.today() + timedelta(days=14),
        )
        assert goal_id is not None
        # Also exercise the None-plan-text path, which is where the
        # original bug bit (CASE WHEN $6 IS NOT NULL with no cast).
        goal_id2 = await store.create(
            member_id=int(member_id),
            title="SQL smoke test 2",
            description="Round-trip with plan_text=None.",
            plan_text=None,
            workout_budget=None,
        )
        assert goal_id2 is not None
        # Cleanup
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM health_goals WHERE id IN ($1, $2)",
                goal_id, goal_id2,
            )
    finally:
        await pool.close()
