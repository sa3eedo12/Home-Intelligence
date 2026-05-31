"""Tests for orchestrator.goal_auto_logger — auto-log HealthKit events
into active health goals."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import goal_auto_logger as al


# ── Fixtures ────────────────────────────────────────────────────


def _conn_pool(*, rows=None, chat_id=None) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchval = AsyncMock(return_value=chat_id)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    pool._conn = conn
    return pool


def _fake_redis_stub() -> SimpleNamespace:
    store: dict[str, str] = {}

    async def _get(key): return store.get(key)
    async def _set(key, value, ex=None):
        store[key] = value
        return True
    async def _xadd(stream, fields):
        store.setdefault(f"_xadd:{stream}", []).append(fields)
        return b"1-0"

    return SimpleNamespace(
        _store=store,
        get=AsyncMock(side_effect=_get),
        set=AsyncMock(side_effect=_set),
        xadd=AsyncMock(side_effect=_xadd),
    )


def _fake_store(*, active=None, recent_log_rows=None):
    return SimpleNamespace(
        list_active=AsyncMock(return_value=active or []),
        record_log_event=AsyncMock(return_value=1),
        recent_log=AsyncMock(return_value=recent_log_rows or []),
    )


def _pushup_goal() -> dict:
    return {
        "id": 4, "member_id": 2, "title": "Pushups",
        "tracker_spec": {
            "trackers": [
                {"id": "sessions_today", "label": "Sets", "kind": "counter",
                 "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
                {"id": "pushups_today", "label": "Pushups", "kind": "counter",
                 "reset": "daily", "target": 100, "unit": "pushup",
                 "direction": "up"},
            ],
            "log_hints": [
                {"if_mentions": ["pushup", "strength", "set"],
                 "increment": {"sessions_today": 1}},
            ],
        },
    }


# ── _metric_matches_goal_hints ──────────────────────────────────


def test_matches_via_log_hints() -> None:
    metric = {"metric": "strength_workout", "unit": "minutes"}
    assert al._metric_matches_goal_hints(_pushup_goal(), metric) is True


def test_matches_via_tracker_label_fallback() -> None:
    goal = {
        "tracker_spec": {
            "trackers": [
                {"id": "weight_kg", "label": "Weight", "kind": "gauge",
                 "reset": "weekly", "target": 80, "unit": "kg",
                 "direction": "down"},
            ],
        },
    }
    metric = {"metric": "weight", "unit": "kg"}
    assert al._metric_matches_goal_hints(goal, metric) is True


def test_no_match_returns_false() -> None:
    metric = {"metric": "heart_rate", "unit": "bpm"}
    assert al._metric_matches_goal_hints(_pushup_goal(), metric) is False


def test_empty_spec_returns_false() -> None:
    assert al._metric_matches_goal_hints({"tracker_spec": None}, {}) is False
    assert al._metric_matches_goal_hints({}, {"metric": "x"}) is False


# ── _fallback_classify ──────────────────────────────────────────


def test_fallback_one_tracker_unit_match() -> None:
    spec = {
        "trackers": [
            {"id": "weight_kg", "label": "Weight", "kind": "gauge",
             "reset": "weekly", "target": 80, "unit": "kg",
             "direction": "down"},
        ],
    }
    metric = {"metric": "weight", "unit": "kg", "value": 87.4,
              "started_at": datetime(2026, 5, 31, 8, tzinfo=UTC)}
    deltas, ts = al._fallback_classify(spec, metric)
    assert deltas == {"weight_kg": 87.4}
    assert isinstance(ts, datetime)


def test_fallback_multiple_trackers_returns_empty() -> None:
    """Multi-tracker goals need the LLM to disambiguate."""
    deltas, _ = al._fallback_classify(
        _pushup_goal()["tracker_spec"], {"metric": "x", "value": 1},
    )
    assert deltas == {}


def test_fallback_unit_mismatch_returns_empty() -> None:
    spec = {
        "trackers": [{"id": "x", "kind": "counter", "reset": "daily",
                       "target": 1, "direction": "up", "unit": "rep"}],
    }
    metric = {"metric": "weight", "unit": "kg", "value": 80}
    assert al._fallback_classify(spec, metric)[0] == {}


# ── _classify_metric (no LLM) ──────────────────────────────────


@pytest.mark.asyncio
async def test_classify_metric_no_llm_uses_fallback() -> None:
    spec = {
        "trackers": [
            {"id": "weight_kg", "label": "Weight", "kind": "gauge",
             "reset": "weekly", "target": 80, "unit": "kg",
             "direction": "down"},
        ],
    }
    goal = {"id": 1, "title": "Lose weight", "tracker_spec": spec}
    metric = {"metric": "weight", "unit": "kg", "value": 87.4,
              "started_at": datetime(2026, 5, 31, 8, tzinfo=UTC)}
    deltas, ts = await al._classify_metric(
        llm=None, goal=goal, metric_row=metric, classifier_model="x",
    )
    assert deltas == {"weight_kg": 87.4}


@pytest.mark.asyncio
async def test_classify_metric_with_llm() -> None:
    """LLM returns deltas + ts_iso → forwarded correctly."""
    spec = _pushup_goal()["tracker_spec"]
    goal = _pushup_goal()
    metric = {"metric": "strength_workout", "unit": "minutes",
              "value": 32,
              "started_at": datetime.now(UTC) - timedelta(hours=2)}
    past_iso = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"sessions_today": 1, "pushups_today": 30},
        "ts_iso": past_iso,
        "reasoning_brief": "Strength workout — counts as a set + 30 reps",
    })}})
    deltas, ts = await al._classify_metric(
        llm=llm, goal=goal, metric_row=metric, classifier_model="qwen3:8b",
    )
    assert deltas == {"sessions_today": 1.0, "pushups_today": 30.0}
    assert ts is not None


@pytest.mark.asyncio
async def test_classify_metric_llm_returns_invalid_json() -> None:
    spec = _pushup_goal()["tracker_spec"]
    goal = _pushup_goal()
    metric = {"metric": "x", "unit": "set", "value": 1,
              "started_at": datetime.now(UTC)}
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": "not json"}})
    deltas, _ = await al._classify_metric(
        llm=llm, goal=goal, metric_row=metric, classifier_model="x",
    )
    assert deltas == {}


@pytest.mark.asyncio
async def test_classify_metric_drops_unknown_tracker_keys() -> None:
    spec = _pushup_goal()["tracker_spec"]
    goal = _pushup_goal()
    metric = {"metric": "x", "started_at": datetime.now(UTC)}
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"nonsense_tracker": 5, "sessions_today": 1},
        "ts_iso": None,
    })}})
    deltas, _ = await al._classify_metric(
        llm=llm, goal=goal, metric_row=metric, classifier_model="x",
    )
    # Only the valid id kept
    assert deltas == {"sessions_today": 1.0}


# ── run_once (full flow) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_advances_watermark_and_logs() -> None:
    """A new metric row matches the goal's log_hints → LLM classifies →
    log entry written + watermark advances."""
    goal = _pushup_goal()
    metric_row = {
        "id": 1001, "metric": "strength_workout", "started_at": datetime.now(UTC),
        "ended_at": None, "value": 32, "unit": "minutes",
        "source": "health_auto_export", "member_id": 2, "metadata": {},
    }
    pool = _conn_pool(rows=[metric_row], chat_id=620842725)
    redis = _fake_redis_stub()
    store = _fake_store(active=[goal])
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"sessions_today": 1, "pushups_today": 30},
        "ts_iso": None,
    })}})
    out = await al.run_once(
        pool=pool, redis=redis, store=store, llm=llm,
    )
    assert out["logged"] == 1
    assert out["watermark"] == 1001
    store.record_log_event.assert_awaited_once()
    record_kwargs = store.record_log_event.await_args.kwargs
    assert record_kwargs["source"] == "auto_healthkit"
    assert record_kwargs["deltas"] == {"sessions_today": 1.0, "pushups_today": 30.0}
    # Watermark persisted
    assert redis._store["auto_log:last_metric_id"] == "1001"
    # Notification was emitted
    notifications = redis._store.get("_xadd:notify.outbound") or []
    assert len(notifications) == 1
    payload = json.loads(notifications[0]["payload"])
    assert "Auto-logged" in payload["text"]
    assert "Pushups" in payload["text"]


@pytest.mark.asyncio
async def test_run_once_skips_when_no_goals() -> None:
    metric_row = {"id": 5, "metric": "weight", "value": 80, "unit": "kg",
                  "started_at": datetime.now(UTC), "ended_at": None,
                  "member_id": 2, "source": "x", "metadata": {}}
    pool = _conn_pool(rows=[metric_row])
    redis = _fake_redis_stub()
    store = _fake_store(active=[])
    out = await al.run_once(pool=pool, redis=redis, store=store)
    assert out["logged"] == 0
    assert out.get("skipped_no_goals") is True
    # Watermark still advanced
    assert redis._store["auto_log:last_metric_id"] == "5"


@pytest.mark.asyncio
async def test_run_once_skips_non_matching_metrics() -> None:
    goal = _pushup_goal()
    # Heart rate doesn't match pushup goal hints
    metric_row = {"id": 1, "metric": "heart_rate", "value": 72, "unit": "bpm",
                  "started_at": datetime.now(UTC), "ended_at": None,
                  "member_id": 2, "source": "x", "metadata": {}}
    pool = _conn_pool(rows=[metric_row])
    redis = _fake_redis_stub()
    store = _fake_store(active=[goal])
    out = await al.run_once(pool=pool, redis=redis, store=store, llm=None)
    assert out["logged"] == 0
    store.record_log_event.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_dedupes_against_recent_manual_log() -> None:
    """If the user already manually logged something for this goal in
    the last 10 minutes, the auto-logger doesn't double-file."""
    goal = _pushup_goal()
    metric_row = {"id": 1, "metric": "strength_workout", "value": 32,
                  "unit": "minutes", "started_at": datetime.now(UTC),
                  "ended_at": None, "member_id": 2,
                  "source": "x", "metadata": {}}
    pool = _conn_pool(rows=[metric_row])
    redis = _fake_redis_stub()
    # A recent telegram log already exists
    store = _fake_store(active=[goal], recent_log_rows=[
        {"id": 99, "ts": datetime.now(UTC) - timedelta(minutes=2),
         "source": "telegram", "deltas": {"sessions_today": 1}},
    ])
    out = await al.run_once(pool=pool, redis=redis, store=store, llm=None)
    assert out["logged"] == 0
    store.record_log_event.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_filters_by_member_id() -> None:
    """A goal owned by member 2 must not be auto-logged from a member-3
    metric row."""
    goal = _pushup_goal()  # member_id=2
    metric_row = {"id": 1, "metric": "strength_workout", "value": 32,
                  "unit": "minutes", "started_at": datetime.now(UTC),
                  "ended_at": None, "member_id": 3,  # different member
                  "source": "x", "metadata": {}}
    pool = _conn_pool(rows=[metric_row])
    redis = _fake_redis_stub()
    store = _fake_store(active=[goal])
    out = await al.run_once(pool=pool, redis=redis, store=store)
    assert out["logged"] == 0


