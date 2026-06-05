"""Engagement observation + window suggestion.

Two pieces:

1. `EngagementStore.record_sent` / `record_inbound`: tiny CRUD for the
   notify_engagement table. record_sent inserts a row when we fire a
   nag; record_inbound stamps first_reply_at + reply_seconds on every
   pending row for that member when the user replies (whether to that
   specific nag or to anything).

2. `propose_window_change`: weekly cron. Pulls the trailing 21 days of
   engagement events for each member, asks the LLM to look at the
   (day-of-week, hour, replied_within_2h) pattern, and if it sees a
   clear low-engagement window proposes adjusting the member's nag
   window. Sends a Telegram message; the user can accept by replying
   with the change in plain English (which routes through the existing
   set_nag_windows handler).

The LLM does the pattern-finding — no hardcoded thresholds beyond a
floor on sample size (we won't propose a change with fewer than 10
events in the window).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from home_agents_sdk.telemetry import get_logger


logger = get_logger("orchestrator.engagement")

# Don't ask the LLM to opine on tiny samples.
MIN_EVENTS_FOR_ANALYSIS = 10
# How many days back to look when running the weekly analysis.
ANALYSIS_WINDOW_DAYS = 21


class EngagementStore:
    """Thin wrapper over notify_engagement. Safe-defaults to no-op
    when the pool is unavailable (so tests + offline modes work)."""

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    async def record_sent(
        self,
        *,
        member_id: int | None,
        topic: str | None = None,
        agent: str | None = None,
        capability: str | None = None,
        sent_at: datetime | None = None,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO notify_engagement(
                    member_id, sent_at, topic, agent, capability
                )
                VALUES ($1, COALESCE($2, now()), $3, $4, $5)
                RETURNING id
                """,
                member_id, sent_at, topic, agent, capability,
            )
        return int(row["id"]) if row else None

    async def record_inbound(
        self,
        *,
        member_id: int,
        when: datetime | None = None,
    ) -> int:
        """Stamp first_reply_at on every pending nag row for this
        member. Returns the count updated. Called by goals_chat on
        every inbound user message."""
        if not self._ready or self.pool is None:
            return 0
        now = when or datetime.now(UTC)
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE notify_engagement
                   SET first_reply_at = $2,
                       reply_seconds  = GREATEST(0, EXTRACT(
                           EPOCH FROM ($2 - sent_at)
                       )::int)
                 WHERE member_id = $1
                   AND first_reply_at IS NULL
                   AND sent_at >= $2 - INTERVAL '6 hours'
                """,
                int(member_id), now,
            )
        # asyncpg returns 'UPDATE N'
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def recent_events(
        self,
        member_id: int,
        *,
        days: int = ANALYSIS_WINDOW_DAYS,
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, sent_at, first_reply_at, reply_seconds,
                       topic, agent, capability
                FROM notify_engagement
                WHERE member_id = $1
                  AND sent_at >= now() - ($2::int * INTERVAL '1 day')
                ORDER BY sent_at DESC
                """,
                int(member_id), int(days),
            )
        return [dict(r) for r in rows]


# ── Weekly window analysis ──────────────────────────────────────


async def propose_window_change(
    *,
    pool: asyncpg.Pool,
    redis: Any,
    engagement_store: EngagementStore,
    nag_windows_store: Any,
    member_id: int,
    llm: Any | None = None,
    classifier_model: str = "qwen3:8b",
) -> dict[str, Any]:
    """Look at the member's recent engagement; if there's a clear
    low-engagement window the user might want quieted, send a
    proposal. The LLM is the pattern finder — we just hand it the
    bucket data + current windows."""
    events = await engagement_store.recent_events(member_id)
    if len(events) < MIN_EVENTS_FOR_ANALYSIS:
        return {"ok": True, "skipped": "too_few_events",
                "events": len(events)}
    buckets = _bucket_engagement(events)
    if llm is None:
        return {"ok": True, "skipped": "no_llm",
                "events": len(events)}
    current = await nag_windows_store.get(member_id)
    proposal = await _ask_llm_for_proposal(
        llm=llm, buckets=buckets, current_windows=current,
        sample_size=len(events), model=classifier_model,
    )
    if not proposal or not proposal.get("should_propose"):
        return {"ok": True, "skipped": "no_pattern",
                "events": len(events)}
    chat_id = await _chat_id_for_member(pool, member_id)
    if chat_id is None:
        return {"ok": False, "error": "no_chat_id"}
    text = (
        proposal.get("user_message") or
        "I noticed a pattern in when you engage with my nudges — want "
        "to adjust your quiet hours?"
    )
    payload = {
        "chat_id": chat_id, "text": text, "severity": "info",
        "topic": f"engagement_proposal:{member_id}",
        "agent": "engagement_observer",
        "capability": "window_proposal",
    }
    await redis.xadd("notify.outbound", {"payload": json.dumps(payload)})
    return {
        "ok": True, "proposed": True, "events": len(events),
        "user_message": text[:200],
    }


