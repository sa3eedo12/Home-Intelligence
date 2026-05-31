"""Daily compute + workout nag + weekly reflection for active health goals.

Three entrypoints:

- `compute_today(...)`: walks every active goal, pulls today's snapshot
  for each linked metric, writes one progress row per goal. Run nightly
  at 23:30 so it has the full day of data.

- `run_workout_nags(...)`: every 30 minutes during the day. For each
  goal whose workout_budget says today is a workout day and the user
  hasn't logged a workout (or excused it), emit a playful nag via the
  notify.outbound Redis stream — but only if the member's nag-window
  preference says we're inside an allowed window AND we haven't already
  nagged 3 times today.

- `run_weekly_reflection(...)`: Sundays at 21:00. For each active goal,
  pulls the last 7 days of progress and asks the reasoner (qwen3:14b)
  to write a one-paragraph reflection + (optionally) a refreshed plan.
  Sends the reflection to the user via notify.outbound.

The first two are pure orchestration over the HealthGoalsStore + the
existing health_metrics table; no LLM calls. The third spends one 14b
call per active goal per week — bounded and predictable.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
from home_agents_sdk.health_goals_store import (
    HealthGoalsStore,
    workout_required_today,
)
from home_agents_sdk.member_nag_windows_store import MemberNagWindowsStore
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis


logger = get_logger("orchestrator.health_goals")

# How many nags we send per goal per day. Three feels like the line
# between "playful prod" and "annoying app". After the third the
# scheduler stays quiet until the daily compute resets the counter.
MAX_NAGS_PER_DAY = 3

# Cap how close together two nags can land. 90 minutes feels human;
# without this, two ticks of the 30-min interval scheduler could each
# clear the window-just-opened gate and double-nag.
MIN_NAG_GAP_MINUTES = 90


_NAG_TEMPLATES_FIRST = [
    "Hey, {title} day. Want to knock out the workout before it gets late?",
    "Just a heads up — your plan has a workout penciled in for today. Up for it?",
    "Today's a workout day for {title}. No pressure, but the earlier you start the easier it lands.",
    "Friendly nudge: {title} is on the schedule today. Want to get moving?",
]
_NAG_TEMPLATES_SECOND = [
    "Still hoping to fit in that workout today? Even 20 minutes counts.",
    "Round two of the nag: workout for {title} is still open.",
    "I haven't seen a workout from you today. Want to do something short?",
    "Quick check — workout for {title}? You've got time.",
]
_NAG_TEMPLATES_THIRD = [
    "Last call from me today on the workout. Whatever you decide, I'll log it tomorrow.",
    "Final nudge: workout day for {title}. After this I'll back off until morning.",
    "If today's a no, just tell me and I'll mark it skipped — no shame.",
]


def _pick_nag_text(goal_title: str, nags_today: int) -> str:
    bucket = (
        _NAG_TEMPLATES_FIRST if nags_today == 0
        else _NAG_TEMPLATES_SECOND if nags_today == 1
        else _NAG_TEMPLATES_THIRD
    )
    return random.choice(bucket).format(title=goal_title)


# ── Daily compute ────────────────────────────────────────────────


async def compute_today(
    *,
    pool: asyncpg.Pool,
    store: HealthGoalsStore,
    today: date | None = None,
) -> dict[str, Any]:
    """Walk every active goal, ask the engine to evaluate it against
    its log entries, and snapshot the result into health_goal_progress.

    This is just a read-side cache so the dashboard / weekly reflection
    don't have to re-walk the log every render. The engine itself runs
    against fresh logs whenever a user asks 'how am I doing'."""
    from . import goal_engine

    today = today or date.today()
    active = await store.list_active()
    processed = 0
    for goal in active:
        try:
            log_rows = await store.recent_log(int(goal["id"]), limit=500)
            result = goal_engine.evaluate(goal=goal, log_rows=log_rows)
        except Exception as exc:
            logger.warning(
                "health_goal_compute_failed", goal_id=goal["id"], error=str(exc)
            )
            continue
        label = _label_from_pct(result.overall_pct)
        await store.upsert_progress(
            int(goal["id"]),
            day=today,
            metric_snapshots=result.state_blob,
            on_track_score=int(round(result.overall_pct)) if result.overall_pct is not None else None,
            on_track_label=label,
            workout_required=any(
                t.reset == "daily" for t in result.trackers
            ),
            workout_completed=result.today_complete,
            rest_day_excused=False,  # honored by excuse_today path
        )
        processed += 1
    return {
        "ok": True, "processed": processed, "day": today.isoformat(),
    }


def _label_from_pct(pct: float | None) -> str | None:
    if pct is None:
        return None
    if pct >= 80:
        return "on_track"
    if pct >= 50:
        return "slipping"
    return "regressing"


async def _snapshot_for_goal(
    *,
    pool: asyncpg.Pool,
    goal: dict[str, Any],
    today: date,
    week_start: date,
) -> tuple[dict[str, Any], int | None, str | None, bool]:
    """Legacy helper kept for tests that pin the older behavior. The
    new pipeline routes everything through goal_engine.evaluate() in
    compute_today(). Returns (snapshot, score, label, completed)."""
    from . import goal_engine

    log_rows: list[dict[str, Any]] = []
    # If the goal has a tracker_spec but no log rows yet, the engine
    # will produce zeros — perfectly correct behavior.
    result = goal_engine.evaluate(goal=goal, log_rows=log_rows)
    label = _label_from_pct(result.overall_pct)
    return result.state_blob, (
        int(round(result.overall_pct)) if result.overall_pct is not None else None
    ), label, result.today_complete


# ── Workout nags ─────────────────────────────────────────────────


async def run_workout_nags(
    *,
    pool: asyncpg.Pool,
    redis: Redis,
    store: HealthGoalsStore,
    nag_store: MemberNagWindowsStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generic nag scheduler. For each active goal, ask the engine
    whether a nudge is due based on the goal's spec + log state. If
    so, respect the member's quiet hours + per-day cap + min gap,
    then fire a playful message."""
    from . import goal_engine

    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    active = await store.list_active()
    emitted = 0
    considered = 0
    skipped: dict[str, int] = {
        "outside_window": 0, "muted": 0, "engine_says_no": 0,
        "cap": 0, "too_soon": 0, "no_chat": 0,
    }
    for goal in active:
        if _is_muted(goal, now):
            skipped["muted"] += 1
            continue
        try:
            log_rows = await store.recent_log(int(goal["id"]), limit=500)
            result = goal_engine.evaluate(
                goal=goal, log_rows=log_rows, now=now,
            )
        except Exception as exc:
            logger.warning("nag_eval_failed", goal_id=goal["id"],
                           error=str(exc))
            continue
        if not result.nudge_due:
            skipped["engine_says_no"] += 1
            continue
        considered += 1
        member_id = int(goal["member_id"])
        if not await nag_store.is_nag_allowed_now(member_id, now=now):
            skipped["outside_window"] += 1
            continue
        progress = await store.get_progress(int(goal["id"]), day=today) or {}
        if progress.get("rest_day_excused"):
            skipped["engine_says_no"] += 1  # respected as a 'no'
            continue
        nags_today = int(progress.get("nags_sent_today") or 0)
        if nags_today >= MAX_NAGS_PER_DAY:
            skipped["cap"] += 1
            continue
        last_nag_at = progress.get("last_nag_at")
        if isinstance(last_nag_at, datetime):
            gap = (now - last_nag_at.astimezone(UTC)).total_seconds() / 60
            if gap < MIN_NAG_GAP_MINUTES:
                skipped["too_soon"] += 1
                continue
        # Compose the message using the engine's tracker state so the
        # nag is actually meaningful ('2 of 5 sets today, three to go')
        # instead of a generic 'still pending'.
        progress_line = goal_engine.format_status_line(result)
        text = _pick_nag_text(goal["title"], nags_today) + "\n\n" + progress_line
        chat_id = await _chat_id_for_member(pool, member_id)
        if chat_id is None:
            skipped["no_chat"] += 1
            continue
        payload = {
            "chat_id": chat_id,
            "text": text,
            "severity": "info",
            "topic": f"goal:{goal['id']}",
            "agent": "health_goals",
            "capability": "workout_nag",
        }
        await redis.xadd("notify.outbound", {"payload": json.dumps(payload)})
        await store.record_nag(int(goal["id"]), day=today)
        emitted += 1
    return {
        "ok": True, "considered": considered, "emitted": emitted,
        "skipped": skipped,
    }


