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


def test_resolve_nag_policy_defaults() -> None:
    """Goals without nudge_rule fields use the system defaults."""
    assert hg._resolve_nag_policy({}) == (
        hg.DEFAULT_MAX_NAGS_PER_DAY, hg.DEFAULT_MIN_NAG_GAP_MINUTES,
    )
    assert hg._resolve_nag_policy({"tracker_spec": None}) == (
        hg.DEFAULT_MAX_NAGS_PER_DAY, hg.DEFAULT_MIN_NAG_GAP_MINUTES,
    )


def test_resolve_nag_policy_per_goal_override() -> None:
    """nudge_rule.max_per_day / min_gap_minutes override the defaults."""
    goal = {
        "tracker_spec": {
            "nudge_rule": {
                "max_per_day": 5,
                "min_gap_minutes": 30,
            },
        },
    }
    assert hg._resolve_nag_policy(goal) == (5, 30)


def test_resolve_nag_policy_clamps_invalid() -> None:
    """Bad values fall back to defaults rather than crashing."""
    goal = {"tracker_spec": {"nudge_rule": {"max_per_day": "five"}}}
    max_p, _ = hg._resolve_nag_policy(goal)
    assert max_p == hg.DEFAULT_MAX_NAGS_PER_DAY
    # Negative max_per_day → clamped to at least 1
    goal2 = {"tracker_spec": {"nudge_rule": {"max_per_day": -2}}}
    assert hg._resolve_nag_policy(goal2)[0] == 1


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
        progress={"nags_sent_today": hg.DEFAULT_MAX_NAGS_PER_DAY},
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


# ── weekly_reflection (now tracker-spec-driven) ─────────────────


@pytest.mark.asyncio
async def test_weekly_reflection_fallback_when_no_llm() -> None:
    """With no LLM, the fallback renders one line per tracker
    grounded in actual log data — no fabricated workout counts."""
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    today = datetime.now(UTC)
    store = SimpleNamespace(
        list_active=AsyncMock(return_value=[{
            "id": 1, "member_id": 2, "title": "Run more",
            "description": "Run 3x a week",
            "plan_text": "current plan",
            "tracker_spec": {
                "trackers": [
                    {"id": "runs_this_week", "label": "Runs",
                     "kind": "counter", "reset": "weekly", "target": 3,
                     "unit": "run", "direction": "up"},
                ],
            },
        }]),
        recent_log=AsyncMock(return_value=[
            {"ts": today - timedelta(days=2),
             "deltas": {"runs_this_week": 1}},
            {"ts": today - timedelta(days=1),
             "deltas": {"runs_this_week": 1}},
        ]),
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
    payload = json.loads(redis.xadd.await_args.args[1]["payload"])
    assert "Run more" in payload["text"]
    # Actual logged total appears (2 runs)
    assert "2 of 3" in payload["text"]
    store.update_plan.assert_not_called()
    store.log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_weekly_reflection_uses_llm_response() -> None:
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    today = datetime.now(UTC)
    store = SimpleNamespace(
        list_active=AsyncMock(return_value=[{
            "id": 1, "member_id": 2, "title": "Run more",
            "description": "Run 3x", "plan_text": "old plan",
            "tracker_spec": {
                "trackers": [
                    {"id": "runs_this_week", "label": "Runs",
                     "kind": "counter", "reset": "weekly", "target": 3,
                     "unit": "run", "direction": "up"},
                ],
            },
        }]),
        recent_log=AsyncMock(return_value=[
            {"ts": today - timedelta(days=i),
             "deltas": {"runs_this_week": 1}}
            for i in (1, 3, 5)
        ]),
        recent_progress=AsyncMock(return_value=[
            {"workout_completed": True}, {"workout_completed": True},
            {"workout_completed": True},
        ]),
        update_plan=AsyncMock(return_value=None),
        log_event=AsyncMock(return_value=None),
    )
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "reflection_text": "Solid week with 3 runs. Lock in the same days.",
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
async def test_weekly_reflection_gauge_tracker_summary() -> None:
    """Goal with a weight gauge produces a summary that reads
    latest weight + change-over-week, without inventing data."""
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    today = datetime.now(UTC)
    store = SimpleNamespace(
        list_active=AsyncMock(return_value=[{
            "id": 1, "member_id": 2, "title": "Lose 5kg",
            "description": "Lose 5 kg",
            "plan_text": "easy pace",
            "tracker_spec": {
                "trackers": [
                    {"id": "weight_kg", "label": "Weight",
                     "kind": "gauge", "reset": "weekly", "target": 85,
                     "unit": "kg", "direction": "down"},
                ],
            },
        }]),
        recent_log=AsyncMock(return_value=[
            {"ts": today - timedelta(days=6),
             "deltas": {"weight_kg": 90.0}},
            {"ts": today - timedelta(days=1),
             "deltas": {"weight_kg": 89.3}},
        ]),
        recent_progress=AsyncMock(return_value=[]),
        update_plan=AsyncMock(return_value=None),
        log_event=AsyncMock(return_value=None),
    )
    out = await hg.run_weekly_reflection(
        pool=pool, redis=redis, store=store, llm=None,
    )
    assert out["reflected"] == 1
    payload = json.loads(redis.xadd.await_args.args[1]["payload"])
    text = payload["text"]
    # Latest weight surfaces; change is annotated
    assert "Weight" in text
    assert "89.3" in text
    # No fabricated "X workouts" — only what's in the tracker summary
    assert "workout" not in text.lower()


# ── Per-goal nag policy (D) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_nag_respects_per_goal_max_per_day() -> None:
    """A goal with nudge_rule.max_per_day=5 should still nag at 4
    even though the default cap is 3."""
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 5, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24,
                        "max_per_day": 5, "min_gap_minutes": 60},
    }
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(
        active_goals=[goal], log_rows=[],
        progress={"nags_sent_today": 4},
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag,
        now=datetime(2026, 5, 31, 14, tzinfo=UTC),
    )
    # 4 < per-goal cap of 5 → fires
    assert out["emitted"] == 1


