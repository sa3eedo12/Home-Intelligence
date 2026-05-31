"""Tests for orchestrator.goals_chat — intent classification + handlers."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.goals_chat import (
    GoalsChatHandler,
    GoalsHandlerResult,
    _humanize_list,
    _match_goal,
    _parse_json_blob,
)


# ── Test doubles ────────────────────────────────────────────────


def _llm_returning(*payloads: dict) -> MagicMock:
    """Build an LLM mock that returns chat responses with the given
    JSON payloads in order. Wraps each in the Ollama chat-shaped
    {message: {content: ...}} envelope."""
    responses = [
        {"message": {"content": json.dumps(p)}} for p in payloads
    ]
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=responses)
    return llm


def _fake_goals_store(**handlers):
    return SimpleNamespace(
        list_active=AsyncMock(return_value=handlers.get("active", [])),
        list_all_for_member=AsyncMock(return_value=handlers.get("all", [])),
        create=AsyncMock(return_value=handlers.get("create_id", 1)),
        update_plan=AsyncMock(return_value=None),
        set_status=AsyncMock(return_value=None),
        set_quiet_until=AsyncMock(return_value=None),
        get_progress=AsyncMock(return_value=handlers.get("progress")),
        recent_progress=AsyncMock(return_value=handlers.get("recent", [])),
        upsert_progress=AsyncMock(return_value=None),
        excuses_this_week=AsyncMock(return_value=handlers.get("excuses_used", 0)),
        excuse_today=AsyncMock(return_value=None),
        recent_log=AsyncMock(return_value=handlers.get("log_rows", [])),
        record_log_event=AsyncMock(return_value=handlers.get("log_id", 1)),
        list_milestones=AsyncMock(return_value=handlers.get("milestones", [])),
    )


def _fake_chore_store(**handlers):
    return SimpleNamespace(
        list_status=AsyncMock(return_value=handlers.get("status", [])),
        log_by_name=AsyncMock(return_value=handlers.get("matched_id")),
    )


def _fake_nag_store(**handlers):
    return SimpleNamespace(
        set=AsyncMock(return_value=handlers.get("updated", {
            "member_id": 2, "weekday_start_hour": 18, "weekday_end_hour": 21,
            "weekend_start_hour": 10, "weekend_end_hour": 21,
            "timezone": "Asia/Dubai", "is_default": False,
        })),
        get=AsyncMock(return_value={
            "weekday_start_hour": 14, "weekday_end_hour": 21,
            "weekend_start_hour": 10, "weekend_end_hour": 21,
            "timezone": "Asia/Dubai", "is_default": True,
        }),
    )


def _build(**kwargs) -> GoalsChatHandler:
    return GoalsChatHandler(
        llm=kwargs.get("llm", MagicMock()),
        goals_store=kwargs.get("goals_store", _fake_goals_store()),
        chore_store=kwargs.get("chore_store", _fake_chore_store()),
        nag_store=kwargs.get("nag_store", _fake_nag_store()),
    )


MEMBER = {"id": 2, "name": "Saeed"}


# ── Helpers ─────────────────────────────────────────────────────


def test_humanize_list_oxford() -> None:
    assert _humanize_list([]) == ""
    assert _humanize_list(["a"]) == "a"
    assert _humanize_list(["a", "b"]) == "a and b"
    assert _humanize_list(["a", "b", "c"]) == "a, b, and c"


def test_match_goal_finds_by_title_substring() -> None:
    goals = [
        {"id": 1, "title": "Work out 4x a week"},
        {"id": 2, "title": "Drink more water"},
    ]
    assert _match_goal(goals, "water")["id"] == 2
    assert _match_goal(goals, "WORKOUT") is None  # not present
    assert _match_goal(goals, "work out")["id"] == 1


def test_parse_json_blob_strips_fence() -> None:
    out = _parse_json_blob('```json\n{"intent":"create_goal"}\n```')
    assert out == {"intent": "create_goal"}
    out2 = _parse_json_blob('here is the answer: {"intent":"x"} ok')
    assert out2 == {"intent": "x"}
    assert _parse_json_blob("") is None
    assert _parse_json_blob("no json here") is None


# ── Classifier fall-through ─────────────────────────────────────


@pytest.mark.asyncio
async def test_general_chat_falls_through() -> None:
    llm = _llm_returning({"intent": "general_chat"})
    h = _build(llm=llm)
    r = await h.try_handle("what's the weather", member=MEMBER)
    assert r.handled is False
    assert r.intent == "general_chat"


@pytest.mark.asyncio
async def test_unknown_intent_falls_through() -> None:
    llm = _llm_returning({"intent": "abracadabra"})
    h = _build(llm=llm)
    r = await h.try_handle("hi", member=MEMBER)
    assert r.handled is False


@pytest.mark.asyncio
async def test_no_member_falls_through() -> None:
    llm = _llm_returning({"intent": "create_goal"})
    h = _build(llm=llm)
    r = await h.try_handle("any text", member=None)
    assert r.handled is False
    # We never even called the classifier
    llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_classifier_failure_safe_fallthrough() -> None:
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("ollama down"))
    h = _build(llm=llm)
    r = await h.try_handle("anything", member=MEMBER)
    assert r.handled is False


# ── create_goal flow ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_goal_writes_goal_and_returns_reply() -> None:
    classify = {
        "intent": "create_goal",
        "title": "Work out 4x a week",
        "description": "I want to work out four times a week.",
    }
    plan = {
        "plan_text": "Stay consistent — four short sessions across the week.",
        "tracker_spec": {
            "trackers": [
                {"id": "workouts_this_week", "label": "Workouts",
                 "kind": "counter", "reset": "weekly",
                 "target": 4, "unit": "workout", "direction": "up"},
            ],
        },
        "milestones": [{"due_date": "2026-07-01",
                        "target_description": "Hit 4 weeks in a row"}],
    }
    llm = _llm_returning(classify, plan)
    goals = _fake_goals_store()
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("I want to work out 4x a week", member=MEMBER)
    assert r.handled is True
    assert r.intent == "create_goal"
    assert "Work out 4x a week" in r.text
    # Reply mentions the tracker we'll be tracking
    assert "Workouts" in r.text
    assert "4 workout per week" in r.text or "4 workout" in r.text
    goals.create.assert_awaited_once()
    create_kwargs = goals.create.await_args.kwargs
    assert create_kwargs["member_id"] == 2
    assert create_kwargs["title"] == "Work out 4x a week"
    assert create_kwargs["tracker_spec"]["trackers"][0]["id"] == "workouts_this_week"
    # Milestones were persisted via update_plan
    goals.update_plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_goal_falls_back_on_planner_failure() -> None:
    classify = {"intent": "create_goal", "title": "T", "description": "D"}
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        {"message": {"content": json.dumps(classify)}},
        RuntimeError("planner died"),
    ])
    goals = _fake_goals_store()
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("D", member=MEMBER)
    assert r.handled is True
    # Goal still created with the fallback plan (no tracker_spec on failure)
    goals.create.assert_awaited_once()
    args = goals.create.await_args.kwargs
    assert args.get("tracker_spec") is None
    assert "check in" in (args.get("plan_text") or "").lower()


# ── check_progress ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_progress_returns_engine_status_line() -> None:
    """check_progress now runs the engine against recent_log."""
    today = datetime.now(UTC)
    spec = {
        "trackers": [
            {"id": "workouts_this_week", "label": "Workouts",
             "kind": "counter", "reset": "weekly", "target": 4,
             "unit": "workout", "direction": "up"},
        ],
    }
    llm = _llm_returning({"intent": "check_progress"})
    goals = _fake_goals_store(
        active=[{
            "id": 1, "member_id": 2, "title": "Strong",
            "tracker_spec": spec,
        }],
        log_rows=[
            {"ts": today, "deltas": {"workouts_this_week": 1}},
            {"ts": today, "deltas": {"workouts_this_week": 1}},
            {"ts": today, "deltas": {"workouts_this_week": 1}},
        ],
    )
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("how am I doing", member=MEMBER)
    assert r.handled is True
    assert "Strong" in r.text
    assert "3 of 4" in r.text


@pytest.mark.asyncio
async def test_check_progress_no_active_goals() -> None:
    llm = _llm_returning({"intent": "check_progress"})
    goals = _fake_goals_store(active=[])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("am i on track", member=MEMBER)
    assert r.handled is True
    assert "don't have any active goals" in r.text


# ── log_workout (generic log_event) ─────────────────────────────


@pytest.mark.asyncio
async def test_log_workout_classifies_text_to_tracker_deltas() -> None:
    """log_workout now writes a health_goal_log row with LLM-parsed
    deltas, not a blanket workout_completed=True on every goal."""
    classify = {"intent": "log_workout", "note": None}
    log_deltas = {"sessions_today": 1, "reps_today": 30}
    llm = _llm_returning(classify, log_deltas)
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
            {"id": "reps_today", "label": "Reps", "kind": "counter",
             "reset": "daily", "target": 150, "unit": "rep", "direction": "up"},
        ],
    }
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Pushups", "tracker_spec": spec},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("did 30 pushups after maghrib", member=MEMBER)
    assert r.handled is True
    goals.record_log_event.assert_awaited_once()
    deltas = goals.record_log_event.await_args.kwargs["deltas"]
    assert deltas == {"sessions_today": 1.0, "reps_today": 30.0}
    assert "Logged" in r.text


@pytest.mark.asyncio
async def test_log_workout_autoheals_spec_when_missing_then_logs() -> None:
    """The original bug: a goal created under the old engine had no
    tracker_spec. User says "did 12 pushups" and gets a dead-end
    "tell me how to measure it" reply. Fix: synthesize the spec
    on the fly via the planner, persist it, then proceed with logging."""
    classify = {"intent": "log_workout"}
    generated_plan = {
        "plan_text": "Daily pushups after each prayer.",
        "tracker_spec": {
            "trackers": [
                {"id": "sessions_today", "label": "Sets",
                 "kind": "counter", "reset": "daily", "target": 5,
                 "unit": "set", "direction": "up"},
            ],
        },
        "milestones": [],
    }
    log_deltas = {"sessions_today": 1}
    llm = _llm_returning(classify, generated_plan, log_deltas)
    goals = _fake_goals_store(active=[
        {"id": 4, "member_id": 2, "title": "Max Pushups After Prayer Daily",
         "description": "I want to do as many pushups as I can after every prayer daily.",
         "tracker_spec": None},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("did 12 pushups after dhuhr", member=MEMBER)
    assert r.handled is True
    # Spec was auto-generated and persisted before the log
    goals.update_plan.assert_awaited_once()
    persisted_spec = goals.update_plan.await_args.kwargs["tracker_spec"]
    assert persisted_spec["trackers"][0]["id"] == "sessions_today"
    # Log went through against the freshly-installed spec
    goals.record_log_event.assert_awaited_once()
    assert goals.record_log_event.await_args.kwargs["deltas"] == {
        "sessions_today": 1.0
    }
    assert "Logged" in r.text


@pytest.mark.asyncio
async def test_log_workout_when_autoheal_also_fails() -> None:
    """If the planner LLM is unreachable, the autoheal can't write a
    spec — fall through to the helpful 'tell me how to measure it'
    message rather than crashing."""
    classify = {"intent": "log_workout"}
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        {"message": {"content": json.dumps(classify)}},
        RuntimeError("planner down"),
    ])
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Old goal",
         "description": "an old goal", "tracker_spec": None},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("did pushups", member=MEMBER)
    assert r.handled is True
    assert "doesn't have any trackers" in r.text
    goals.record_log_event.assert_not_called()


@pytest.mark.asyncio
async def test_log_workout_uses_keyword_fallback_when_llm_fails() -> None:
    """If the LLM delta-classifier raises, the deterministic
    log_hints keyword mapper takes over so the user still gets logged."""
    classify = {"intent": "log_workout"}
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        {"message": {"content": json.dumps(classify)}},
        RuntimeError("llm down"),
    ])
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
        "log_hints": [
            {"if_mentions": ["pushup"], "increment": {"sessions_today": 1}},
        ],
    }
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Pushups", "tracker_spec": spec},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("just did 20 pushups", member=MEMBER)
    assert r.handled is True
    goals.record_log_event.assert_awaited_once()
    deltas = goals.record_log_event.await_args.kwargs["deltas"]
    assert deltas == {"sessions_today": 1.0}


# ── skip_workout ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_workout_excuses_within_budget() -> None:
    llm = _llm_returning({"intent": "skip_workout", "reason": "knee pain"})
    goals = _fake_goals_store(
        active=[{
            "id": 1, "member_id": 2, "title": "Run more",
            "workout_budget": {"required_per_week": 4,
                                "flexible_rest_per_week": 2},
        }],
        excuses_used=0,
    )
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("skipping today", member=MEMBER)
    assert r.handled is True
    assert "Run more" in r.text
    assert "rest day" in r.text
    goals.excuse_today.assert_awaited()


@pytest.mark.asyncio
async def test_skip_workout_flags_when_over_budget() -> None:
    llm = _llm_returning({"intent": "skip_workout", "reason": None})
    goals = _fake_goals_store(
        active=[{
            "id": 1, "member_id": 2, "title": "Run more",
            "workout_budget": {"required_per_week": 4,
                                "flexible_rest_per_week": 1},
        }],
        excuses_used=2,
    )
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("skip", member=MEMBER)
    assert r.handled is True
    assert "budget" in r.text
    assert "weekly review" in r.text


# ── nag windows ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_nag_windows_passes_int_fields() -> None:
    llm = _llm_returning({
        "intent": "set_nag_windows",
        "weekday_start_hour": 18,
    })
    nag = _fake_nag_store()
    h = _build(llm=llm, nag_store=nag)
    r = await h.try_handle(
        "don't nag me before 6pm weekdays", member=MEMBER,
    )
    assert r.handled is True
    nag.set.assert_awaited_once()
    call = nag.set.await_args
    assert call.args == (2,)
    assert call.kwargs == {"weekday_start_hour": 18}
    assert "Weekdays 18:00 to 21:00" in r.text


@pytest.mark.asyncio
async def test_set_nag_windows_handles_validation_error() -> None:
    llm = _llm_returning({
        "intent": "set_nag_windows",
        "weekday_start_hour": 22, "weekday_end_hour": 8,
    })
    nag = SimpleNamespace(
        set=AsyncMock(side_effect=ValueError("weekday end must be > start")),
        get=AsyncMock(return_value={}),
    )
    h = _build(llm=llm, nag_store=nag)
    r = await h.try_handle("change window", member=MEMBER)
    assert r.handled is True
    assert "Couldn't update" in r.text


# ── chores ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_chore_logs_when_match_found() -> None:
    llm = _llm_returning({"intent": "complete_chore", "name": "vacuum"})
    chores = _fake_chore_store(matched_id=1)
    h = _build(llm=llm, chore_store=chores)
    r = await h.try_handle("just vacuumed", member=MEMBER)
    assert r.handled is True
    assert "Logged" in r.text
    chores.log_by_name.assert_awaited_once()
    assert chores.log_by_name.await_args.args == ("vacuum",)


@pytest.mark.asyncio
async def test_complete_chore_when_no_match() -> None:
    llm = _llm_returning({"intent": "complete_chore", "name": "moonwalk"})
    chores = _fake_chore_store(matched_id=None)
    h = _build(llm=llm, chore_store=chores)
    r = await h.try_handle("moonwalked", member=MEMBER)
    assert r.handled is True
    assert "couldn't match" in r.text


# ── goal state changes ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_goal_calls_set_status() -> None:
    llm = _llm_returning({"intent": "pause_goal", "which": "strong"})
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Strong"},
        {"id": 2, "member_id": 2, "title": "Sleep more"},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("pause the strong goal", member=MEMBER)
    assert r.handled is True
    goals.set_status.assert_awaited_once_with(1, "paused", note="user requested")


@pytest.mark.asyncio
async def test_mute_goal_sets_quiet_until() -> None:
    llm = _llm_returning({
        "intent": "mute_goal", "duration_hours": 48, "which": None,
    })
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Strong"},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("mute for 2 days", member=MEMBER)
    assert r.handled is True
    goals.set_quiet_until.assert_awaited_once()
    call = goals.set_quiet_until.await_args
    assert call.args == (1,)
    assert isinstance(call.kwargs["until"], datetime)


# ── list_chores ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_chores_returns_humanized() -> None:
    from home_agents_sdk.chore_store import ChoreStatus
    llm = _llm_returning({"intent": "list_chores"})
    chores = _fake_chore_store(status=[
        ChoreStatus(
            template_id=1, name="Vacuum", category="cleaning",
            cadence_days=7, grace_days=2, auto_detect_kind=None,
            auto_detect_entity=None, last_done_at=None, last_done_by=None,
            next_due_on=date(2026, 5, 27), days_overdue=2, status="overdue",
            description=None,
        ),
    ])
    h = _build(llm=llm, chore_store=chores)
    r = await h.try_handle("what chores are overdue", member=MEMBER)
    assert r.handled is True
    assert "Vacuum" in r.text
    assert "Overdue" in r.text


@pytest.mark.asyncio
async def test_list_chores_when_empty() -> None:
    llm = _llm_returning({"intent": "list_chores"})
    chores = _fake_chore_store(status=[])
    h = _build(llm=llm, chore_store=chores)
    r = await h.try_handle("any chores", member=MEMBER)
    assert r.handled is True
    assert "Enjoy" in r.text


# ── weekly_review ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weekly_review_counts_workouts() -> None:
    llm = _llm_returning({"intent": "weekly_review"})
    goals = _fake_goals_store(
        active=[{
            "id": 1, "member_id": 2, "title": "Run more",
            "workout_budget": {"required_per_week": 4},
        }],
        recent=[
            {"workout_completed": True}, {"workout_completed": True},
            {"workout_completed": False}, {"workout_completed": True},
        ],
    )
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("weekly review please", member=MEMBER)
    assert r.handled is True
    assert "Run more" in r.text
    assert "3 workouts" in r.text
    assert "target 4" in r.text


# ── Dispatcher failure handling ─────────────────────────────────


@pytest.mark.asyncio
async def test_handler_failure_returns_apology() -> None:
    llm = _llm_returning({"intent": "create_goal", "title": "x",
                          "description": "y"})
    # Goals store create raises
    goals = _fake_goals_store()
    goals.create = AsyncMock(side_effect=RuntimeError("db broke"))
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("start a goal", member=MEMBER)
    assert r.handled is True
    assert "something went wrong" in r.text


# ── explain_plan + conversational context ────────────────────────


def _fake_redis_stub() -> SimpleNamespace:
    """Tiny in-memory Redis stub — just enough for SET ex= and GET and DELETE."""
    store: dict[str, str] = {}

    async def _get(key):
        return store.get(key)

    async def _set(key, value, ex=None):
        store[key] = value
        return True

    async def _delete(*keys):
        n = 0
        for k in keys:
            if k in store:
                del store[k]
                n += 1
        return n

    return SimpleNamespace(_store=store, get=AsyncMock(side_effect=_get),
                           set=AsyncMock(side_effect=_set),
                           delete=AsyncMock(side_effect=_delete))


def _fake_goals_store_with_get(*goals):
    """Like _fake_goals_store but includes get() (needed by context load)."""
    by_id = {int(g["id"]): g for g in goals}
    base = _fake_goals_store(active=list(goals), all=list(goals))
    base.get = AsyncMock(side_effect=lambda gid: by_id.get(int(gid)))
    base.list_milestones = AsyncMock(return_value=[])
    return base


@pytest.mark.asyncio
async def test_explain_plan_renders_full_plan_card() -> None:
    llm = _llm_returning({"intent": "explain_plan"})
    goal = {
        "id": 1, "member_id": 2, "title": "Double pushups in 2 weeks",
        "description": "Train 4x to double max pushup count",
        "plan_text": "Greasing the groove: 5 short sets across each "
                     "training day, finishing with one max-effort set.",
        "tracker_spec": {
            "trackers": [
                {"id": "sessions_today", "label": "Pushup sets",
                 "kind": "counter", "reset": "daily", "target": 5,
                 "unit": "set", "direction": "up"},
                {"id": "reps_today", "label": "Pushup reps",
                 "kind": "counter", "reset": "daily", "target": 50,
                 "unit": "rep", "direction": "up"},
            ],
        },
        "status": "active",
    }
    goals = _fake_goals_store_with_get(goal)
    goals.list_milestones = AsyncMock(return_value=[
        {"due_date": date(2026, 6, 5),
         "target_description": "Hit 1.5× starting max"},
        {"due_date": date(2026, 6, 12),
         "target_description": "Hit 2× starting max"},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("what would the plan involve?", member=MEMBER)
    assert r.handled is True
    assert r.intent == "explain_plan"
    assert "Double pushups in 2 weeks" in r.text
    assert "Greasing the groove" in r.text
    # tracker labels + targets render in the plan card
    assert "Pushup sets" in r.text
    assert "5 set" in r.text
    assert "Hit 1.5× starting max" in r.text


@pytest.mark.asyncio
async def test_explain_plan_handles_missing_plan_text() -> None:
    llm = _llm_returning({"intent": "explain_plan"})
    goal = {
        "id": 1, "member_id": 2, "title": "Sleep more",
        "description": "Get to bed earlier",
        "plan_text": None,
        "workout_budget": {},
        "status": "active",
    }
    goals = _fake_goals_store_with_get(goal)
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("what does this look like", member=MEMBER)
    assert r.handled is True
    assert "haven't written a detailed plan" in r.text


@pytest.mark.asyncio
async def test_create_goal_stashes_context_in_redis() -> None:
    classify = {"intent": "create_goal", "title": "T", "description": "D"}
    plan = {
        "plan_text": "Plan.", "metric_links": [],
        "workout_budget": None, "milestones": [],
    }
    llm = _llm_returning(classify, plan)
    redis = _fake_redis_stub()
    goals = _fake_goals_store()
    goals.create = AsyncMock(return_value=42)
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    r = await h.try_handle("T", member=MEMBER)
    assert r.handled is True
    # Context was written
    assert "goals_chat:context:2" in redis._store
    saved = json.loads(redis._store["goals_chat:context:2"])
    assert saved["last_goal_id"] == 42
    assert saved["last_intent"] == "create_goal"


@pytest.mark.asyncio
async def test_followup_explain_plan_uses_context_for_goal_resolution() -> None:
    """The full bug-repro test: user creates a goal, then asks a vague
    'what would the plan involve?' — must resolve to the freshly-created
    goal even though the text contains no goal-identifying words."""
    # First message: create
    classify_create = {"intent": "create_goal", "title": "Double pushups",
                       "description": "Double pushups in 2 weeks"}
    plan = {
        "plan_text": "Greasing the groove.", "metric_links": [],
        "workout_budget": {"required_per_week": 4}, "milestones": [],
    }
    # Second message: explain_plan (after context is set, classifier
    # sees recent-context block in the system prompt and returns
    # explain_plan)
    classify_explain = {"intent": "explain_plan"}
    llm = _llm_returning(classify_create, plan, classify_explain)
    redis = _fake_redis_stub()
    goal_row = {
        "id": 42, "member_id": 2, "title": "Double pushups",
        "description": "Double pushups in 2 weeks",
        "plan_text": "Greasing the groove.",
        "workout_budget": {"required_per_week": 4},
        "status": "active",
    }
    goals = _fake_goals_store_with_get(goal_row)
    goals.create = AsyncMock(return_value=42)
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    r1 = await h.try_handle(
        "I want to double the number of pushups I can do in 2 weeks",
        member=MEMBER,
    )
    assert r1.handled is True
    r2 = await h.try_handle("what would the plan involve?", member=MEMBER)
    assert r2.handled is True
    assert r2.intent == "explain_plan"
    assert "Double pushups" in r2.text
    assert "Greasing the groove" in r2.text


@pytest.mark.asyncio
async def test_classifier_prompt_includes_recent_context_when_present() -> None:
    """The classifier should see the just-created goal in its system
    prompt so it can disambiguate vague follow-ups. Validates the
    prompt-building path, not a live LLM call."""
    prompt = GoalsChatHandler._classifier_prompt(
        "tell me more about it",
        context={
            "last_goal_id": 1, "last_goal_title": "Sleep more",
            "last_intent": "create_goal", "age_seconds": 60,
        },
    )
    assert "RECENT CONTEXT" in prompt["system"]
    assert "Sleep more" in prompt["system"]
    assert "explain_plan" in prompt["system"]


@pytest.mark.asyncio
async def test_classifier_prompt_no_context_block_when_absent() -> None:
    prompt = GoalsChatHandler._classifier_prompt("tell me", context=None)
    assert "RECENT CONTEXT" not in prompt["system"]


@pytest.mark.asyncio
async def test_load_context_drops_stale_when_goal_abandoned() -> None:
    """If the stashed goal_id maps to an abandoned/missing goal, the
    context is treated as stale and ignored."""
    redis = _fake_redis_stub()
    redis._store["goals_chat:context:2"] = json.dumps({
        "last_goal_id": 99, "last_intent": "create_goal",
        "ts": datetime.now(UTC).isoformat(),
    })
    goals = _fake_goals_store_with_get({
        "id": 99, "member_id": 2, "title": "Old goal",
        "status": "abandoned",
    })
    h = GoalsChatHandler(
        llm=MagicMock(), goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    ctx = await h._load_context(2)
    assert ctx is None


@pytest.mark.asyncio
async def test_explain_plan_falls_back_to_first_goal_with_no_context() -> None:
    """No conversational context, no explicit 'which' → use the most
    recent active goal (head of list_active output)."""
    llm = _llm_returning({"intent": "explain_plan"})
    goal = {
        "id": 7, "member_id": 2, "title": "Strong",
        "description": "Get strong", "plan_text": "Lift.",
        "workout_budget": {"required_per_week": 3},
        "status": "active",
    }
    goals = _fake_goals_store_with_get(goal)
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("what's the plan?", member=MEMBER)
    assert r.handled is True
    assert "Strong" in r.text


# ── refine_goal flow ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refine_goal_updates_existing_plan_not_creates_new() -> None:
    """User suggests an alternative approach after creating a goal —
    must call update_plan on the existing goal, NOT create() a new
    one. This is the exact regression bug from real usage."""
    classify = {"intent": "refine_goal",
                "refinement": "do pushups after every prayer, daily"}
    new_plan = {
        "plan_text": "Daily after each prayer: as many as you can.",
        "tracker_spec": {
            "trackers": [
                {"id": "sessions_today", "label": "Pushup sets",
                 "kind": "counter", "reset": "daily", "target": 5,
                 "unit": "set", "direction": "up"},
            ],
        },
        "milestones": [],
    }
    llm = _llm_returning(classify, new_plan)
    goal_row = {
        "id": 5, "member_id": 2, "title": "Double Pushups in 2 Weeks",
        "description": "Double pushup max in 14 days",
        "plan_text": "Old plan: 3-4x/week",
        "tracker_spec": {
            "trackers": [
                {"id": "workouts_this_week", "label": "Workouts",
                 "kind": "counter", "reset": "weekly", "target": 3,
                 "unit": "workout", "direction": "up"},
            ],
        },
        "status": "active",
    }
    goals = _fake_goals_store_with_get(goal_row)
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle(
        "I was thinking doing as many push ups as I can after every prayer daily. What do you think",
        member=MEMBER,
    )
    assert r.handled is True
    assert r.intent == "refine_goal"
    assert "Double Pushups in 2 Weeks" in r.text
    assert "Daily after each prayer" in r.text
    # The critical assertion: no new goal was created
    goals.create.assert_not_called()
    goals.update_plan.assert_awaited_once()
    update_call = goals.update_plan.await_args
    assert update_call.args == (5,)
    assert "Daily after each prayer" in update_call.kwargs["plan_text"]
    # Tracker spec was rewritten to the new shape
    assert update_call.kwargs["tracker_spec"]["trackers"][0]["id"] == "sessions_today"


@pytest.mark.asyncio
async def test_refine_goal_with_no_active_goals_apologizes() -> None:
    classify = {"intent": "refine_goal", "refinement": "make it daily"}
    llm = _llm_returning(classify)
    goals = _fake_goals_store(active=[])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("make it daily", member=MEMBER)
    assert r.handled is True
    assert "no active goal to refine" in r.text


@pytest.mark.asyncio
async def test_refine_goal_uses_context_when_no_which_arg() -> None:
    """Without explicit 'which', refine_goal resolves to the most
    recently touched goal (from Redis context)."""
    classify = {"intent": "refine_goal", "refinement": "Tue/Thu/Sat"}
    plan = {
        "plan_text": "Updated.",
        "tracker_spec": {"trackers": [
            {"id": "workouts_this_week", "label": "Workouts",
             "kind": "counter", "reset": "weekly", "target": 3,
             "unit": "workout", "direction": "up"},
        ]},
        "milestones": [],
    }
    llm = _llm_returning(classify, plan)
    redis = _fake_redis_stub()
    redis._store["goals_chat:context:2"] = json.dumps({
        "last_goal_id": 99, "last_intent": "create_goal",
        "ts": datetime.now(UTC).isoformat(),
    })
    target = {
        "id": 99, "member_id": 2, "title": "Run more",
        "description": "Run 3x", "plan_text": "old",
        "workout_budget": {"required_per_week": 3},
        "status": "active",
    }
    other = {
        "id": 100, "member_id": 2, "title": "Sleep more",
        "description": "x", "plan_text": "y",
        "workout_budget": {}, "status": "active",
    }
    goals = _fake_goals_store_with_get(other, target)  # `other` first in list_active
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    r = await h.try_handle("shift to Tue Thu Sat", member=MEMBER)
    assert r.handled is True
    # Resolved via context, NOT head-of-list
    update_call = goals.update_plan.await_args
    assert update_call.args == (99,)
    assert "Run more" in r.text


def test_classifier_prompt_mentions_refine_in_context_block() -> None:
    prompt = GoalsChatHandler._classifier_prompt(
        "what if I did it daily instead",
        context={
            "last_goal_id": 1, "last_goal_title": "Pushups",
            "last_intent": "create_goal", "age_seconds": 60,
        },
    )
    # The context block must teach the classifier that mid-conversation
    # "I was thinking instead..." is refine, not create.
    assert "refine_goal" in prompt["system"]
    assert "different outcome" in prompt["system"]


# ── Time-aware log classification ───────────────────────────────


@pytest.mark.asyncio
async def test_log_workout_resolves_ts_iso_from_classifier() -> None:
    """When the LLM returns a ts_iso for 'after Dhuhr earlier today',
    the log row is written with that timestamp instead of now()."""
    classify = {"intent": "log_workout"}
    # Today's Dhuhr in Dubai is ~12:30 local. Pick a fixed valid past ts.
    past_ts_local = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    log_response = {
        "deltas": {"sessions_today": 1, "pushups_today": 12},
        "ts_iso": past_ts_local,
        "reasoning_brief": "after Dhuhr ~4h ago",
    }
    llm = _llm_returning(classify, log_response)
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
            {"id": "pushups_today", "label": "Pushups", "kind": "counter",
             "reset": "daily", "target": 100, "unit": "pushup",
             "direction": "up"},
        ],
    }
    goals = _fake_goals_store(active=[
        {"id": 4, "member_id": 2, "title": "Pushups", "tracker_spec": spec},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle(
        "did 12 pushups earlier today after Dhuhr prayer", member=MEMBER,
    )
    assert r.handled is True
    goals.record_log_event.assert_awaited_once()
    call = goals.record_log_event.await_args
    # ts was forwarded, not None
    assert call.kwargs["ts"] is not None
    assert abs(
        (call.kwargs["ts"].astimezone(UTC) -
         datetime.fromisoformat(past_ts_local).astimezone(UTC)).total_seconds()
    ) < 5
    # Confirmation message acknowledges the time
    assert "earlier today" in r.text.lower()


@pytest.mark.asyncio
async def test_log_workout_defaults_to_now_when_no_ts_signal() -> None:
    """When the LLM returns ts_iso=null (no temporal hint), the store
    gets ts=None and defaults to now()."""
    classify = {"intent": "log_workout"}
    log_response = {
        "deltas": {"sessions_today": 1},
        "ts_iso": None,
        "reasoning_brief": "none",
    }
    llm = _llm_returning(classify, log_response)
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
    }
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "g", "tracker_spec": spec},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("just did a set", member=MEMBER)
    assert r.handled is True
    assert goals.record_log_event.await_args.kwargs["ts"] is None
    assert "earlier today" not in r.text.lower()


@pytest.mark.asyncio
async def test_log_workout_supports_legacy_bare_delta_shape() -> None:
    """Older prompt returned bare {tracker_id: n}. The parser should
    still accept that for forward-compatibility with model drift."""
    classify = {"intent": "log_workout"}
    # No 'deltas' wrapper, just the raw tracker mapping
    log_response = {"sessions_today": 2}
    llm = _llm_returning(classify, log_response)
    spec = {
        "trackers": [
            {"id": "sessions_today", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
    }
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "g", "tracker_spec": spec},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("did 2 sets", member=MEMBER)
    assert r.handled is True
    assert goals.record_log_event.await_args.kwargs["deltas"] == {
        "sessions_today": 2.0
    }


# ── _parse_ts_hint guardrails ───────────────────────────────────


def test_parse_ts_hint_accepts_valid_recent_iso() -> None:
    from orchestrator.goals_chat import _parse_ts_hint
    ts_str = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    out = _parse_ts_hint(ts_str)
    assert out is not None
    assert out.tzinfo is not None


def test_parse_ts_hint_rejects_future_dates() -> None:
    """The LLM occasionally hallucinates future timestamps. Drop them
    rather than store nonsense."""
    from orchestrator.goals_chat import _parse_ts_hint
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    assert _parse_ts_hint(future) is None


def test_parse_ts_hint_rejects_ancient_dates() -> None:
    from orchestrator.goals_chat import _parse_ts_hint
    ancient = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    assert _parse_ts_hint(ancient) is None


def test_parse_ts_hint_handles_naive_datetime() -> None:
    """If the LLM forgets the tz offset, assume Dubai-local."""
    from orchestrator.goals_chat import _parse_ts_hint
    from zoneinfo import ZoneInfo
    naive = (datetime.now(ZoneInfo("Asia/Dubai")) - timedelta(hours=1)
             ).replace(tzinfo=None).isoformat()
    out = _parse_ts_hint(naive)
    assert out is not None
    assert out.tzinfo is not None


def test_parse_ts_hint_safe_on_garbage() -> None:
    from orchestrator.goals_chat import _parse_ts_hint
    assert _parse_ts_hint(None) is None
    assert _parse_ts_hint("") is None
    assert _parse_ts_hint("not a date") is None
    assert _parse_ts_hint(42) is None


# ── Natural-language mute / snooze (G) ──────────────────────────


@pytest.mark.asyncio
async def test_mute_goal_resolves_until_phrase_via_llm() -> None:
    """User says 'mute pushups until Monday'. LLM resolves to an ISO
    timestamp; store gets it as quiet_until."""
    classify = {"intent": "mute_goal", "phrase": "until Monday",
                "which": "pushups"}
    # 5 days from now is a Monday-ish target
    until_iso = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    resolver = {"until_iso": until_iso}
    llm = _llm_returning(classify, resolver)
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Pushups"},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("mute pushups until monday", member=MEMBER)
    assert r.handled is True
    goals.set_quiet_until.assert_awaited_once()
    call = goals.set_quiet_until.await_args
    assert call.args == (1,)
    # The resolved tz-aware datetime was passed through
    assert isinstance(call.kwargs["until"], datetime)
    assert call.kwargs["until"].tzinfo is not None
    # Reply mentions a future date
    assert "Muted" in r.text
    assert "Pushups" in r.text


@pytest.mark.asyncio
async def test_mute_goal_falls_back_to_24h_when_resolver_returns_null() -> None:
    """If the LLM can't resolve the phrase ('until I get back from travel'),
    fall back to a safe 24-hour mute."""
    classify = {"intent": "mute_goal",
                "phrase": "until I get back from travel"}
    resolver = {"until_iso": None}
    llm = _llm_returning(classify, resolver)
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Pushups"},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("mute until I'm back", member=MEMBER)
    assert r.handled is True
    goals.set_quiet_until.assert_awaited_once()
    until = goals.set_quiet_until.await_args.kwargs["until"]
    # Default fallback is ~24h from now
    delta = (until - datetime.now(UTC)).total_seconds() / 3600
    assert 23 < delta < 25


@pytest.mark.asyncio
async def test_mute_goal_backward_compat_with_duration_hours() -> None:
    """Older classifier responses returned duration_hours instead of
    phrase. Path still works so a stale model deployment doesn't break."""
    classify = {"intent": "mute_goal", "duration_hours": 48}
    llm = _llm_returning(classify)  # only one response — no resolver call
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "Pushups"},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("mute for 2 days", member=MEMBER)
    assert r.handled is True
    goals.set_quiet_until.assert_awaited_once()
    until = goals.set_quiet_until.await_args.kwargs["until"]
    delta = (until - datetime.now(UTC)).total_seconds() / 3600
    assert 47 < delta < 49