def _is_muted(goal: dict[str, Any], now: datetime) -> bool:
    quiet = goal.get("quiet_until")
    if quiet is None:
        return False
    if isinstance(quiet, datetime):
        return quiet > now.astimezone(quiet.tzinfo or UTC)
    return False


async def _chat_id_for_member(pool: asyncpg.Pool, member_id: int) -> int | None:
    async with pool.acquire() as conn:
        chat_id = await conn.fetchval(
            "SELECT telegram_chat_id FROM household_members WHERE id = $1",
            member_id,
        )
    if chat_id is None:
        return None
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


# ── Weekly reflection ────────────────────────────────────────────


async def run_weekly_reflection(
    *,
    pool: asyncpg.Pool,
    redis: Redis,
    store: HealthGoalsStore,
    llm: Any | None = None,
    reasoner_model: str = "qwen3:14b",
) -> dict[str, Any]:
    """Per active goal, fetch the last 7 days of progress and ask the
    reasoner for a one-paragraph reflection. Sends it to the user via
    notify.outbound. Updates plan_text if the reasoner returned a new
    one."""
    active = await store.list_active()
    reflected = 0
    skipped = 0
    for goal in active:
        member_id = int(goal["member_id"])
        progress_rows = await store.recent_progress(int(goal["id"]), days=7)
        stats = _summarize_week(progress_rows, goal)
        if llm is None:
            reflection_text = _fallback_reflection_text(goal, stats)
            new_plan = None
        else:
            try:
                out = await _llm_reflect(
                    llm=llm, goal=goal, stats=stats,
                    reasoner_model=reasoner_model,
                )
                reflection_text = out.get("reflection_text") or ""
                new_plan = out.get("new_plan_text")
            except Exception as exc:
                logger.warning("weekly_reflection_llm_failed",
                               goal_id=goal["id"], error=str(exc))
                reflection_text = _fallback_reflection_text(goal, stats)
                new_plan = None
        if not reflection_text.strip():
            skipped += 1
            continue
        chat_id = await _chat_id_for_member(pool, member_id)
        if chat_id is not None:
            payload = {
                "chat_id": chat_id,
                "text": _format_reflection_message(goal, stats, reflection_text),
                "severity": "info",
                "topic": f"goal:{goal['id']}:weekly",
                "agent": "health_goals",
                "capability": "weekly_reflection",
            }
            await redis.xadd("notify.outbound", {"payload": json.dumps(payload)})
        if new_plan and new_plan.strip():
            try:
                await store.update_plan(
                    int(goal["id"]), plan_text=new_plan,
                )
            except Exception as exc:
                logger.warning("weekly_reflection_plan_update_failed",
                               goal_id=goal["id"], error=str(exc))
        await store.log_event(
            int(goal["id"]), "weekly_review",
            member_id=member_id, note=reflection_text[:300],
        )
        reflected += 1
    return {"ok": True, "reflected": reflected, "skipped": skipped}


