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
  pulls the last 7 days of progress and asks the reasoner (qwen36-moe-32k)
  to write a one-paragraph reflection + (optionally) a refreshed plan.
  Sends the reflection to the user via notify.outbound.

The first two are pure orchestration over the HealthGoalsStore + the
existing health_metrics table; no LLM calls. The third spends one 14b
call per active goal per week — bounded and predictable.
"""
from __future__ import annotations

import json
import re
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

# Defaults used when a goal's nudge_rule doesn't override them.
# Per-goal values take precedence; these only kick in when the planner
# didn't supply max_per_day / min_gap_minutes.
DEFAULT_MAX_NAGS_PER_DAY = 3
DEFAULT_MIN_NAG_GAP_MINUTES = 90

# One-line safe fallback when the LLM nag-writer is unreachable.
# Just enough to remind the user; the engine's status line carries
# the actual numbers.
_FALLBACK_NAG = (
    "Quick nudge — your {title} goal still has room today."
)


def _resolve_nag_policy(goal: dict[str, Any]) -> tuple[int, int]:
    """Read max_per_day + min_gap_minutes from the goal's nudge_rule,
    falling back to the system defaults."""
    rule = ((goal.get("tracker_spec") or {}).get("nudge_rule") or {})
    if not isinstance(rule, dict):
        rule = {}
    try:
        max_per_day = int(rule.get("max_per_day") or DEFAULT_MAX_NAGS_PER_DAY)
    except (TypeError, ValueError):
        max_per_day = DEFAULT_MAX_NAGS_PER_DAY
    try:
        min_gap = int(rule.get("min_gap_minutes") or DEFAULT_MIN_NAG_GAP_MINUTES)
    except (TypeError, ValueError):
        min_gap = DEFAULT_MIN_NAG_GAP_MINUTES
    return max(1, max_per_day), max(0, min_gap)


async def _compose_nag_text(
    *,
    llm: Any | None,
    goal: dict[str, Any],
    status_line: str,
    nags_today: int,
    recent_log: list[dict[str, Any]],
    model: str = "qwen3-8b-8k",
) -> str:
    """Generate one warm, brief nag line grounded in the goal's real
    current state. The LLM gets the goal title + plan + status line +
    a hint about how many times we've nudged today so it can escalate
    tone gracefully. Falls back to a single safe template on failure."""
    title = str(goal.get("title") or "your goal")
    if llm is None:
        return _FALLBACK_NAG.format(title=title)
    plan = str(goal.get("plan_text") or "")[:400]
    recent_lines = []
    for row in recent_log[:5]:
        ts = row.get("ts")
        text = row.get("raw_text") or ""
        if isinstance(ts, datetime) and text:
            recent_lines.append(
                f"- {ts.strftime('%a %H:%M')}: {text[:120]}"
            )
    recent_block = "\n".join(recent_lines) or "(no logs yet today)"
    tone_hint = (
        "first nudge of the day — be warm and brief" if nags_today == 0
        else "second nudge — keep it light, no guilt"
        if nags_today == 1
        else "later in the day — softer, mention this is the last one"
    )
    system = (
        "You are a calm, brief health coach checking in with one user. "
        "Write exactly ONE sentence (max ~25 words). Warm, conversational, "
        "no emoji-as-syntax, no markdown, no exclamation marks, no clichés "
        "like 'crush it' or 'you got this'. "
        "GROUND your message in the status line provided. NEVER invent "
        "numbers (kilograms lost, percentages, streaks) that aren't shown "
        "there. If the status line shows 0 progress or no data, "
        "acknowledge that honestly rather than inventing improvement. "
        "If the status doesn't justify celebration, don't celebrate. "
        "REQUIRED: your sentence must reference at least one concrete "
        "detail from the current state or recent activity — a specific "
        "number, count, time-of-day, or named action. Generic encouragement "
        "without an anchor is a fail. "
        f"Tone: {tone_hint}.\n\n"
        "Return ONLY the sentence — no quotes, no preamble."
    )
    user = (
        f"Goal: {title}\n"
        f"Plan: {plan}\n"
        f"Current state: {status_line}\n"
        f"Recent activity:\n{recent_block}"
    )
    try:
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=model,
            temperature=0.7,  # a touch of variation so repeats don't feel canned
            think=False,
        )
    except Exception as exc:
        logger.warning("nag_text_llm_failed", goal_id=goal.get("id"), error=str(exc))
        return _FALLBACK_NAG.format(title=title)
    msg = resp.get("message") or {}
    text = (msg.get("content") if isinstance(msg, dict) else "") or ""
    text = text.strip().strip('"').strip()
    if not text:
        return _FALLBACK_NAG.format(title=title)
    # Trim runaway responses to two sentences max (LLM safety net).
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:2]).strip()


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
    llm: Any | None = None,
    nag_model: str = "qwen3-8b-8k",
    engagement_store: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generic nag scheduler. For each active goal, ask the engine
    whether a nudge is due based on the goal's spec + log state. If
    so, respect the member's quiet hours + the goal's own
    nudge_rule.max_per_day / min_gap_minutes (defaults if absent),
    then compose a warm one-line message via the LLM (with a safe
    template fallback). The status line goes underneath so the
    user can see actual numbers.

    When engagement_store is provided, every emitted nag is recorded
    so the weekly window-observation job can spot low-engagement
    periods."""
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
        # Per-goal nag policy beats the global defaults.
        max_per_day, min_gap = _resolve_nag_policy(goal)
        nags_today = int(progress.get("nags_sent_today") or 0)
        if nags_today >= max_per_day:
            skipped["cap"] += 1
            continue
        last_nag_at = progress.get("last_nag_at")
        if isinstance(last_nag_at, datetime):
            gap = (now - last_nag_at.astimezone(UTC)).total_seconds() / 60
            if gap < min_gap:
                skipped["too_soon"] += 1
                continue
        status_line = goal_engine.format_status_line(result)
        nag_line = await _compose_nag_text(
            llm=llm, goal=goal, status_line=status_line,
            nags_today=nags_today, recent_log=log_rows,
            model=nag_model,
        )
        text = f"{nag_line}\n\n{status_line}"
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
        if engagement_store is not None:
            try:
                await engagement_store.record_sent(
                    member_id=member_id,
                    topic=f"goal:{goal['id']}",
                    agent="health_goals",
                    capability="workout_nag",
                )
            except Exception as exc:
                logger.warning("engagement_record_failed",
                               goal_id=goal["id"], error=str(exc))
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
    reasoner_model: str = "qwen36-moe-32k",
) -> dict[str, Any]:
    """Per active goal, build a tracker-spec-driven summary of the
    last 7 days and ask the reasoner for a one-paragraph reflection.
    Sends it to the user via notify.outbound. Updates plan_text if
    the reasoner returned a new one."""
    from . import goal_engine

    active = await store.list_active()
    reflected = 0
    skipped = 0
    for goal in active:
        member_id = int(goal["member_id"])
        try:
            log_rows = await store.recent_log(int(goal["id"]), limit=500)
            eval_result = goal_engine.evaluate(goal=goal, log_rows=log_rows)
            progress_rows = await store.recent_progress(int(goal["id"]), days=7)
            stats = _summarize_week(
                goal=goal, log_rows=log_rows, progress_rows=progress_rows,
                eval_result=eval_result,
            )
        except Exception as exc:
            logger.warning("weekly_reflection_summarize_failed",
                           goal_id=goal["id"], error=str(exc))
            skipped += 1
            continue
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
    *, goal: dict[str, Any],
    log_rows: list[dict[str, Any]],
    progress_rows: list[dict[str, Any]],
    eval_result: Any,
) -> dict[str, Any]:
    """Build a tracker-spec-driven week summary. Generic across goal
    shapes — anything in the tracker_spec gets summarized.

    For each tracker:
    - kind=counter, reset=daily   → sum of logs over the last 7 days,
                                    plus per-day breakdown
    - kind=counter, reset=weekly  → current weekly total + target
    - kind=gauge                  → latest reading + 7-day-ago reading
                                    + change

    Plus general rollups: total log count, excused days, nags sent."""
    spec = goal.get("tracker_spec") or {}
    trackers = spec.get("trackers") or []
    by_id = {t.get("id"): t for t in trackers if isinstance(t, dict)}

    # Per-tracker summary
    trackers_summary: list[dict[str, Any]] = []
    now_local_day = datetime.now(UTC).date()
    seven_days_ago = now_local_day - timedelta(days=7)
    for tcfg in trackers:
        if not isinstance(tcfg, dict):
            continue
        tid = tcfg.get("id")
        kind = tcfg.get("kind") or "counter"
        target = tcfg.get("target")
        unit = tcfg.get("unit") or ""
        label = tcfg.get("label") or tid
        # Pull this tracker's values out of the week's logs
        values_with_ts: list[tuple[datetime, float]] = []
        for row in log_rows:
            ts = row.get("ts")
            if not isinstance(ts, datetime):
                continue
            if ts.date() < seven_days_ago:
                continue
            deltas = row.get("deltas") or {}
            if not isinstance(deltas, dict) or tid not in deltas:
                continue
            try:
                values_with_ts.append((ts, float(deltas[tid])))
            except (TypeError, ValueError):
                continue
        if kind == "gauge":
            latest_val = values_with_ts[-1][1] if values_with_ts else None
            oldest_val = values_with_ts[0][1] if values_with_ts else None
            change = (
                latest_val - oldest_val
                if (latest_val is not None and oldest_val is not None)
                else None
            )
            trackers_summary.append({
                "id": tid, "label": label, "kind": kind,
                "unit": unit, "target": target,
                "latest_value": latest_val,
                "change_over_week": change,
                "samples_this_week": len(values_with_ts),
            })
        else:
            total = sum(v for _, v in values_with_ts)
            trackers_summary.append({
                "id": tid, "label": label, "kind": kind,
                "unit": unit, "target": target,
                "week_total": total,
                "events_this_week": len(values_with_ts),
            })

    # General rollups
    excused = sum(1 for r in progress_rows if r.get("rest_day_excused"))
    nags = sum(int(r.get("nags_sent_today") or 0) for r in progress_rows)
    completed_days = sum(1 for r in progress_rows if r.get("workout_completed"))

    return {
        "trackers": trackers_summary,
        "completed_days": completed_days,
        "excused_days": excused,
        "nags_sent": nags,
        "days_with_data": len(progress_rows),
        "overall_pct_now": getattr(eval_result, "overall_pct", None),
        "today_complete": getattr(eval_result, "today_complete", False),
    }