@pytest.mark.asyncio
async def test_watermark_persists_across_polls() -> None:
    """Second poll starts where the first left off."""
    metric1 = {"id": 10, "metric": "weight", "value": 80, "unit": "kg",
               "started_at": datetime.now(UTC), "ended_at": None,
               "member_id": 2, "source": "x", "metadata": {}}
    metric2 = {"id": 20, "metric": "weight", "value": 79, "unit": "kg",
               "started_at": datetime.now(UTC), "ended_at": None,
               "member_id": 2, "source": "x", "metadata": {}}
    redis = _fake_redis_stub()
    # First call returns metric1; second call returns metric2; we want
    # to verify the SQL is called with the right since_id each time.
    pool = _conn_pool(rows=[metric1])
    store = _fake_store(active=[])
    await al.run_once(pool=pool, redis=redis, store=store)
    # After first poll, watermark is 10
    assert redis._store["auto_log:last_metric_id"] == "10"
    # Build a new pool returning metric2 and verify it gets called with since_id=10
    pool2 = _conn_pool(rows=[metric2])
    store2 = _fake_store(active=[])
    await al.run_once(pool=pool2, redis=redis, store=store2)
    args = pool2._conn.fetch.await_args.args
    # args[0] is the SQL query; args[1] is since_id; args[2] is limit
    assert args[1] == 10
    assert redis._store["auto_log:last_metric_id"] == "20"