@pytest.mark.asyncio
async def test_set_nag_windows_resolves_phrase_via_llm() -> None:
    classify = {"intent": "set_nag_windows",
                "phrase": "don't nag me before 6pm on weekdays"}
    resolver = {"weekday_start_hour": 18}
    llm = _llm_returning(classify, resolver)
    nag = _fake_nag_store()
    h = _build(llm=llm, nag_store=nag)
    r = await h.try_handle(
        "don't nag me before 6pm on weekdays", member=MEMBER,
    )
    assert r.handled is True
    nag.set.assert_awaited_once()
    # The resolver's int landed in the store call
    assert nag.set.await_args.kwargs == {"weekday_start_hour": 18}


@pytest.mark.asyncio
async def test_set_nag_windows_falls_back_to_legacy_int_args() -> None:
    """If the classifier hands back the older explicit-int shape
    instead of a phrase, the handler still applies them."""
    classify = {"intent": "set_nag_windows",
                "weekday_start_hour": 18, "weekday_end_hour": 22}
    llm = _llm_returning(classify)  # only one response, no resolver call
    nag = _fake_nag_store()
    h = _build(llm=llm, nag_store=nag)
    r = await h.try_handle("change weekday window", member=MEMBER)
    assert r.handled is True
    nag.set.assert_awaited_once()
    assert nag.set.await_args.kwargs == {
        "weekday_start_hour": 18, "weekday_end_hour": 22,
    }


