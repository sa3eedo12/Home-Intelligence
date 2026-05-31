"""Tests for orchestrator.health_goals — generic engine-driven compute
+ nag + weekly reflection."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import health_goals as hg


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
    pool._conn = conn
    return pool


def _fake_store(*, active_goals=None, progress=None, log_rows=None):
    return SimpleNamespace(
        list_active=AsyncMock(return_value=active_goals or []),
        upsert_progress=AsyncMock(return_value=None),
        get_progress=AsyncMock(return_value=progress),
        record_nag=AsyncMock(return_value=None),
        excuse_today=AsyncMock(return_value=None),
        recent_log=AsyncMock(return_value=log_rows or []),
        record_log_event=AsyncMock(return_value=1),
        update_plan=AsyncMock(return_value=None),
        log_event=AsyncMock(return_value=None),
        recent_progress=AsyncMock(return_value=[]),
    )


def _fake_nag_store(*, allowed=True):
    return SimpleNamespace(is_nag_allowed_now=AsyncMock(return_value=allowed))


def _redis_recorder() -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock(return_value=b"1-0")
    return redis


# ── Pure helpers ─────────────────────────────────────────────────


def test_label_from_pct_buckets() -> None:
    assert hg._label_from_pct(None) is None
    assert hg._label_from_pct(95) == "on_track"
    assert hg._label_from_pct(80) == "on_track"
    assert hg._label_from_pct(65) == "slipping"
    assert hg._label_from_pct(50) == "slipping"
    assert hg._label_from_pct(20) == "regressing"


def test_pick_nag_text_uses_correct_tier() -> None:
    first = hg._pick_nag_text("Get strong", 0)
    second = hg._pick_nag_text("Get strong", 1)
    third = hg._pick_nag_text("Get strong", 2)
    assert any(t.format(title="Get strong") == first for t in hg._NAG_TEMPLATES_FIRST)
    assert any(t.format(title="Get strong") == second for t in hg._NAG_TEMPLATES_SECOND)
    assert any(t.format(title="Get strong") == third for t in hg._NAG_TEMPLATES_THIRD)


def test_is_muted_respects_quiet_until() -> None:
    now = datetime(2026, 5, 31, 15, tzinfo=UTC)
    assert hg._is_muted({"quiet_until": None}, now) is False
    assert hg._is_muted({"quiet_until": now + timedelta(hours=2)}, now) is True
    assert hg._is_muted({"quiet_until": now - timedelta(hours=1)}, now) is False


# ── compute_today (engine-driven) ───────────────────────────────


@pytest.mark.asyncio
async def test_compute_today_uses_engine_against_log_rows() -> None:
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
        "completion_rule": {"kind": "all_targets_met",
                             "trackers": ["sessions_today"]},
        "nudge_rule": {"kind": "behind_schedule",
                        "tracker": "sessions_today",
                        "after_local_hour": 14},
    }
    goal = {
        "id": 1, "member_id": 2, "title": "Pushups",
        "tracker_spec": spec, "workout_budget": {},
    }
    today = datetime.now(UTC)
    log_rows = [
        {"ts": today, "deltas": {"sessions_today": 2}},
        {"ts": today, "deltas": {"sessions_today": 1}},
    ]
    store = _fake_store(active_goals=[goal], log_rows=log_rows)
    out = await hg.compute_today(pool=_conn_pool(), store=store)
    assert out["ok"] is True
    assert out["processed"] == 1
    call = store.upsert_progress.await_args
    # 3 of 5 = 60% → slipping label
    assert call.kwargs["on_track_score"] == 60
    assert call.kwargs["on_track_label"] == "slipping"
    assert call.kwargs["workout_completed"] is False
    assert call.kwargs["metric_snapshots"]["sessions_today"] == 3


@pytest.mark.asyncio
async def test_compute_today_marks_complete_when_target_hit() -> None:
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
        "completion_rule": {"kind": "all_targets_met",
                             "trackers": ["sessions_today"]},
    }
    goal = {"id": 1, "member_id": 2, "title": "g", "tracker_spec": spec}
    today = datetime.now(UTC)
    log_rows = [{"ts": today, "deltas": {"sessions_today": 5}}]
    store = _fake_store(active_goals=[goal], log_rows=log_rows)
    await hg.compute_today(pool=_conn_pool(), store=store)
    call = store.upsert_progress.await_args
    assert call.kwargs["workout_completed"] is True
    assert call.kwargs["on_track_score"] == 100


@pytest.mark.asyncio
async def test_compute_today_continues_when_one_goal_fails() -> None:
    spec = {"trackers": [{"id": "x", "kind": "counter", "reset": "daily",
                           "target": 1, "direction": "up", "label": "x"}]}
    g1 = {"id": 1, "member_id": 2, "title": "g1", "tracker_spec": spec}
    g2 = {"id": 2, "member_id": 2, "title": "g2", "tracker_spec": spec}
    store = _fake_store(active_goals=[g1, g2])
    store.recent_log = AsyncMock(side_effect=[
        RuntimeError("boom"),
        [],
    ])
    out = await hg.compute_today(pool=_conn_pool(), store=store)
    assert out["processed"] == 1


# ── run_workout_nags (engine-driven) ────────────────────────────


@pytest.mark.asyncio
async def test_nag_emits_when_engine_says_nudge_due() -> None:
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
        "nudge_rule": {"kind": "behind_schedule",
                        "tracker": "sessions_today",
                        "after_local_hour": 14,
                        "before_local_hour": 22},
    }
    goal = {"id": 1, "member_id": 2, "title": "Pushups",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    # No log entries → 0 of 5 → nudge_due=True at 18:00 Dubai (14 UTC)
    store = _fake_store(active_goals=[goal], log_rows=[], progress=None)
    nag = _fake_nag_store(allowed=True)
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)  # 18:00 Dubai
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=now,
    )
    assert out["emitted"] == 1
    assert out["considered"] == 1
    redis.xadd.assert_awaited_once()
    payload = json.loads(redis.xadd.await_args.args[1]["payload"])
    assert payload["chat_id"] == 620842725
    # Even with random nag wording, the engine-grounded progress line
    # always carries the tracker state.
    assert "0 of 5" in payload["text"]
    store.record_nag.assert_awaited_once()


@pytest.mark.asyncio
async def test_nag_skipped_when_engine_says_complete() -> None:
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
    }
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    today = datetime(2026, 5, 31, 14, tzinfo=UTC)
    log_rows = [{"ts": today, "deltas": {"sessions_today": 5}}]
    store = _fake_store(active_goals=[goal], log_rows=log_rows, progress=None)
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=today,
    )
    assert out["emitted"] == 0
    assert out["skipped"]["engine_says_no"] == 1


@pytest.mark.asyncio
async def test_nag_skipped_outside_user_window() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 1, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[goal], log_rows=[], progress=None)
    nag = _fake_nag_store(allowed=False)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 31, 14, tzinfo=UTC),
    )
    assert out["emitted"] == 0
    assert out["skipped"]["outside_window"] == 1
    redis.xadd.assert_not_called()


@pytest.mark.asyncio
async def test_nag_skipped_when_cap_reached() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 1, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(
        active_goals=[goal], log_rows=[],
        progress={"nags_sent_today": hg.MAX_NAGS_PER_DAY},
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 31, 14, tzinfo=UTC),
    )
    assert out["emitted"] == 0
    assert out["skipped"]["cap"] == 1


@pytest.mark.asyncio
async def test_nag_respects_min_gap() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 1, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    store = _fake_store(
        active_goals=[goal], log_rows=[],
        progress={"nags_sent_today": 1, "last_nag_at": now - timedelta(minutes=30)},
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=now,
    )
    assert out["emitted"] == 0
    assert out["skipped"]["too_soon"] == 1


@pytest.mark.asyncio
async def test_nag_skipped_when_goal_muted() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 1, "direction": "up"}],
    }
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec,
            "quiet_until": now + timedelta(hours=4)}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[goal])
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=now,
    )
    assert out["emitted"] == 0
    assert out["skipped"]["muted"] == 1


@pytest.mark.asyncio
async def test_nag_skipped_when_no_chat_id() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 1, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=None)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[goal], log_rows=[], progress=None)
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 31, 14, tzinfo=UTC),
    )
    assert out["emitted"] == 0
    assert out["skipped"]["no_chat"] == 1
    redis.xadd.assert_not_called()


# ── weekly_reflection (unchanged signature) ─────────────────────


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