def _fallback_reflection_text(
    goal: dict[str, Any], stats: dict[str, Any],
) -> str:
    """Used when the LLM is unavailable. Pure template — describes
    each tracker honestly without inventing progress."""
    title = goal.get("title") or "your goal"
    lines: list[str] = []
    for t in stats.get("trackers") or []:
        label = t.get("label") or t.get("id") or "tracker"
        unit = t.get("unit") or ""
        target = t.get("target")
        if t.get("kind") == "gauge":
            latest = t.get("latest_value")
            if latest is None:
                lines.append(f"- {label}: no readings this week.")
                continue
            line = f"- {label}: latest {latest} {unit}".strip()
            change = t.get("change_over_week")
            if change is not None and change != 0:
                arrow = "↑" if change > 0 else "↓"
                line += f" ({arrow}{abs(change):.1f} over the week)"
            if target is not None:
                line += f"; target {target} {unit}".strip()
            lines.append(line)
        else:
            total = t.get("week_total") or 0
            line = f"- {label}: {int(total) if total == int(total) else total} {unit}".strip()
            if target is not None:
                line += f" this week (target {target})"
            lines.append(line)
    if not lines:
        return (
            f"This week I didn't see any logged activity for \"{title}\". "
            "Tell me what you've done and I'll start tracking properly."
        )
    return (
        f"Quick check-in on \"{title}\":\n"
        + "\n".join(lines)
    )


