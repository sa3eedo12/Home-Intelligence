"""Auto-log from HealthKit metrics into active health goals.

Wakes up periodically, finds health_metrics rows inserted since the
last watermark, and for each row checks every active goal:

1. Cheap text-match: does the metric's name appear in any of the
   goal's tracker_spec.log_hints triggers? If no, skip.
2. LLM-classify: given the goal's tracker spec + a one-sentence
   description of the metric ("strength workout, 32 minutes,
   2026-05-31 13:15") return per-tracker deltas + an event timestamp.
3. If deltas are produced, insert a health_goal_log row with
   source=auto_healthkit and emit a brief Telegram notification
   ('Logged 32 min strength workout toward Pushups').

Watermark lives in Redis (`auto_log:last_metric_id`) so a restart
resumes where we left off. Cheap rate limit keeps notifications from
flooding: one per (goal, day) batch.

Pure generic plumbing — the LLM decides what counts. No hardcoded
mapping of metric name to tracker id.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from home_agents_sdk.health_goals_store import HealthGoalsStore
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis


logger = get_logger("orchestrator.goal_auto_logger")

WATERMARK_KEY = "auto_log:last_metric_id"
# Soft cap on how many health_metrics rows we process per poll, to
# stop us hammering the LLM if a backfill drops thousands of rows.
MAX_ROWS_PER_POLL = 50
# Don't auto-log if the user manually logged anything for this goal
# in the last ~10 minutes — assume they already captured it.
DEDUPE_MANUAL_WINDOW_MINUTES = 10
# Events whose timestamp is older than this don't get a Telegram ping
# at all — the data still lands in health_goal_log so the engine
# sees it, but the user isn't notified about ancient measurements.
NOTIFY_FRESHNESS_HOURS = 4
# Hard ceiling on Telegram pings per (member, send-day), regardless of
# how many goals fired. Stops a single watch-sync from blasting the
# user even if they have 5 active goals all matching the same events.
MAX_AUTO_NOTIFY_PER_MEMBER_PER_DAY = 4


async def run_once(
    *,
    pool: asyncpg.Pool,
    redis: Redis,
    store: HealthGoalsStore,
    llm: Any | None = None,
    classifier_model: str = "qwen3-8b-8k",
    now: datetime | None = None,
) -> dict[str, Any]:
    """One poll cycle. Returns counters for observability.

    Notifications are batched per (goal, poll cycle): if N events
    land for a goal during one run, the user gets ONE message
    summarizing them, not N. Events older than NOTIFY_FRESHNESS_HOURS
    are silently logged without a ping (backfill catch-up)."""
    now = now or datetime.now(UTC)
    watermark = await _get_watermark(redis)
    new_rows = await _fetch_new_metrics(pool, since_id=watermark)
    if not new_rows:
        return {"ok": True, "considered": 0, "logged": 0, "watermark": watermark}
    active = await store.list_active()
    if not active:
        # No goals → just advance the watermark so we don't reprocess.
        await _set_watermark(redis, new_rows[-1]["id"])
        return {"ok": True, "considered": len(new_rows), "logged": 0,
                "skipped_no_goals": True}
    logged = 0
    # Per-goal batched notification accumulator. Each entry collects
    # all the (deltas, event_ts, row) tuples that landed on this goal
    # in this poll cycle so we can emit ONE summary message at the end.
    batches: dict[int, dict[str, Any]] = {}
    for row in new_rows:
        member_id = row.get("member_id")
        for goal in active:
            if member_id is not None and int(goal["member_id"]) != int(member_id):
                continue
            if not _metric_matches_goal_hints(goal, row):
                continue
            # Dedupe: if a manual log happened recently for this goal,
            # assume the user already captured this event.
            if await _has_recent_manual_log(
                store, int(goal["id"]), within_minutes=DEDUPE_MANUAL_WINDOW_MINUTES,
                now=now,
            ):
                continue
            deltas, event_ts = await _classify_metric(
                llm=llm, goal=goal, metric_row=row,
                classifier_model=classifier_model,
            )
            if not deltas:
                continue
            try:
                await store.record_log_event(
                    int(goal["id"]),
                    deltas=deltas,
                    raw_text=_describe_metric(row),
                    member_id=int(goal["member_id"]),
                    source="auto_healthkit",
                    ts=event_ts or row.get("started_at"),
                )
            except Exception as exc:
                logger.warning("auto_log_insert_failed",
                               goal_id=goal["id"], error=str(exc))
                continue
            logged += 1
            batch = batches.setdefault(int(goal["id"]), {
                "goal": goal, "events": [], "member_id": int(goal["member_id"]),
            })
            batch["events"].append({
                "deltas": deltas,
                "event_ts": event_ts or row.get("started_at"),
                "row": row,
            })
    await _set_watermark(redis, new_rows[-1]["id"])
    # Emit one batched notification per goal. Apply the per-member
    # cap last so a single watch-sync can't flood a member with 5
    # active goals.
    member_send_counts: dict[int, int] = {}
    notified = 0
    for goal_id, batch in batches.items():
        events = batch["events"]
        if not events:
            continue
        # Drop stale-only batches entirely: if EVERY event in the
        # batch is older than the freshness window, this is a
        # backfill — silent.
        fresh = [
            ev for ev in events
            if isinstance(ev.get("event_ts"), datetime)
            and (now - ev["event_ts"].astimezone(UTC))
            <= timedelta(hours=NOTIFY_FRESHNESS_HOURS)
        ]
        if not fresh:
            logger.info("auto_log_silent_backfill",
                        goal_id=goal_id, events=len(events))
            continue
        member_id = batch["member_id"]
        sent_today = member_send_counts.get(member_id, 0)
        if sent_today >= MAX_AUTO_NOTIFY_PER_MEMBER_PER_DAY:
            logger.info("auto_log_member_cap_hit",
                        member_id=member_id, goal_id=goal_id)
            continue
        await _notify_user_batch(
            pool=pool, redis=redis, goal=batch["goal"], events=fresh,
        )
        member_send_counts[member_id] = sent_today + 1
        notified += 1
    return {
        "ok": True, "considered": len(new_rows), "logged": logged,
        "notified": notified, "watermark": new_rows[-1]["id"],
    }


# ── Watermark ────────────────────────────────────────────────────


async def _get_watermark(redis: Redis) -> int:
    try:
        raw = await redis.get(WATERMARK_KEY)
    except Exception:
        return 0
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def _set_watermark(redis: Redis, value: int) -> None:
    try:
        await redis.set(WATERMARK_KEY, str(int(value)))
    except Exception as exc:
        logger.warning("auto_log_watermark_set_failed", error=str(exc))


# ── Metric fetching ──────────────────────────────────────────────


async def _fetch_new_metrics(
    pool: asyncpg.Pool, *, since_id: int,
) -> list[dict[str, Any]]:
    """Pull health_metrics rows inserted after `since_id`. We bound to
    auto-export-friendly metric kinds to keep the LLM bill small —
    sleep summaries, weights, workouts, and the like. The watermark
    advances past everything we read so we never re-process."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, metric, started_at, ended_at, value, unit,
                   source, member_id, metadata
            FROM health_metrics
            WHERE id > $1
            ORDER BY id ASC
            LIMIT $2
            """,
            int(since_id), int(MAX_ROWS_PER_POLL),
        )
    return [dict(r) for r in rows]


# ── Matching + classification ────────────────────────────────────


def _metric_matches_goal_hints(
    goal: dict[str, Any], metric_row: dict[str, Any],
) -> bool:
    """Cheap pre-filter: does any of the goal's log_hints triggers or
    tracker labels show up in the metric name or unit?  Stops us from
    calling the LLM on every random heart-rate sample for every goal."""
    spec = goal.get("tracker_spec") or {}
    if not isinstance(spec, dict):
        return False
    haystack = " ".join(filter(None, [
        str(metric_row.get("metric") or "").lower(),
        str(metric_row.get("unit") or "").lower(),
        str(metric_row.get("source") or "").lower(),
    ]))
    for hint in spec.get("log_hints") or []:
        if not isinstance(hint, dict):
            continue
        for trig in hint.get("if_mentions") or []:
            if isinstance(trig, str) and trig.lower() in haystack:
                return True
    # Fall back to tracker id / label / unit substrings — covers
    # specs the LLM forgot to give log_hints for.
    for tracker in spec.get("trackers") or []:
        if not isinstance(tracker, dict):
            continue
        for field in ("id", "label", "unit"):
            value = str(tracker.get(field) or "").lower()
            if value and value in haystack:
                return True
    return False


def _describe_metric(row: dict[str, Any]) -> str:
    """Render a one-line natural description of a health_metrics row
    for the LLM prompt + the raw_text on the log entry."""
    metric = str(row.get("metric") or "metric")
    value = row.get("value")
    unit = str(row.get("unit") or "").strip()
    started = row.get("started_at")
    when = ""
    if isinstance(started, datetime):
        when = f" at {started.strftime('%Y-%m-%d %H:%M')}"
    parts = [metric.replace("_", " ")]
    if value is not None:
        v = f"{value:.1f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value)}"
        parts.append(f"{v} {unit}".strip())
    return " — ".join(parts) + when


async def _classify_metric(
    *,
    llm: Any | None,
    goal: dict[str, Any],
    metric_row: dict[str, Any],
    classifier_model: str,
) -> tuple[dict[str, float], datetime | None]:
    """Ask the LLM whether this metric satisfies any tracker on the
    goal. Returns (deltas, event_ts). Same shape as the user-text
    classifier so downstream code is unchanged."""
    spec = goal.get("tracker_spec") or {}
    trackers = spec.get("trackers") or []
    if not trackers:
        return {}, None
    if llm is None:
        # No LLM — try a deterministic single-tracker fallback when
        # there's only one tracker and the unit roughly matches.
        return _fallback_classify(spec, metric_row)
    trackers_brief = "; ".join(
        f"{t.get('id')} (kind={t.get('kind')}, label={t.get('label')}, "
        f"unit={t.get('unit')})"
        for t in trackers if isinstance(t, dict)
    )
    by_id = {t.get("id"): t for t in trackers if isinstance(t, dict)}
    description = _describe_metric(metric_row)
    started = metric_row.get("started_at")
    started_iso = (
        started.astimezone(UTC).isoformat()
        if isinstance(started, datetime) else None
    )
    system = (
        "You decide whether a health-metric event (from Apple Watch / "
        "HealthKit) satisfies any of a goal's trackers, and if so what "
        "value to record per tracker. Be conservative: if the metric "
        "doesn't clearly map to a tracker, return empty deltas.\n\n"
        "Two kinds of trackers exist:\n"
        "- COUNTER: the value you return is ADDED to today's running "
        "total. Use positive numbers only (e.g. minutes of activity, "
        "number of sessions). Never negative, never an absolute total "
        "— just the increment this one event represents.\n"
        "- GAUGE: the value you return REPLACES the latest reading. "
        "Use the ABSOLUTE measurement from the event (e.g. weight "
        "92.8, body_fat_pct 24.3). Never a delta or change-since-"
        "previous. Never negative for measurements that can't be "
        "negative.\n"
        "If the metric value looks implausible for the tracker (e.g. "
        "a weight reading of 0.5 kg, body fat of 300%), skip rather "
        "than guess.\n\n"
        "Return ONLY a JSON object: "
        "{\"deltas\": {<tracker_id>: <number>, ...}, "
        "\"ts_iso\": ISO-8601 of when the event happened or null, "
        "\"reasoning_brief\": <1 short sentence>}.\n\n"
        f"Goal: {goal.get('title')}\n"
        f"Trackers available: {trackers_brief}\n"
        f"Event time (from the metric): {started_iso or 'unknown'}"
    )
    try:
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": description},
            ],
            model=classifier_model,
            temperature=0.0,
            response_format="json",
            think=False,
        )
    except Exception as exc:
        logger.warning("auto_log_llm_failed",
                       goal_id=goal.get("id"), error=str(exc))
        return _fallback_classify(spec, metric_row)
    msg = resp.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else ""
    if not isinstance(content, str):
        return {}, None
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, None
    if not isinstance(parsed, dict):
        return {}, None
    raw_deltas = parsed.get("deltas") or {}
    if not isinstance(raw_deltas, dict):
        return {}, None
    out: dict[str, float] = {}
    for k, v in raw_deltas.items():
        if k not in by_id:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        # Per-kind validation. Generic rules — same logic for any
        # tracker the LLM might invent.
        tracker = by_id[k] or {}
        kind = str(tracker.get("kind") or "counter").lower()
        if kind == "counter":
            # Counters only accumulate. Negative numbers are usually
            # the LLM trying to subtract — drop them.
            if fv < 0:
                logger.warning("auto_log_dropped_negative_counter",
                               tracker=k, value=fv)
                continue
        else:
            # Gauges should be absolute readings. Plausibility check:
            # the LLM's value should be in the same ballpark as the
            # event's raw value (allow ±15% drift OR within 10 units
            # for small-number gauges). Catches the "stored -0.2
            # instead of 92.8" bug deterministically.
            event_value = metric_row.get("value")
            if event_value is not None:
                try:
                    ev = float(event_value)
                except (TypeError, ValueError):
                    ev = None
                if ev is not None and ev != 0:
                    # Reject if signs differ and the absolute value is
                    # within 10 (looks like a delta).
                    if (fv < 0 and ev > 0) or (fv > 0 and ev < 0):
                        logger.warning("auto_log_dropped_sign_mismatch",
                                       tracker=k, fv=fv, ev=ev)
                        continue
                    drift = abs(fv - ev) / abs(ev)
                    if drift > 0.15 and abs(fv - ev) > 10:
                        logger.warning("auto_log_dropped_implausible_gauge",
                                       tracker=k, fv=fv, ev=ev, drift=drift)
                        continue
        out[k] = fv
    # Reuse the existing parser from goals_chat
    from .goals_chat import _parse_ts_hint
    ts = _parse_ts_hint(parsed.get("ts_iso"))
    if ts is None and isinstance(started, datetime):
        ts = started.astimezone(UTC)
    return out, ts


def _fallback_classify(
    spec: dict[str, Any], metric_row: dict[str, Any],
) -> tuple[dict[str, float], datetime | None]:
    """When the LLM is unavailable: if the goal has exactly one
    tracker AND the metric's value+unit roughly match the tracker's
    unit, apply the value directly. Otherwise do nothing."""
    trackers = [t for t in (spec.get("trackers") or []) if isinstance(t, dict)]
    if len(trackers) != 1:
        return {}, None
    tracker = trackers[0]
    value = metric_row.get("value")
    if value is None:
        return {}, None
    tracker_unit = str(tracker.get("unit") or "").lower()
    metric_unit = str(metric_row.get("unit") or "").lower()
    if tracker_unit and metric_unit and tracker_unit not in metric_unit \
            and metric_unit not in tracker_unit:
        return {}, None
    started = metric_row.get("started_at")
    ts = started.astimezone(UTC) if isinstance(started, datetime) else None
    return {str(tracker["id"]): float(value)}, ts


# ── Dedupe ────────────────────────────────────────────────────────


async def _has_recent_manual_log(
    store: HealthGoalsStore, goal_id: int, *,
    within_minutes: int, now: datetime,
) -> bool:
    """True iff a manually-sourced log entry landed on this goal in
    the last `within_minutes`. Prevents the watch sync from
    duplicating something the user already typed."""
    cutoff = now - timedelta(minutes=within_minutes)
    rows = await store.recent_log(goal_id, since=cutoff, limit=10)
    for r in rows:
        src = str(r.get("source") or "")
        if src.startswith("auto"):
            continue
        return True
    return False


# ── Notification ─────────────────────────────────────────────────


async def _notify_user_batch(
    *, pool: asyncpg.Pool, redis: Redis,
    goal: dict[str, Any], events: list[dict[str, Any]],
) -> None:
    """Send one consolidated Telegram message summarizing all
    auto-logs that landed on this goal in the current poll cycle.

    For gauges: show the latest reading + count of samples.
    For counters: show the total added across the batch.
    One bullet per tracker."""
    if not events:
        return
    chat_id = await _chat_id_for_member(pool, int(goal["member_id"]))
    if chat_id is None:
        return
    spec = goal.get("tracker_spec") or {}
    by_id = {t.get("id"): t for t in (spec.get("trackers") or [])
             if isinstance(t, dict)}
    # Group event values by tracker_id, preserving time order
    per_tracker: dict[str, list[tuple[datetime | None, float]]] = {}
    for ev in events:
        deltas = ev.get("deltas") or {}
        ts = ev.get("event_ts")
        for tid, value in deltas.items():
            try:
                fv = float(value)
            except (TypeError, ValueError):
                continue
            per_tracker.setdefault(tid, []).append((ts, fv))
    bits = []
    for tid, values in per_tracker.items():
        t = by_id.get(tid, {})
        label = str(t.get("label") or tid)
        unit = str(t.get("unit") or "")
        kind = str(t.get("kind") or "counter").lower()
        if kind == "gauge":
            # Latest value wins for a gauge
            with_ts = [(ts, v) for ts, v in values if isinstance(ts, datetime)]
            with_ts.sort(key=lambda p: p[0])
            latest = with_ts[-1][1] if with_ts else values[-1][1]
            v_str = _fmt_num(latest)
            if len(values) == 1:
                bits.append(
                    f"{label.lower()} is now {v_str} {unit}".strip().replace(
                        "  ", " "
                    )
                )
            else:
                bits.append(
                    f"{label.lower()}: {len(values)} readings, latest "
                    f"{v_str} {unit}".strip().replace("  ", " ")
                )
        else:
            total = sum(v for _, v in values)
            v_str = _fmt_num(total)
            if len(values) == 1:
                bits.append(
                    f"added {v_str} {unit} to {label.lower()}".strip().replace(
                        "  ", " "
                    )
                )
            else:
                bits.append(
                    f"added {v_str} {unit} to {label.lower()} "
                    f"({len(values)} events)".strip().replace("  ", " ")
                )
    text = (
        "Auto-logged from your watch: " + "; ".join(bits) +
        f" toward \"{goal.get('title')}\". "
        "Reply 'undo' if any of that's wrong."
    )
    payload = {
        "chat_id": chat_id, "text": text, "severity": "info",
        "topic": f"goal:{goal['id']}:auto",
        "agent": "goal_auto_logger",
        "capability": "metric_observed",
    }
    try:
        await redis.xadd("notify.outbound", {"payload": json.dumps(payload)})
    except Exception as exc:
        logger.warning("auto_log_notify_failed",
                       goal_id=goal["id"], error=str(exc))


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


async def _notify_user(
    *, pool: asyncpg.Pool, redis: Redis,
    goal: dict[str, Any], row: dict[str, Any],
    deltas: dict[str, float], event_ts: datetime | None,
) -> None:
    """Send a low-key Telegram confirmation. Same pattern as the
    workout-nag emitter — push to notify.outbound, let the
    notify consumer respect quiet hours / policy."""
    chat_id = await _chat_id_for_member(pool, int(goal["member_id"]))
    if chat_id is None:
        return
    spec = goal.get("tracker_spec") or {}
    by_id = {t.get("id"): t for t in (spec.get("trackers") or []) if isinstance(t, dict)}
    bits = []
    for tid, value in deltas.items():
        t = by_id.get(tid, {})
        label = str(t.get("label") or tid)
        unit = str(t.get("unit") or "")
        kind = str(t.get("kind") or "counter").lower()
        v_str = f"{value:.1f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value)}"
        if kind == "gauge":
            # Gauges are absolute readings — phrase as "<label> is now N unit"
            bits.append(
                f"{label.lower()} is now {v_str} {unit}".strip().replace(
                    "  ", " "
                )
            )
        else:
            # Counters accumulate — phrase as "added N unit to <label>"
            bits.append(
                f"added {v_str} {unit} to {label.lower()}".strip().replace(
                    "  ", " "
                )
            )
    text = (
        "Auto-logged from your watch: " + "; ".join(bits) +
        f" toward \"{goal.get('title')}\". "
        "Reply 'undo' if that's wrong."
    )
    payload = {
        "chat_id": chat_id,
        "text": text,
        "severity": "info",
        "topic": f"goal:{goal['id']}:auto",
        "agent": "goal_auto_logger",
        "capability": "metric_observed",
    }
    try:
        await redis.xadd("notify.outbound", {"payload": json.dumps(payload)})
    except Exception as exc:
        logger.warning("auto_log_notify_failed",
                       goal_id=goal["id"], error=str(exc))


async def _chat_id_for_member(pool: asyncpg.Pool, member_id: int) -> int | None:
    async with pool.acquire() as conn:
        chat_id = await conn.fetchval(
            "SELECT telegram_chat_id FROM household_members WHERE id = $1",
            member_id,
        )
    try:
        return int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        return None
