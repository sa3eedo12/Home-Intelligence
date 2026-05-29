"""Tests for orchestrator.health_goals — daily compute + workout nag."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from orchestrator import health_goals as hg


# ── Test doubles ────────────────────────────────────────────────


def _conn_pool(**handlers) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=handlers.get("fetchrow"))
    conn.fetchval = AsyncMock(return_value=handlers.get("fetchval"))
    conn.fetch = AsyncMock(return_value=handlers.get("fetch", []))
    conn.execute = AsyncMock(return_value="OK")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    pool._conn = conn  # expose for assertions
    return pool


def _fake_store(
    *,
    active_goals=None,
    progress=None,
):
    """Replacement for HealthGoalsStore that just records calls."""
    store = SimpleNamespace(
        list_active=AsyncMock(return_value=active_goals or []),
        upsert_progress=AsyncMock(return_value=None),
        get_progress=AsyncMock(return_value=progress),
        record_nag=AsyncMock(return_value=None),
        excuse_today=AsyncMock(return_value=None),
    )
    return store


def _fake_nag_store(*, allowed=True):
    return SimpleNamespace(is_nag_allowed_now=AsyncMock(return_value=allowed))


# ── Helpers ─────────────────────────────────────────────────────


def test_label_from_score_buckets() -> None:
    assert hg._label_from_score(None) is None
    assert hg._label_from_score(95) == "on_track"
    assert hg._label_from_score(80) == "on_track"
    assert hg._label_from_score(65) == "slipping"
    assert hg._label_from_score(50) == "slipping"
    assert hg._label_from_score(20) == "regressing"


def test_score_blends_workout_and_weight() -> None:
    # 3/4 workouts → 75; weight exactly on target → 100; average 87.5 → 88
    s = hg._score_from_snapshot(
        workout_count_week=3, workout_target=4,
        weight_actual=88.0, weight_target=88.0,
    )
    assert s == 88


def test_score_returns_none_when_no_signal() -> None:
    assert hg._score_from_snapshot(
        workout_count_week=0, workout_target=None,
        weight_actual=None, weight_target=None,
    ) is None


def test_pick_nag_text_uses_correct_tier() -> None:
    import random as _r
    _r.seed(42)
    first = hg._pick_nag_text("Get strong", 0)
    second = hg._pick_nag_text("Get strong", 1)
    third = hg._pick_nag_text("Get strong", 2)
    assert "Get strong" in first or "workout" in first.lower()
    assert any(t.format(title="Get strong") == first
               for t in hg._NAG_TEMPLATES_FIRST)
    assert any(t.format(title="Get strong") == second
               for t in hg._NAG_TEMPLATES_SECOND)
    assert any(t.format(title="Get strong") == third
               for t in hg._NAG_TEMPLATES_THIRD)


def test_is_muted_respects_quiet_until() -> None:
    now = datetime(2026, 5, 29, 15, tzinfo=UTC)
    assert hg._is_muted({"quiet_until": None}, now) is False
    assert hg._is_muted(
        {"quiet_until": now + timedelta(hours=2)}, now
    ) is True
    assert hg._is_muted(
        {"quiet_until": now - timedelta(hours=1)}, now
    ) is False


# ── compute_today ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_today_writes_progress_per_goal() -> None:
    pool = _conn_pool(
        fetchrow={"today_count": 1, "week_count": 3},
    )
    store = _fake_store(active_goals=[
        {
            "id": 1, "member_id": 2, "title": "4x/week",
            "metric_links": [{"metric": "workout", "target_per_week": 4}],
            "workout_budget": {"required_per_week": 4,
                               "days_preferred": ["fri", "sat"]},
        },
    ])
    out = await hg.compute_today(
        pool=pool, store=store, today=date(2026, 5, 29),  # Friday
    )
    assert out == {
        "ok": True, "processed": 1,
        "day": "2026-05-29", "nags_emitted": 0,
    }
    store.upsert_progress.assert_awaited_once()
    call = store.upsert_progress.await_args
    assert call.args == (1,)
    snap = call.kwargs["metric_snapshots"]
    assert snap["workouts_today"] == 1
    assert snap["workouts_this_week"] == 3
    assert call.kwargs["workout_required"] is True
    assert call.kwargs["workout_completed"] is True
    # 3/4 weekly = 75 → slipping bucket
    assert call.kwargs["on_track_label"] == "slipping"


@pytest.mark.asyncio
async def test_compute_today_continues_when_one_goal_fails() -> None:
    pool = _conn_pool(fetchrow={"today_count": 0, "week_count": 0})
    # The pool's fetchrow returns same value for every call; force one
    # call to raise by patching it after the fact.
    bad_first = True

    async def flaky_fetchrow(*args, **kwargs):
        nonlocal bad_first
        if bad_first:
            bad_first = False
            raise RuntimeError("boom")
        return {"today_count": 0, "week_count": 0}

    pool._conn.fetchrow = flaky_fetchrow
    store = _fake_store(active_goals=[
        {
            "id": 1, "member_id": 2, "title": "g1",
            "metric_links": [{"metric": "workout", "target_per_week": 4}],
            "workout_budget": {"required_per_week": 4},
        },
        {
            "id": 2, "member_id": 2, "title": "g2",
            "metric_links": [{"metric": "workout", "target_per_week": 3}],
            "workout_budget": {"required_per_week": 3},
        },
    ])
    out = await hg.compute_today(
        pool=pool, store=store, today=date(2026, 5, 29),
    )
    assert out["processed"] == 1


# ── run_workout_nags ─────────────────────────────────────────────


def _redis_recorder() -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock(return_value=b"1-0")
    return redis


@pytest.mark.asyncio
async def test_workout_nag_emits_when_required_and_not_done() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    goal = {
        "id": 1, "member_id": 2, "title": "Stay strong",
        "workout_budget": {"days_preferred": ["fri"]},
        "quiet_until": None,
    }
    store = _fake_store(active_goals=[goal], progress=None)
    nag = _fake_nag_store(allowed=True)
    now = datetime(2026, 5, 29, 16, tzinfo=UTC)  # Friday
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=now,
    )
    assert out["emitted"] == 1
    assert out["considered"] == 1
    redis.xadd.assert_awaited_once()
    raw = redis.xadd.await_args.args[1]["payload"]
    payload = json.loads(raw)
    assert payload["chat_id"] == 620842725
    assert "Stay strong" in payload["text"] or "workout" in payload["text"].lower()
    assert payload["topic"] == "goal:1"
    store.record_nag.assert_awaited_once()


@pytest.mark.asyncio
async def test_workout_nag_skipped_when_outside_window() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[{
        "id": 1, "member_id": 2, "title": "g",
        "workout_budget": {"days_preferred": ["fri"]},
    }])
    nag = _fake_nag_store(allowed=False)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 29, 10, tzinfo=UTC),
    )
    assert out["emitted"] == 0
    assert out["skipped"]["outside_window"] == 1
    redis.xadd.assert_not_called()


@pytest.mark.asyncio
async def test_workout_nag_skipped_when_workout_already_done() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(
        active_goals=[{
            "id": 1, "member_id": 2, "title": "g",
            "workout_budget": {"days_preferred": ["fri"]},
        }],
        progress={"workout_completed": True, "nags_sent_today": 0},
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 29, 16, tzinfo=UTC),
    )
    assert out["emitted"] == 0
    assert out["skipped"]["already_done"] == 1


@pytest.mark.asyncio
async def test_workout_nag_skipped_when_excused() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(
        active_goals=[{
            "id": 1, "member_id": 2, "title": "g",
            "workout_budget": {"days_preferred": ["fri"]},
        }],
        progress={"rest_day_excused": True, "nags_sent_today": 0},
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 29, 16, tzinfo=UTC),
    )
    assert out["emitted"] == 0
    assert out["skipped"]["excused"] == 1


@pytest.mark.asyncio
async def test_workout_nag_skipped_when_cap_reached() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(
        active_goals=[{
            "id": 1, "member_id": 2, "title": "g",
            "workout_budget": {"days_preferred": ["fri"]},
        }],
        progress={"nags_sent_today": hg.MAX_NAGS_PER_DAY},
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 29, 16, tzinfo=UTC),
    )
    assert out["emitted"] == 0
    assert out["skipped"]["cap"] == 1


@pytest.mark.asyncio
async def test_workout_nag_respects_min_gap() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    now = datetime(2026, 5, 29, 16, tzinfo=UTC)
    store = _fake_store(
        active_goals=[{
            "id": 1, "member_id": 2, "title": "g",
            "workout_budget": {"days_preferred": ["fri"]},
        }],
        progress={
            "nags_sent_today": 1,
            "last_nag_at": now - timedelta(minutes=30),
        },
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=now,
    )
    assert out["emitted"] == 0
    assert out["skipped"]["too_soon"] == 1


@pytest.mark.asyncio
async def test_workout_nag_skipped_when_not_a_workout_day() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[{
        "id": 1, "member_id": 2, "title": "g",
        # Wed only — Friday isn't a workout day
        "workout_budget": {"days_preferred": ["wed"]},
    }])
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 29, 16, tzinfo=UTC),  # Friday
    )
    assert out["emitted"] == 0
    assert out["skipped"]["not_required"] == 1
    assert out["considered"] == 0


@pytest.mark.asyncio
async def test_workout_nag_skipped_when_no_chat_id() -> None:
    pool = _conn_pool(fetchval=None)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[{
        "id": 1, "member_id": 2, "title": "g",
        "workout_budget": {"days_preferred": ["fri"]},
    }])
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 29, 16, tzinfo=UTC),
    )
    # Counted as considered (window allowed) but no emit + no record_nag.
    assert out["emitted"] == 0
    redis.xadd.assert_not_called()
    store.record_nag.assert_not_called()


@pytest.mark.asyncio
async def test_workout_nag_skipped_when_goal_muted() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    now = datetime(2026, 5, 29, 16, tzinfo=UTC)
    store = _fake_store(active_goals=[{
        "id": 1, "member_id": 2, "title": "g",
        "workout_budget": {"days_preferred": ["fri"]},
        "quiet_until": now + timedelta(hours=4),
    }])
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=now,
    )
    assert out["emitted"] == 0
    assert out["skipped"]["muted"] == 1
    redis.xadd.assert_not_called()


# ── weekly reflection ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_weekly_reflection_fallback_when_no_llm() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = SimpleNamespace(
        list_active=AsyncMock(return_value=[{
            "id": 1, "member_id": 2, "title": "Run more",
            "description": "Run 3x a week",
            "plan_text": "current plan",
            "workout_budget": {"required_per_week": 3},
        }]),
        recent_progress=AsyncMock(return_value=[
            {"workout_completed": True}, {"workout_completed": True},
            {"workout_completed": False, "rest_day_excused": True},
        ]),
        update_plan=AsyncMock(return_value=None),
        log_event=AsyncMock(return_value=None),
    )
    out = await hg.run_weekly_reflection(
        pool=pool, redis=redis, store=store, llm=None,
    )
    assert out == {"ok": True, "reflected": 1, "skipped": 0}
    redis.xadd.assert_awaited_once()
    raw = redis.xadd.await_args.args[1]["payload"]
    payload = json.loads(raw)
    assert "Run more" in payload["text"]
    assert "2 of 3" in payload["text"]
    assert payload["topic"] == "goal:1:weekly"
    # plan unchanged (no LLM, no new_plan_text)
    store.update_plan.assert_not_called()
    store.log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_weekly_reflection_uses_llm_response() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = SimpleNamespace(
        list_active=AsyncMock(return_value=[{
            "id": 1, "member_id": 2, "title": "Run more",
            "description": "Run 3x", "plan_text": "old plan",
            "workout_budget": {"required_per_week": 3},
        }]),
        recent_progress=AsyncMock(return_value=[
            {"workout_completed": True}, {"workout_completed": True},
            {"workout_completed": True},
        ]),
        update_plan=AsyncMock(return_value=None),
        log_event=AsyncMock(return_value=None),
    )
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "reflection_text": "Solid week. Lock in the same three days next week.",
        "new_plan_text": "Three runs on Tue/Thu/Sat, easy pace.",
    })}})
    out = await hg.run_weekly_reflection(
        pool=pool, redis=redis, store=store, llm=llm,
    )
    assert out["reflected"] == 1
    store.update_plan.assert_awaited_once()
    update_args = store.update_plan.await_args
    assert update_args.kwargs["plan_text"].startswith("Three runs")


@pytest.mark.asyncio
async def test_weekly_reflection_continues_on_llm_failure() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = SimpleNamespace(
        list_active=AsyncMock(return_value=[
            {"id": 1, "member_id": 2, "title": "G1",
             "description": "x", "plan_text": "p",
             "workout_budget": {"required_per_week": 3}},
            {"id": 2, "member_id": 2, "title": "G2",
             "description": "y", "plan_text": "q",
             "workout_budget": {"required_per_week": 4}},
        ]),
        recent_progress=AsyncMock(return_value=[]),
        update_plan=AsyncMock(return_value=None),
        log_event=AsyncMock(return_value=None),
    )
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("boom"))
    out = await hg.run_weekly_reflection(
        pool=pool, redis=redis, store=store, llm=llm,
    )
    # Both fell back to template text; both got messages
    assert out["reflected"] == 2
    assert redis.xadd.await_count == 2