def _format_reflection_message(
    goal: dict[str, Any], stats: dict[str, Any], reflection: str,
) -> str:
    """Compose the user-facing weekly summary message."""
    title = goal["title"]
    header = f"Weekly check-in on \"{title}\""
    body = reflection.strip()
    # Per-tracker fact strip (only when we have non-trivial data)
    facts = []
    for t in stats.get("trackers") or []:
        label = t.get("label") or t.get("id")
        unit = t.get("unit") or ""
        if t.get("kind") == "gauge":
            latest = t.get("latest_value")
            if latest is None:
                continue
            facts.append(f"{label}: {latest} {unit}".strip())
        else:
            total = t.get("week_total") or 0
            target = t.get("target")
            if target:
                facts.append(f"{label}: {int(total) if total == int(total) else total} of {target}")
            elif total:
                facts.append(f"{label}: {int(total) if total == int(total) else total}")
    if stats.get("excused_days"):
        facts.append(f"Rest days excused: {stats['excused_days']}")
    parts = [header, body]
    if facts:
        parts.append(" · ".join(facts))
    return "\n\n".join(parts)


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
        "Ground your reflection in the per-tracker stats provided. "
        "Never invent numbers (kg lost, percentages, streaks) that "
        "don't appear in the stats. If a tracker has no data this "
        "week, acknowledge that — don't pretend there was progress.\n\n"
        "Return ONLY a JSON object: "
        "{\"reflection_text\": str, \"new_plan_text\": str|null}.\n"
        "- reflection_text: 2 to 4 sentences about how the week went and "
        "ONE small adjustment to try next week. Reference at least one "
        "actual number from the stats.\n"
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
