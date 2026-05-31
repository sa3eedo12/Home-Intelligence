"""Tests for orchestrator.engagement — engagement observation +
window proposal + cross-goal insight."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import engagement as eng


# ── Fixtures ────────────────────────────────────────────────────


def _conn_pool(*, exec_result="UPDATE 0", rows=None, chat_id=None) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=chat_id)
    conn.execute = AsyncMock(return_value=exec_result)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    pool._conn = conn
    return pool


def _redis_stub() -> SimpleNamespace:
    store: dict = {}

    async def _xadd(stream, fields):
        store.setdefault(f"_xadd:{stream}", []).append(fields)
        return b"1-0"

    return SimpleNamespace(_store=store, xadd=AsyncMock(side_effect=_xadd))


# ── EngagementStore ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_sent_returns_new_id() -> None:
    pool = _conn_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": 42})
    store = eng.EngagementStore(pool)
    out = await store.record_sent(
        member_id=2, topic="goal:1", agent="health_goals",
        capability="workout_nag",
    )
    assert out == 42


@pytest.mark.asyncio
async def test_record_sent_safe_when_no_pool() -> None:
    store = eng.EngagementStore(pool=None)
    assert await store.record_sent(member_id=2) is None


@pytest.mark.asyncio
async def test_record_inbound_parses_update_count() -> None:
    pool = _conn_pool(exec_result="UPDATE 3")
    store = eng.EngagementStore(pool)
    n = await store.record_inbound(member_id=2)
    assert n == 3


@pytest.mark.asyncio
async def test_record_inbound_handles_malformed_result() -> None:
    pool = _conn_pool(exec_result="weird")
    store = eng.EngagementStore(pool)
    n = await store.record_inbound(member_id=2)
    assert n == 0


@pytest.mark.asyncio
async def test_recent_events_returns_rows() -> None:
    rows = [
        {"id": 1, "sent_at": datetime.now(UTC) - timedelta(hours=2),
         "first_reply_at": datetime.now(UTC) - timedelta(hours=1),
         "reply_seconds": 3600, "topic": "x", "agent": "a", "capability": "c"},
    ]
    pool = _conn_pool(rows=rows)
    store = eng.EngagementStore(pool)
    out = await store.recent_events(2)
    assert len(out) == 1
    assert out[0]["reply_seconds"] == 3600


# ── _bucket_engagement ──────────────────────────────────────────


def test_bucket_engagement_aggregates_by_dow_and_band() -> None:
    # 2026-05-31 Sun 10:00 UTC = Sun 14:00 Dubai (afternoon)
    sent_aft = datetime(2026, 5, 31, 10, 0, tzinfo=UTC)
    # 2026-05-31 Sun 04:00 UTC = Sun 08:00 Dubai (morning)
    sent_morn = datetime(2026, 5, 31, 4, 0, tzinfo=UTC)
    events = [
        {"sent_at": sent_aft, "first_reply_at": sent_aft + timedelta(minutes=10)},
        {"sent_at": sent_aft, "first_reply_at": None},  # not replied
        {"sent_at": sent_morn, "first_reply_at": None},
    ]
    out = eng._bucket_engagement(events)
    assert "sun_afternoon" in out
    assert out["sun_afternoon"]["sent"] == 2
    assert out["sun_afternoon"]["replied"] == 1
    assert "sun_morning" in out
    assert out["sun_morning"]["sent"] == 1
    assert out["sun_morning"]["replied"] == 0


def test_bucket_engagement_ignores_invalid_timestamps() -> None:
    out = eng._bucket_engagement([
        {"sent_at": "not a datetime"},
        {"sent_at": None},
    ])
    assert out == {}


# ── propose_window_change ───────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_skips_when_too_few_events() -> None:
    pool = _conn_pool()
    redis = _redis_stub()
    store = eng.EngagementStore(pool)
    store.recent_events = AsyncMock(return_value=[
        {"sent_at": datetime.now(UTC)}
    ])
    nag_store = SimpleNamespace(get=AsyncMock(return_value={}))
    out = await eng.propose_window_change(
        pool=pool, redis=redis, engagement_store=store,
        nag_windows_store=nag_store, member_id=2, llm=MagicMock(),
    )
    assert out["skipped"] == "too_few_events"


@pytest.mark.asyncio
async def test_propose_skips_when_llm_says_no_pattern() -> None:
    pool = _conn_pool()
    redis = _redis_stub()
    store = eng.EngagementStore(pool)
    store.recent_events = AsyncMock(return_value=[
        {"sent_at": datetime.now(UTC), "first_reply_at": None}
        for _ in range(15)
    ])
    nag_store = SimpleNamespace(get=AsyncMock(return_value={
        "weekday_start_hour": 14, "weekday_end_hour": 21,
        "weekend_start_hour": 10, "weekend_end_hour": 21,
    }))
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "should_propose": False,
        "user_message": "",
        "pattern_summary": "no clear pattern",
    })}})
    out = await eng.propose_window_change(
        pool=pool, redis=redis, engagement_store=store,
        nag_windows_store=nag_store, member_id=2, llm=llm,
    )
    assert out["skipped"] == "no_pattern"
    # No Telegram sent
    assert "_xadd:notify.outbound" not in redis._store


@pytest.mark.asyncio
async def test_propose_sends_telegram_when_pattern_found() -> None:
    pool = _conn_pool(chat_id=620842725)
    redis = _redis_stub()
    store = eng.EngagementStore(pool)
    store.recent_events = AsyncMock(return_value=[
        {"sent_at": datetime.now(UTC) - timedelta(days=i),
         "first_reply_at": None}
        for i in range(15)
    ])
    nag_store = SimpleNamespace(get=AsyncMock(return_value={
        "weekday_start_hour": 14, "weekday_end_hour": 21,
        "weekend_start_hour": 10, "weekend_end_hour": 21,
    }))
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "should_propose": True,
        "user_message": ("I notice you don't reply to messages on "
                         "weekday mornings — want me to quiet 09:00-13:00?"),
        "pattern_summary": "0 of 8 weekday-morning nudges replied",
    })}})
    out = await eng.propose_window_change(
        pool=pool, redis=redis, engagement_store=store,
        nag_windows_store=nag_store, member_id=2, llm=llm,
    )
    assert out["proposed"] is True
    notifications = redis._store["_xadd:notify.outbound"]
    assert len(notifications) == 1
    payload = json.loads(notifications[0]["payload"])
    assert "quiet 09:00-13:00" in payload["text"]


@pytest.mark.asyncio
async def test_propose_skips_when_no_llm() -> None:
    pool = _conn_pool()
    redis = _redis_stub()
    store = eng.EngagementStore(pool)
    store.recent_events = AsyncMock(return_value=[
        {"sent_at": datetime.now(UTC), "first_reply_at": None}
        for _ in range(15)
    ])
    nag_store = SimpleNamespace(get=AsyncMock(return_value={}))
    out = await eng.propose_window_change(
        pool=pool, redis=redis, engagement_store=store,
        nag_windows_store=nag_store, member_id=2, llm=None,
    )
    assert out["skipped"] == "no_llm"


# ── run_cross_goal_insight ──────────────────────────────────────


def _goal(goal_id: int, title: str) -> dict:
    return {
        "id": goal_id, "member_id": 2, "title": title,
        "plan_text": f"Plan for {title}",
        "tracker_spec": {"trackers": [{
            "id": "x", "label": "X", "kind": "counter",
            "reset": "daily", "target": 1, "direction": "up",
        }]},
    }


@pytest.mark.asyncio
async def test_cross_goal_skips_with_one_goal() -> None:
    pool = _conn_pool(chat_id=620842725)
    redis = _redis_stub()
    goals_store = SimpleNamespace(
        list_active=AsyncMock(return_value=[_goal(1, "g1")]),
        recent_progress=AsyncMock(return_value=[]),
    )
    out = await eng.run_cross_goal_insight(
        pool=pool, redis=redis, goals_store=goals_store, member_id=2,
        llm=MagicMock(),
    )
    assert out["skipped"] == "too_few_goals"


@pytest.mark.asyncio
async def test_cross_goal_skips_when_llm_says_no_insight() -> None:
    pool = _conn_pool(chat_id=620842725)
    redis = _redis_stub()
    goals_store = SimpleNamespace(
        list_active=AsyncMock(return_value=[_goal(1, "g1"), _goal(2, "g2")]),
        recent_progress=AsyncMock(return_value=[]),
    )
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "have_insight": False, "insight_text": "", "suggestion": None,
    })}})
    out = await eng.run_cross_goal_insight(
        pool=pool, redis=redis, goals_store=goals_store, member_id=2,
        llm=llm,
    )
    assert out["skipped"] == "no_insight"
    assert "_xadd:notify.outbound" not in redis._store


@pytest.mark.asyncio
async def test_cross_goal_persists_and_sends_when_insight_found() -> None:
    pool = _conn_pool(chat_id=620842725)
    redis = _redis_stub()
    goals_store = SimpleNamespace(
        list_active=AsyncMock(return_value=[_goal(1, "Run"), _goal(2, "Lose 5kg")]),
        recent_progress=AsyncMock(return_value=[
            {"on_track_score": 90, "on_track_label": "on_track",
             "metric_snapshots": {"workouts_this_week": 4}},
        ]),
    )
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "have_insight": True,
        "insight_text": ("Workouts are on track but weight is flat — "
                         "calories are probably the bottleneck."),
        "suggestion": {"target": "Lose 5kg", "action": "log_calories"},
    })}})
    out = await eng.run_cross_goal_insight(
        pool=pool, redis=redis, goals_store=goals_store, member_id=2,
        llm=llm,
    )
    assert out["ok"] is True
    assert out["sent"] is True
    # Insert into cross_goal_insights happened
    pool._conn.execute.assert_awaited_once()
    sql = pool._conn.execute.await_args.args[0]
    assert "cross_goal_insights" in sql
    # Telegram sent
    notifications = redis._store["_xadd:notify.outbound"]
    assert len(notifications) == 1
    payload = json.loads(notifications[0]["payload"])
    assert "calories" in payload["text"]


@pytest.mark.asyncio
async def test_cross_goal_skips_when_no_llm() -> None:
    pool = _conn_pool()
    redis = _redis_stub()
    goals_store = SimpleNamespace(
        list_active=AsyncMock(return_value=[_goal(1, "a"), _goal(2, "b")]),
        recent_progress=AsyncMock(return_value=[]),
    )
    out = await eng.run_cross_goal_insight(
        pool=pool, redis=redis, goals_store=goals_store, member_id=2,
        llm=None,
    )
    assert out["skipped"] == "no_llm"


@pytest.mark.asyncio
async def test_cross_goal_handles_llm_invalid_json() -> None:
    pool = _conn_pool(chat_id=620842725)
    redis = _redis_stub()
    goals_store = SimpleNamespace(
        list_active=AsyncMock(return_value=[_goal(1, "a"), _goal(2, "b")]),
        recent_progress=AsyncMock(return_value=[]),
    )
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": "not json"}})
    out = await eng.run_cross_goal_insight(
        pool=pool, redis=redis, goals_store=goals_store, member_id=2,
        llm=llm,
    )
    assert out["error"] == "parse_failed"