@pytest.mark.asyncio
async def test_set_nag_windows_apologizes_when_unresolvable() -> None:
    classify = {"intent": "set_nag_windows", "phrase": "be smarter"}
    resolver = {}  # LLM returned no usable hour fields
    llm = _llm_returning(classify, resolver)
    nag = _fake_nag_store()
    h = _build(llm=llm, nag_store=nag)
    r = await h.try_handle("be smarter about it", member=MEMBER)
    assert r.handled is True
    assert "couldn't tell" in r.text
    nag.set.assert_not_called()


# ── _parse_until_iso guardrails ─────────────────────────────────


def test_parse_until_iso_accepts_near_future() -> None:
    from orchestrator.goals_chat import _parse_until_iso
    future = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    assert _parse_until_iso(future) is not None


def test_parse_until_iso_rejects_past() -> None:
    from orchestrator.goals_chat import _parse_until_iso
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert _parse_until_iso(past) is None


def test_parse_until_iso_clamps_far_future() -> None:
    from orchestrator.goals_chat import _parse_until_iso
    very_far = (datetime.now(UTC) + timedelta(days=400)).isoformat()
    assert _parse_until_iso(very_far) is None


def test_parse_until_iso_handles_garbage() -> None:
    from orchestrator.goals_chat import _parse_until_iso
    assert _parse_until_iso(None) is None
    assert _parse_until_iso("") is None
    assert _parse_until_iso("never") is None