def _summarize_week(
    progress_rows: list[dict[str, Any]], goal: dict[str, Any],
) -> dict[str, Any]:
    """Roll the week's progress rows into the inputs the LLM (or the
    fallback writer) needs."""
    workouts_done = sum(1 for r in progress_rows if r.get("workout_completed"))
    excused = sum(1 for r in progress_rows if r.get("rest_day_excused"))
    nags = sum(int(r.get("nags_sent_today") or 0) for r in progress_rows)
    labels = [r.get("on_track_label") for r in progress_rows
              if r.get("on_track_label")]
    target = (goal.get("workout_budget") or {}).get("required_per_week")
    return {
        "workouts_done": workouts_done,
        "workouts_target": target,
        "excused_days": excused,
        "nags_sent": nags,
        "labels": labels,
        "days_with_data": len(progress_rows),
    }


def _fallback_reflection_text(
    goal: dict[str, Any], stats: dict[str, Any],
) -> str:
    """Used when the LLM is unavailable. Plain template, no shame."""
    target = stats["workouts_target"]
    done = stats["workouts_done"]
    if target and done >= int(target):
        return (
            f"Great week — you hit {done} of {target} workouts. "
            "Keep the same rhythm next week and the cadence will start "
            "feeling automatic."
        )
    if target:
        return (
            f"This week you logged {done} of {target} workouts. "
            "Not bad, but there's room. If a particular day keeps "
            "slipping, swap it for one that fits your schedule better."
        )
    return (
        f"This week you logged {done} workout" +
        ("" if done == 1 else "s") + ". I'll keep tracking and "
        "nudge you when a session is due."
    )


def _format_reflection_message(
    goal: dict[str, Any], stats: dict[str, Any], reflection: str,
) -> str:
    """Compose the user-facing weekly summary message."""
    title = goal["title"]
    target = stats["workouts_target"]
    done = stats["workouts_done"]
    header = f"Weekly check-in on \"{title}\""
    body = reflection.strip()
    facts = []
    if target:
        facts.append(f"Workouts: {done} of {target}")
    if stats["excused_days"]:
        facts.append(
            f"Rest days excused: {stats['excused_days']}"
        )
    return "\n\n".join([header, body] + ([" · ".join(facts)] if facts else []))


async def _llm_reflect(
    *,
    llm: Any,
    goal: dict[str, Any],
    stats: dict[str, Any],
    reasoner_model: str,
) -> dict[str, Any]:
    """One reasoner call per goal per week. Returns
    {reflection_text, new_plan_text?}."""
    system = (
        "You are a calm, practical health coach reviewing one user's "
        "week against one specific goal. Be warm, brief, and concrete. "
        "Avoid praise inflation. Avoid the word 'consistency'.\n\n"
        "Return ONLY a JSON object: "
        "{\"reflection_text\": str, \"new_plan_text\": str|null}.\n"
        "- reflection_text: 2 to 4 sentences about how the week went and "
        "one small adjustment to try next week.\n"
        "- new_plan_text: only set if the existing plan no longer fits "
        "(e.g. user is consistently missing the same day). Otherwise null."
    )
    user = (
        f"Goal title: {goal['title']}\n"
        f"Goal description: {goal['description']}\n"
        f"Current plan text: {goal.get('plan_text') or '(none)'}\n"
        f"This week's stats: {json.dumps(stats, default=str)}\n"
    )
    resp = await llm.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=reasoner_model,
        temperature=0.3,
        response_format="json",
        think=False,
        keep_alive=60,
    )
    msg = resp.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else ""
    if not isinstance(content, str):
        return {"reflection_text": ""}
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"reflection_text": content[:600], "new_plan_text": None}
    if not isinstance(parsed, dict):
        return {"reflection_text": str(content)[:600]}
    return {
        "reflection_text": str(parsed.get("reflection_text") or ""),
        "new_plan_text": parsed.get("new_plan_text") or None,
    }
