"""Tests for orchestrator.goals_chat — intent classification + handlers."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
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
        "metric_links": [{"metric": "workout", "target_per_week": 4}],
        "workout_budget": {"required_per_week": 4,
                            "flexible_rest_per_week": 2,
                            "days_preferred": ["sun", "tue", "thu", "sat"]},
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
    assert "4 workouts a week" in r.text
    goals.create.assert_awaited_once()
    create_kwargs = goals.create.await_args.kwargs
    assert create_kwargs["member_id"] == 2
    assert create_kwargs["title"] == "Work out 4x a week"
    assert create_kwargs["workout_budget"]["required_per_week"] == 4
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
    # Goal still created with the fallback plan
    goals.create.assert_awaited_once()
    args = goals.create.await_args.kwargs
    assert "I will check in" in (args.get("plan_text") or "")


# ── check_progress ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_progress_returns_humanized_line() -> None:
    llm = _llm_returning({"intent": "check_progress"})
    goals = _fake_goals_store(
        active=[{
            "id": 1, "member_id": 2, "title": "Strong",
            "workout_budget": {"required_per_week": 4},
        }],
        progress={
            "on_track_label": "on_track",
            "metric_snapshots": {"workouts_this_week": 3},
            "workout_required": True,
            "workout_completed": False,
            "rest_day_excused": False,
        },
    )
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("how am I doing", member=MEMBER)
    assert r.handled is True
    assert "Strong" in r.text
    assert "on track" in r.text
    assert "3 of 4" in r.text


@pytest.mark.asyncio
async def test_check_progress_no_active_goals() -> None:
    llm = _llm_returning({"intent": "check_progress"})
    goals = _fake_goals_store(active=[])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("am i on track", member=MEMBER)
    assert r.handled is True
    assert "don't have any active goals" in r.text


# ── log_workout ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_workout_marks_progress_completed_for_each_goal() -> None:
    llm = _llm_returning({"intent": "log_workout", "note": "leg day"})
    goals = _fake_goals_store(active=[
        {"id": 1, "member_id": 2, "title": "G1",
         "workout_budget": {"required_per_week": 4}},
        {"id": 2, "member_id": 2, "title": "G2",
         "workout_budget": {"required_per_week": 3}},
    ])
    h = _build(llm=llm, goals_store=goals)
    r = await h.try_handle("just did a workout", member=MEMBER)
    assert r.handled is True
    assert "2 goals" in r.text
    assert goals.upsert_progress.await_count == 2
    for call in goals.upsert_progress.await_args_list:
        assert call.kwargs["workout_completed"] is True


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
    """Tiny in-memory Redis stub — just enough for SET ex= and GET."""
    store: dict[str, str] = {}

    async def _get(key):
        return store.get(key)

    async def _set(key, value, ex=None):
        store[key] = value
        return True

    return SimpleNamespace(_store=store, get=AsyncMock(side_effect=_get),
                           set=AsyncMock(side_effect=_set))


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
        "workout_budget": {"required_per_week": 4,
                            "flexible_rest_per_week": 1,
                            "days_preferred": ["mon", "wed", "fri", "sun"]},
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
    assert "4 workouts per week" in r.text
    assert "Hit 1.5× starting max" in r.text
    assert "1.5× starting max" in r.text


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