@pytest.mark.asyncio
async def test_notification_batched_per_poll() -> None:
    """If HealthKit sync drops many matching rows at once, the user
    gets exactly ONE batched notification (was per-goal-per-day cap)."""
    goal = _pushup_goal()
    now = datetime.now(UTC)
    # 6 matching metrics on the same day
    rows = []
    for i in range(6):
        rows.append({
            "id": 100 + i, "metric": "strength_workout", "value": 10,
            "unit": "minutes", "started_at": now - timedelta(minutes=i * 5),
            "ended_at": None, "member_id": 2,
            "source": "x", "metadata": {},
        })
    pool = _conn_pool(rows=rows, chat_id=620842725)
    redis = _fake_redis_stub()
    store = _fake_store(active=[goal])
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"sessions_today": 1}, "ts_iso": None,
    })}})
    out = await al.run_once(
        pool=pool, redis=redis, store=store, llm=llm,
    )
    # All 6 get logged
    assert out["logged"] == 6
    notifications = redis._store.get("_xadd:notify.outbound") or []
    # ONE batched notification, not 6
    assert len(notifications) == 1
    # The single message summarizes the batch
    payload = json.loads(notifications[0]["payload"])
    assert "6 events" in payload["text"] or "6 readings" in payload["text"]
    assert "added 6 set" in payload["text"]