# ── Multi-turn goal creation (C) ────────────────────────────────


@pytest.mark.asyncio
async def test_create_goal_asks_clarification_when_planner_says_not_ready() -> None:
    """User says 'I want to lose weight'. Planner returns ready=false
    + a question. Handler stashes a draft and asks the question
    instead of committing a half-baked goal."""
    classify = {"intent": "create_goal", "title": "Lose weight",
                "description": "I want to lose weight"}
    not_ready = {"ready": False,
                  "clarification_question": "How much, and over what timeframe?"}
    llm = _llm_returning(classify, not_ready)
    redis = _fake_redis_stub()
    goals = _fake_goals_store()
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    r = await h.try_handle("I want to lose weight", member=MEMBER)
    assert r.handled is True
    assert "How much, and over what timeframe?" in r.text
    # No goal was created
    goals.create.assert_not_called()
    # Draft was stashed
    assert "goals_chat:draft:2" in redis._store
    draft = json.loads(redis._store["goals_chat:draft:2"])
    assert draft["title"] == "Lose weight"
    assert draft["pending_question"] == "How much, and over what timeframe?"
    assert draft["answers"] == []


@pytest.mark.asyncio
async def test_create_goal_draft_completes_after_user_answers() -> None:
    """Round 1: planner asks a question, draft stashed.
    Round 2: user replies, planner returns ready=true with a plan.
    The goal commits with the Q&A merged into description."""
    classify_1 = {"intent": "create_goal", "title": "Lose weight",
                  "description": "I want to lose weight"}
    not_ready = {"ready": False,
                  "clarification_question": "How much, and over what timeframe?"}
    ready_plan = {
        "ready": True,
        "plan_text": "10 kg over 16 weeks: ~0.6 kg/week.",
        "tracker_spec": {
            "trackers": [
                {"id": "weight_kg", "label": "Weight", "kind": "gauge",
                 "reset": "weekly", "target": 88, "unit": "kg",
                 "direction": "down"},
            ],
        },
        "milestones": [],
    }
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        {"message": {"content": json.dumps(classify_1)}},
        {"message": {"content": json.dumps(not_ready)}},
        {"message": {"content": json.dumps(ready_plan)}},
    ])
    redis = _fake_redis_stub()
    goals = _fake_goals_store()
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    # Round 1
    r1 = await h.try_handle("I want to lose weight", member=MEMBER)
    assert r1.handled is True
    assert "How much" in r1.text
    goals.create.assert_not_called()
    # Round 2 (user answers; classifier is NOT called because draft is open)
    r2 = await h.try_handle(
        "10 kg over the next 4 months", member=MEMBER,
    )
    assert r2.handled is True
    goals.create.assert_awaited_once()
    create_kwargs = goals.create.await_args.kwargs
    # Q&A folded into the stored description
    assert "Q: How much" in create_kwargs["description"]
    assert "10 kg over the next 4 months" in create_kwargs["description"]
    assert create_kwargs["tracker_spec"]["trackers"][0]["id"] == "weight_kg"
    # Draft cleared after commit
    assert "goals_chat:draft:2" not in redis._store