def _bucket_engagement(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Aggregate events by (day_of_week, hour_band) into
    sent/replied counts. Day_of_week is the standard name; hour_band
    is 4-hour windows so the LLM has digestible buckets."""
    bands = {
        "early_morning": range(0, 6),
        "morning": range(6, 12),
        "afternoon": range(12, 18),
        "evening": range(18, 24),
    }

    def find_band(hour: int) -> str:
        for name, r in bands.items():
            if hour in r:
                return name
        return "other"

    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    out: dict[str, dict[str, int]] = {}
    for ev in events:
        sent = ev.get("sent_at")
        if not isinstance(sent, datetime):
            continue
        # Convert to Dubai local for bucketing
        local = sent + timedelta(hours=4)
        key = f"{days[local.weekday()]}_{find_band(local.hour)}"
        bucket = out.setdefault(key, {"sent": 0, "replied": 0})
        bucket["sent"] += 1
        if ev.get("first_reply_at"):
            bucket["replied"] += 1
    return out


async def _ask_llm_for_proposal(
    *,
    llm: Any,
    buckets: dict[str, dict[str, int]],
    current_windows: dict[str, Any],
    sample_size: int,
    model: str,
) -> dict[str, Any]:
    """Hand the engagement buckets + current windows to the LLM and
    ask whether to propose a change. The LLM owns the pattern logic
    — we only require a JSON object back."""
    system = (
        "You analyze a user's recent notification engagement and decide "
        "whether their quiet hours need adjusting. The user receives "
        "automated health-goal nudges; we track which arrive in a "
        "window when they later reply (any reply within a few hours "
        "counts).\n\n"
        "Return ONLY a JSON object:\n"
        "{\"should_propose\": bool, "
        "\"user_message\": <one short conversational message proposing "
        "the change, OR empty if should_propose is false>, "
        "\"pattern_summary\": <one sentence on what you noticed>}\n\n"
        "Propose a change only when a pattern is genuinely clear "
        "(e.g. mornings on weekdays consistently 0% reply rate across "
        "many samples). Don't propose if the data is noisy or thin. "
        "Be conservative — quiet hours are already set; only suggest "
        "tightening them, not opening them up.\n\n"
        "User message style: plain conversational English. Mention "
        "the specific window. Phrase as a question so the user can "
        "say yes/no, e.g. 'Looks like you don't engage with messages "
        "between 09:00 and 13:00 on weekdays — want me to add that to "
        "your quiet hours?'."
    )
    user = (
        f"Current windows: weekdays "
        f"{current_windows.get('weekday_start_hour', 14):02d}:00-"
        f"{current_windows.get('weekday_end_hour', 21):02d}:00, weekends "
        f"{current_windows.get('weekend_start_hour', 10):02d}:00-"
        f"{current_windows.get('weekend_end_hour', 21):02d}:00.\n"
        f"Sample size: {sample_size} nudges over the last ~3 weeks.\n"
        f"Bucketed engagement (sent / replied by day_band):\n"
        f"{json.dumps(buckets, indent=2)}"
    )
    try:
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=model,
            temperature=0.0,
            response_format="json",
            think=False,
        )
    except Exception as exc:
        logger.warning("window_proposal_llm_failed", error=str(exc))
        return {}
    msg = resp.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else ""
    if not isinstance(content, str):
        return {}
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── Cross-goal insight (F) ──────────────────────────────────────


async def run_cross_goal_insight(
    *,
    pool: asyncpg.Pool,
    redis: Any,
    goals_store: Any,
    member_id: int,
    llm: Any | None = None,
    reasoner_model: str = "gemma4:12b",
) -> dict[str, Any]:
    """Once a week, if the member has 2+ active goals, ask the
    reasoner for ONE cross-cutting observation. Save it to
    cross_goal_insights + send via Telegram.

    Examples of useful insights:
    - 'Workouts are up but weight is flat — calories are probably the
      bottleneck.'
    - 'Sleep and pushup-frequency moved together this week — keep an
      eye on that link.'
    - 'You skipped 3 workout days this week, all of which were also
      late-bedtime days.'"""
    active = await goals_store.list_active(member_id=member_id)
    if len(active) < 2:
        return {"ok": True, "skipped": "too_few_goals",
                "count": len(active)}
    # Build a compact summary of each goal's last 7 days
    snapshot_lines = []
    for goal in active:
        try:
            prog = await goals_store.recent_progress(int(goal["id"]), days=7)
        except Exception:
            prog = []
        title = goal.get("title")
        plan = (goal.get("plan_text") or "")[:200]
        last_state = prog[-1] if prog else None
        score = last_state.get("on_track_score") if last_state else None
        label = last_state.get("on_track_label") if last_state else None
        snap = last_state.get("metric_snapshots") if last_state else None
        snapshot_lines.append(
            f"- {title}: score={score}, label={label}, "
            f"latest_state={json.dumps(snap or {})}, plan={plan[:120]!r}"
        )
    if llm is None:
        return {"ok": True, "skipped": "no_llm"}
    system = (
        "You are a calm, practical health coach reviewing one user's "
        "active goals together. Look for ONE genuine connection between "
        "the goals (a trade-off, a hidden bottleneck, a shared trend). "
        "Don't force an insight — if nothing actionable jumps out, "
        "return have_insight=false.\n\n"
        "Return ONLY a JSON object:\n"
        "{\"have_insight\": bool, "
        "\"insight_text\": <2-3 sentences in plain conversational "
        "English, OR empty if have_insight is false>, "
        "\"suggestion\": <optional structured tag like 'reduce target' "
        "on a specific goal, OR null>}\n\n"
        "Tone: warm but direct. No emoji-as-syntax, no clichés, no "
        "exclamation marks."
    )
    user = (
        "Active goals (last-7d snapshot):\n" + "\n".join(snapshot_lines)
    )
    try:
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=reasoner_model,
            temperature=0.4,
            response_format="json",
            think=False,
            keep_alive=60,
        )
    except Exception as exc:
        logger.warning("cross_goal_llm_failed", error=str(exc))
        return {"ok": False, "error": "llm_failed"}
    msg = resp.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else ""
    if not isinstance(content, str):
        return {"ok": False, "error": "invalid_response"}
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"ok": False, "error": "parse_failed"}
    if not isinstance(parsed, dict) or not parsed.get("have_insight"):
        return {"ok": True, "skipped": "no_insight"}
    insight_text = str(parsed.get("insight_text") or "").strip()
    if not insight_text:
        return {"ok": True, "skipped": "empty_insight"}
    suggestion = parsed.get("suggestion") if isinstance(
        parsed.get("suggestion"), dict
    ) else None
    goal_ids = [int(g["id"]) for g in active]
    # Persist
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO cross_goal_insights(
                member_id, goal_ids, insight_text, suggestion
            )
            VALUES ($1, $2::jsonb, $3, $4::jsonb)
            """,
            int(member_id), json.dumps(goal_ids),
            insight_text,
            json.dumps(suggestion) if suggestion else None,
        )
    # Send to Telegram
    chat_id = await _chat_id_for_member(pool, member_id)
    if chat_id is not None:
        payload = {
            "chat_id": chat_id,
            "text": "Cross-goal check-in: " + insight_text,
            "severity": "info",
            "topic": f"cross_goal:{member_id}",
            "agent": "cross_goal_insight",
            "capability": "weekly_insight",
        }
        await redis.xadd("notify.outbound", {"payload": json.dumps(payload)})
    return {"ok": True, "sent": True, "goal_ids": goal_ids}


# ── Helpers ─────────────────────────────────────────────────────


async def _chat_id_for_member(
    pool: asyncpg.Pool, member_id: int,
) -> int | None:
    async with pool.acquire() as conn:
        chat_id = await conn.fetchval(
            "SELECT telegram_chat_id FROM household_members WHERE id = $1",
            member_id,
        )
    try:
        return int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        return None