# ── Gauge vs counter validation in auto-log ─────────────────────


@pytest.mark.asyncio
async def test_gauge_tracker_records_absolute_value_not_delta() -> None:
    """The original bug: LLM was storing -0.2 (delta-since-previous)
    instead of 92.8 (absolute weight). Validation should drop the
    delta because it's implausibly far from the actual metric value."""
    spec = {"trackers": [
        {"id": "weight_kg", "label": "Weight", "kind": "gauge",
         "reset": "weekly", "target": 85, "unit": "kg", "direction": "down"},
    ]}
    goal = {"id": 1, "title": "Lose weight", "tracker_spec": spec}
    metric = {"metric": "weight", "unit": "kg", "value": 92.8,
              "started_at": datetime.now(UTC)}
    llm = MagicMock()
    # LLM tries to return a delta — should be rejected as implausible
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"weight_kg": -0.2},
        "ts_iso": None,
    })}})
    deltas, _ = await al._classify_metric(
        llm=llm, goal=goal, metric_row=metric, classifier_model="x",
    )
    assert deltas == {}


@pytest.mark.asyncio
async def test_gauge_tracker_accepts_plausible_absolute_value() -> None:
    """LLM returns the actual absolute reading (close to event value)
    — accepted."""
    spec = {"trackers": [
        {"id": "weight_kg", "label": "Weight", "kind": "gauge",
         "reset": "weekly", "target": 85, "unit": "kg", "direction": "down"},
    ]}
    goal = {"id": 1, "title": "Lose weight", "tracker_spec": spec}
    metric = {"metric": "weight", "unit": "kg", "value": 92.8,
              "started_at": datetime.now(UTC)}
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"weight_kg": 92.8},
        "ts_iso": None,
    })}})
    deltas, _ = await al._classify_metric(
        llm=llm, goal=goal, metric_row=metric, classifier_model="x",
    )
    assert deltas == {"weight_kg": 92.8}


@pytest.mark.asyncio
async def test_counter_tracker_rejects_negative_value() -> None:
    """LLM tries to subtract from a counter — dropped silently."""
    spec = {"trackers": [
        {"id": "sessions_today", "label": "Sets", "kind": "counter",
         "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
    ]}
    goal = {"id": 1, "title": "Pushups", "tracker_spec": spec}
    metric = {"metric": "strength_workout", "unit": "minutes",
              "value": 32, "started_at": datetime.now(UTC)}
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"sessions_today": -1},
        "ts_iso": None,
    })}})
    deltas, _ = await al._classify_metric(
        llm=llm, goal=goal, metric_row=metric, classifier_model="x",
    )
    assert deltas == {}


@pytest.mark.asyncio
async def test_gauge_within_15pct_drift_accepted() -> None:
    """Tiny LLM rounding (e.g. 92.8 → 93) is accepted."""
    spec = {"trackers": [
        {"id": "weight_kg", "kind": "gauge", "reset": "weekly",
         "target": 85, "unit": "kg", "direction": "down", "label": "Weight"},
    ]}
    goal = {"id": 1, "title": "x", "tracker_spec": spec}
    metric = {"metric": "weight", "unit": "kg", "value": 92.8,
              "started_at": datetime.now(UTC)}
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"weight_kg": 93}, "ts_iso": None,
    })}})
    deltas, _ = await al._classify_metric(
        llm=llm, goal=goal, metric_row=metric, classifier_model="x",
    )
    assert deltas == {"weight_kg": 93.0}