@pytest.mark.asyncio
async def test_create_goal_force_commits_after_two_rounds() -> None:
    """If the planner keeps asking questions, we commit after the 2nd
    round to avoid burning the user's patience."""
    classify_1 = {"intent": "create_goal", "title": "X", "description": "X"}
    # Planner returns ready=false twice in a row (with question), but
    # the handler should treat the third call as forced-commit.
    # In this test, the LLM keeps returning ready=false but with a
    # plan_text fallback — the engine forces ready=True after 2 rounds.
    persistent_no = {
        "ready": False,
        "clarification_question": "more info?",
        "plan_text": "Forced fallback plan.",
        "tracker_spec": {"trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 1, "direction": "up"},
        ]},
    }
    llm = MagicMock()
    # Need: classify + 3 planner calls (1 initial, 2 follow-ups)
    llm.chat = AsyncMock(side_effect=[
        {"message": {"content": json.dumps(classify_1)}},
        {"message": {"content": json.dumps(persistent_no)}},
        {"message": {"content": json.dumps(persistent_no)}},
        {"message": {"content": json.dumps(persistent_no)}},
    ])
    redis = _fake_redis_stub()
    goals = _fake_goals_store()
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    # Round 1: ask
    r1 = await h.try_handle("X", member=MEMBER)
    assert "more info" in r1.text
    goals.create.assert_not_called()
    # Round 2: still asking (1 prior answer)
    r2 = await h.try_handle("answer1", member=MEMBER)
    assert "more info" in r2.text
    goals.create.assert_not_called()
    # Round 3: hit the 2-round cap → forced commit even though LLM said no
    r3 = await h.try_handle("answer2", member=MEMBER)
    goals.create.assert_awaited_once()
    assert "goals_chat:draft:2" not in redis._store