@pytest.mark.asyncio
async def test_nag_respects_per_goal_min_gap() -> None:
    """min_gap_minutes=30 on the goal lets a nag fire even if the
    global default would block it."""
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 5, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24,
                        "min_gap_minutes": 30},
    }
    goal = {"id": 1, "member_id": 2, "title": "g",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    store = _fake_store(
        active_goals=[goal], log_rows=[],
        progress={"nags_sent_today": 1,
                  "last_nag_at": now - timedelta(minutes=45)},
    )
    nag = _fake_nag_store(allowed=True)
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, now=now,
    )
    # 45 min > goal's 30-min gap → fires (even though default 90 would block)
    assert out["emitted"] == 1


# ── LLM-generated nag text (B) ──────────────────────────────────


@pytest.mark.asyncio
async def test_nag_uses_llm_generated_text_when_llm_provided() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 5, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "member_id": 2, "title": "Pushups",
            "tracker_spec": spec, "quiet_until": None,
            "plan_text": "Daily after each prayer."}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[goal], log_rows=[], progress=None)
    nag = _fake_nag_store(allowed=True)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {
        "content": "Soft check-in — still time today to knock out a couple sets after Asr.",
    }})
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, llm=llm,
        now=datetime(2026, 5, 31, 14, tzinfo=UTC),
    )
    assert out["emitted"] == 1
    payload = json.loads(redis.xadd.await_args.args[1]["payload"])
    assert "Soft check-in" in payload["text"]
    # Status line still appears underneath
    assert "0 of 5" in payload["text"]


@pytest.mark.asyncio
async def test_nag_falls_back_to_template_when_llm_unavailable() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 5, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "member_id": 2, "title": "Pushups",
            "tracker_spec": spec, "quiet_until": None}
    pool = _conn_pool(fetchval=620842725)
    redis = _redis_recorder()
    store = _fake_store(active_goals=[goal], log_rows=[], progress=None)
    nag = _fake_nag_store(allowed=True)
    # llm=None → straight to fallback
    out = await hg.run_workout_nags(
        pool=pool, redis=redis, store=store, nag_store=nag, llm=None,
        now=datetime(2026, 5, 31, 14, tzinfo=UTC),
    )
    assert out["emitted"] == 1
    payload = json.loads(redis.xadd.await_args.args[1]["payload"])
    assert "Pushups" in payload["text"]
    # Fallback line specifically mentions the goal title
    assert "goal still has room" in payload["text"]


@pytest.mark.asyncio
async def test_nag_text_trims_runaway_llm_response_to_2_sentences() -> None:
    spec = {
        "trackers": [{"id": "x", "label": "X", "kind": "counter",
                       "reset": "daily", "target": 5, "direction": "up"}],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "member_id": 2, "title": "Pushups",
            "tracker_spec": spec, "quiet_until": None}
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {
        "content": (
            "First sentence is fine. Second is also fine. "
            "Third one is too much. Fourth definitely is."
        ),
    }})
    out = await hg._compose_nag_text(
        llm=llm, goal=goal, status_line="0 of 5",
        nags_today=0, recent_log=[],
    )
    # Only the first two sentences survived
    assert out == "First sentence is fine. Second is also fine."


@pytest.mark.asyncio
async def test_nag_text_handles_llm_crash() -> None:
    """LLM call raising must not break the nag scheduler — fall back."""
    goal = {"id": 1, "member_id": 2, "title": "Run More",
            "tracker_spec": {}, "quiet_until": None}
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("ollama down"))
    out = await hg._compose_nag_text(
        llm=llm, goal=goal, status_line="0 of 5",
        nags_today=0, recent_log=[],
    )
    assert "Run More" in out
    assert "goal still has room" in out