# ── Notification rendering: gauge vs counter ────────────────────


@pytest.mark.asyncio
async def test_notification_for_gauge_says_is_now() -> None:
    """Gauge auto-log should read 'weight is now 92.8 kg', NOT
    '92.8 kg weight'."""
    spec = {"trackers": [
        {"id": "weight_kg", "label": "Weight", "kind": "gauge",
         "reset": "weekly", "target": 85, "unit": "kg", "direction": "down"},
    ]}
    goal = {"id": 1, "member_id": 2, "title": "Lose weight",
            "tracker_spec": spec}
    row = {"id": 1, "metric": "weight", "value": 92.8, "unit": "kg",
           "started_at": datetime.now(UTC), "ended_at": None,
           "member_id": 2, "source": "x", "metadata": {}}
    redis = _fake_redis_stub()
    pool = _conn_pool(rows=[row], chat_id=620842725)
    await al._notify_user(
        pool=pool, redis=redis, goal=goal, row=row,
        deltas={"weight_kg": 92.8}, event_ts=None,
    )
    payload = json.loads(redis._store["_xadd:notify.outbound"][0]["payload"])
    text = payload["text"]
    assert "is now 92.8 kg" in text
    # No "92.8 kg weight" awkwardness
    assert "92.8 kg weight" not in text


@pytest.mark.asyncio
async def test_notification_for_counter_says_added_to() -> None:
    spec = {"trackers": [
        {"id": "sessions_today", "label": "Sets", "kind": "counter",
         "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
    ]}
    goal = {"id": 1, "member_id": 2, "title": "Pushups",
            "tracker_spec": spec}
    row = {"id": 1, "metric": "strength_workout", "value": 32, "unit": "min",
           "started_at": datetime.now(UTC), "ended_at": None,
           "member_id": 2, "source": "x", "metadata": {}}
    redis = _fake_redis_stub()
    pool = _conn_pool(rows=[row], chat_id=620842725)
    await al._notify_user(
        pool=pool, redis=redis, goal=goal, row=row,
        deltas={"sessions_today": 1}, event_ts=None,
    )
    payload = json.loads(redis._store["_xadd:notify.outbound"][0]["payload"])
    text = payload["text"]
    assert "added 1 set" in text or "added 1 set to sets" in text


# ── Freshness + per-member batching ─────────────────────────────


@pytest.mark.asyncio
async def test_backfill_logs_silently_no_notification() -> None:
    """Historical rows (older than NOTIFY_FRESHNESS_HOURS) get logged
    into health_goal_log so the engine sees them, but no Telegram
    ping fires. This is the bug from the user re-scan after DB
    cleanup."""
    goal = _pushup_goal()
    now = datetime.now(UTC)
    rows = [{
        "id": 100 + i, "metric": "strength_workout", "value": 10,
        "unit": "minutes",
        # All 7 days old → backfill, not real-time
        "started_at": now - timedelta(days=7) + timedelta(minutes=i * 5),
        "ended_at": None, "member_id": 2,
        "source": "x", "metadata": {},
    } for i in range(3)]
    pool = _conn_pool(rows=rows, chat_id=620842725)
    redis = _fake_redis_stub()
    store = _fake_store(active=[goal])
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"sessions_today": 1}, "ts_iso": None,
    })}})
    out = await al.run_once(pool=pool, redis=redis, store=store, llm=llm)
    # All 3 logged
    assert out["logged"] == 3
    # Zero notifications — they were backfill
    notifications = redis._store.get("_xadd:notify.outbound") or []
    assert len(notifications) == 0
    assert out["notified"] == 0