@pytest.mark.asyncio
async def test_open_draft_intercepts_classification() -> None:
    """When a draft is open, the next message goes straight to the
    draft handler — the classifier is skipped entirely (the user's
    answer might look like 'check_progress' or anything else)."""
    classify_1 = {"intent": "create_goal", "title": "g", "description": "g"}
    not_ready = {"ready": False, "clarification_question": "huh?"}
    ready = {
        "ready": True, "plan_text": "ok",
        "tracker_spec": {"trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 1, "direction": "up"},
        ]},
    }
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        {"message": {"content": json.dumps(classify_1)}},
        {"message": {"content": json.dumps(not_ready)}},
        # The second user message ("how am I doing") would normally hit
        # the classifier first; with an open draft it must go to the
        # planner directly. So next call is the planner ready response.
        {"message": {"content": json.dumps(ready)}},
    ])
    redis = _fake_redis_stub()
    goals = _fake_goals_store()
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=redis,
    )
    r1 = await h.try_handle("g", member=MEMBER)
    assert "huh?" in r1.text
    # User's next message is a confounder ("how am I doing") — must NOT
    # route to check_progress.
    r2 = await h.try_handle("how am I doing", member=MEMBER)
    # Commits the goal with "how am I doing" as the answer
    goals.create.assert_awaited_once()
    assert "how am I doing" in goals.create.await_args.kwargs["description"]


@pytest.mark.asyncio
async def test_no_draft_no_redis_short_circuits_safely() -> None:
    """If redis is None (e.g. test env or transient outage), draft
    flow degrades to single-shot create. The planner is asked once
    and if it says not_ready we commit anyway (we can't store draft)."""
    classify = {"intent": "create_goal", "title": "X", "description": "X"}
    not_ready = {
        "ready": False,
        "clarification_question": "?",
    }
    llm = _llm_returning(classify, not_ready)
    # No redis
    goals = _fake_goals_store()
    h = GoalsChatHandler(
        llm=llm, goals_store=goals,
        chore_store=_fake_chore_store(),
        nag_store=_fake_nag_store(),
        redis=None,
    )
    r = await h.try_handle("X", member=MEMBER)
    # Without redis we can't persist the draft, so the question still
    # comes back to the user but no goal is created yet. The user
    # would need to amend their message to commit.
    assert r.handled is True
    assert "?" in r.text
    goals.create.assert_not_called()