@pytest.mark.asyncio
async def test_mixed_batch_only_fresh_notification() -> None:
    """If a batch contains 2 fresh + 1 stale event, only the fresh
    pair appears in the user-facing notification. The stale one is
    still logged into health_goal_log."""
    goal = _pushup_goal()
    now = datetime.now(UTC)
    rows = [
        # stale (8 hours old)
        {"id": 100, "metric": "strength_workout", "value": 10,
         "unit": "minutes", "started_at": now - timedelta(hours=8),
         "ended_at": None, "member_id": 2,
         "source": "x", "metadata": {}},
        # fresh (1 hour old)
        {"id": 101, "metric": "strength_workout", "value": 10,
         "unit": "minutes", "started_at": now - timedelta(hours=1),
         "ended_at": None, "member_id": 2,
         "source": "x", "metadata": {}},
        # fresh (30 min old)
        {"id": 102, "metric": "strength_workout", "value": 10,
         "unit": "minutes", "started_at": now - timedelta(minutes=30),
         "ended_at": None, "member_id": 2,
         "source": "x", "metadata": {}},
    ]
    pool = _conn_pool(rows=rows, chat_id=620842725)
    redis = _fake_redis_stub()
    store = _fake_store(active=[goal])
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"sessions_today": 1}, "ts_iso": None,
    })}})
    out = await al.run_once(pool=pool, redis=redis, store=store, llm=llm)
    assert out["logged"] == 3
    # One notification summarizing the 2 fresh events
    notifications = redis._store.get("_xadd:notify.outbound") or []
    assert len(notifications) == 1
    payload = json.loads(notifications[0]["payload"])
    assert "2 events" in payload["text"]


@pytest.mark.asyncio
async def test_per_member_cap_across_goals() -> None:
    """When a member has many active goals matching the same metric,
    we cap notifications per member-per-cycle to MAX_AUTO_NOTIFY_
    PER_MEMBER_PER_DAY."""
    # 5 goals owned by the same member, all matching the same metric
    goals = [
        {**_pushup_goal(), "id": i, "title": f"Goal {i}"}
        for i in range(1, 6)
    ]
    now = datetime.now(UTC)
    # One fresh metric that matches all goals
    rows = [{
        "id": 100, "metric": "strength_workout", "value": 10,
        "unit": "minutes", "started_at": now - timedelta(minutes=5),
        "ended_at": None, "member_id": 2,
        "source": "x", "metadata": {},
    }]
    pool = _conn_pool(rows=rows, chat_id=620842725)
    redis = _fake_redis_stub()
    store = _fake_store(active=goals)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": json.dumps({
        "deltas": {"sessions_today": 1}, "ts_iso": None,
    })}})
    out = await al.run_once(pool=pool, redis=redis, store=store, llm=llm)
    # All 5 goals got logged (5 logs from 1 row × 5 goals)
    assert out["logged"] == 5
    # But notifications capped at the per-member ceiling
    notifications = redis._store.get("_xadd:notify.outbound") or []
    assert len(notifications) == al.MAX_AUTO_NOTIFY_PER_MEMBER_PER_DAY


@pytest.mark.asyncio
async def test_batched_gauge_shows_latest_value() -> None:
    """6 weight readings in one cycle should produce ONE message
    showing the most recent value, not 6 messages."""
    spec = {"trackers": [{
        "id": "weight_kg", "label": "Weight", "kind": "gauge",
        "reset": "weekly", "target": 85, "unit": "kg", "direction": "down",
    }]}
    goal = {"id": 5, "member_id": 2, "title": "Lose weight",
            "tracker_spec": spec}
    now = datetime.now(UTC)
    rows = [{
        "id": 100 + i, "metric": "weight", "value": 92.0 + (i * 0.1),
        "unit": "kg",
        "started_at": now - timedelta(minutes=(5 - i) * 10),
        "ended_at": None, "member_id": 2,
        "source": "x", "metadata": {},
    } for i in range(6)]
    # Latest reading (i=5) lands closest to now, value 92.5
    pool = _conn_pool(rows=rows, chat_id=620842725)
    redis = _fake_redis_stub()
    store = _fake_store(active=[goal])
    llm = MagicMock()

    # LLM returns the row's value as the absolute reading
    def _chat(messages, **kw):
        # Pull value from the user message description
        user_msg = messages[-1]["content"]
        # The description has the value in 'weight — 92.X kg at ...'
        import re as _re
        m = _re.search(r"(\d+\.\d+)", user_msg)
        v = float(m.group(1)) if m else 92.0
        return {"message": {"content": json.dumps({
            "deltas": {"weight_kg": v}, "ts_iso": None,
        })}}

    llm.chat = AsyncMock(side_effect=_chat)
    out = await al.run_once(pool=pool, redis=redis, store=store, llm=llm)
    assert out["logged"] == 6
    notifications = redis._store.get("_xadd:notify.outbound") or []
    assert len(notifications) == 1
    payload = json.loads(notifications[0]["payload"])
    text = payload["text"]
    assert "6 readings" in text
    assert "92.5" in text  # latest
    # No "is now" / "added" awkwardness when batched
    assert "is now" not in text
