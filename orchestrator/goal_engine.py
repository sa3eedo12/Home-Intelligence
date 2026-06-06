"""Generic goal-tracker engine.

A goal carries a `tracker_spec` (jsonb) that the LLM writes at creation
time. This module is the runtime: it knows how to read the spec, walk
the goal's log entries, compute current state, and decide whether to
nudge — all without baking workout-specific assumptions into the code.

Spec shape (canonical):

    {
      "trackers": [
        {
          "id": "sessions_today",       # unique within this goal
          "label": "Pushup sets today", # human-readable for messages
          "kind": "counter",            # counter | gauge
          "reset": "daily",             # daily | weekly | monthly | never
          "target": 5,
          "unit": "session",            # free text — "set", "kg", "min"
          "direction": "up"             # up = more is better, down = less
        },
        ...
      ],
      "completion_rule": {
        "kind": "all_targets_met",      # currently the only kind
        "trackers": ["sessions_today"]  # which trackers count for done
      },
      "nudge_rule": {
        "kind": "behind_schedule",      # behind_schedule | overdue | none
        "tracker": "sessions_today",
        "after_local_hour": 14,         # don't nag before this
        "before_local_hour": 22
      },
      "log_hints": [                    # help the log classifier
        {"if_mentions": ["pushup", "set"],
         "increment": {"sessions_today": 1, "reps_today": "ask"}}
      ]
    }

The runtime treats unknown fields as no-ops and missing rules as "no
behavior". Goals can opt into as much or as little of the framework
as the LLM thinks makes sense.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any


VALID_RESETS = {"daily", "weekly", "monthly", "never"}
VALID_KINDS = {"counter", "gauge"}
VALID_DIRECTIONS = {"up", "down"}


@dataclass(slots=True)
class TrackerState:
    """Computed view of one tracker at a point in time."""
    id: str
    label: str
    kind: str
    target: float | None
    direction: str
    unit: str
    reset: str
    current_value: float
    pct_of_target: float | None  # 0..100, None if no target OR no reading
    period_start: datetime | None
    period_end: datetime | None
    # False for gauges with no log row in the window. Counter trackers
    # always have a reading (zero is a legitimate "you haven't done it
    # yet" value). For gauges, missing data must NOT default to 0 — that
    # would falsely satisfy a "body_fat ≤ 20%" target and award 100% pct
    # for a tracker that has literally no source data.
    has_reading: bool = True


@dataclass(slots=True)
class GoalEvalResult:
    """Computed view of a whole goal at a point in time."""
    goal_id: int
    trackers: list[TrackerState]
    overall_pct: float | None   # blended 0..100, None if no scoreable trackers
    today_complete: bool
    nudge_due: bool
    nudge_reason: str | None
    state_blob: dict[str, Any]  # serializable {tracker_id: current_value}


# ── Spec normalization ───────────────────────────────────────────


def normalize_spec(spec: Any) -> dict[str, Any]:
    """Defensively coerce a tracker_spec into a valid shape. Drops
    malformed entries silently rather than raising — we never want the
    runtime to die on bad LLM output."""
    if not isinstance(spec, dict):
        return {"trackers": [], "completion_rule": None, "nudge_rule": None}
    trackers_raw = spec.get("trackers") or []
    if not isinstance(trackers_raw, list):
        trackers_raw = []
    trackers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in trackers_raw:
        if not isinstance(raw, dict):
            continue
        tid = str(raw.get("id") or "").strip()
        if not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        kind = str(raw.get("kind") or "counter").lower()
        if kind not in VALID_KINDS:
            kind = "counter"
        reset = str(raw.get("reset") or "daily").lower()
        if reset not in VALID_RESETS:
            reset = "daily"
        direction = str(raw.get("direction") or "up").lower()
        if direction not in VALID_DIRECTIONS:
            direction = "up"
        target_raw = raw.get("target")
        try:
            target = float(target_raw) if target_raw is not None else None
        except (TypeError, ValueError):
            target = None
        trackers.append({
            "id": tid,
            "label": str(raw.get("label") or tid),
            "kind": kind,
            "reset": reset,
            "target": target,
            "unit": str(raw.get("unit") or "").strip(),
            "direction": direction,
        })
    completion = spec.get("completion_rule")
    if not isinstance(completion, dict):
        completion = None
    nudge = spec.get("nudge_rule")
    if not isinstance(nudge, dict):
        nudge = None
    log_hints = spec.get("log_hints")
    if not isinstance(log_hints, list):
        log_hints = []
    return {
        "trackers": trackers,
        "completion_rule": completion,
        "nudge_rule": nudge,
        "log_hints": log_hints,
    }


# ── Period windows ───────────────────────────────────────────────


def _period_window(
    reset: str, now: datetime, tz_offset_hours: int = 4,
) -> tuple[datetime | None, datetime | None]:
    """Return the [start, end) datetime window for a tracker's current
    period, in the user's local time (default Asia/Dubai = UTC+4).

    'never' returns (None, None) meaning 'sum across all history'."""
    if reset == "never":
        return None, None
    local = now + timedelta(hours=tz_offset_hours)
    if reset == "daily":
        local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
    elif reset == "weekly":
        # Week starts Monday
        days_since_monday = local.weekday()
        local_start = (local - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        local_end = local_start + timedelta(days=7)
    elif reset == "monthly":
        local_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Next month
        if local_start.month == 12:
            local_end = local_start.replace(year=local_start.year + 1, month=1)
        else:
            local_end = local_start.replace(month=local_start.month + 1)
    else:
        return None, None
    start = local_start - timedelta(hours=tz_offset_hours)
    end = local_end - timedelta(hours=tz_offset_hours)
    return start.replace(tzinfo=UTC), end.replace(tzinfo=UTC)


# ── Evaluation ───────────────────────────────────────────────────


def evaluate(
    *,
    goal: dict[str, Any],
    log_rows: list[dict[str, Any]],
    now: datetime | None = None,
    tz_offset_hours: int = 4,
) -> GoalEvalResult:
    """Walk the goal's log rows, apply the tracker spec, and produce
    a structured snapshot. Pure function — no IO, no LLM."""
    now = now or datetime.now(UTC)
    spec = normalize_spec(goal.get("tracker_spec"))
    trackers_spec = spec["trackers"]
    states: list[TrackerState] = []
    state_blob: dict[str, Any] = {}
    for tcfg in trackers_spec:
        start, end = _period_window(
            tcfg["reset"], now, tz_offset_hours=tz_offset_hours,
        )
        if tcfg["kind"] == "gauge":
            # Gauge = most recent value reported for this tracker
            value = 0.0
            latest_ts: datetime | None = None
            for row in log_rows:
                ts = row.get("ts")
                if not isinstance(ts, datetime):
                    continue
                if start is not None and ts < start:
                    continue
                if end is not None and ts >= end:
                    continue
                deltas = row.get("deltas") or {}
                if not isinstance(deltas, dict):
                    continue
                raw = deltas.get(tcfg["id"])
                if raw is None:
                    continue
                try:
                    raw_value = float(raw)
                except (TypeError, ValueError):
                    continue
                if latest_ts is None or ts > latest_ts:
                    value = raw_value
                    latest_ts = ts
            has_reading = latest_ts is not None
        else:
            # counter = sum of deltas in window
            value = 0.0
            for row in log_rows:
                ts = row.get("ts")
                if not isinstance(ts, datetime):
                    continue
                if start is not None and ts < start:
                    continue
                if end is not None and ts >= end:
                    continue
                deltas = row.get("deltas") or {}
                if not isinstance(deltas, dict):
                    continue
                raw = deltas.get(tcfg["id"])
                if raw is None:
                    continue
                try:
                    value += float(raw)
                except (TypeError, ValueError):
                    continue
            # Counters always have a meaningful current value (0 = "you
            # haven't done it yet today"). Only gauges need the
            # has_reading guard.
            has_reading = True
        target = tcfg["target"]
        pct: float | None = None
        if target is not None and target != 0 and has_reading:
            if tcfg["direction"] == "down":
                # Lower is better; treat target as a ceiling.
                # pct = 100 * max(0, (start_value - value) / (start_value - target))
                # But we don't track a start value, so fall back to
                # 'fraction of target you're under': if value <= target
                # → 100, else 0.
                pct = 100.0 if value <= target else 0.0
            else:
                pct = max(0.0, min(100.0, 100.0 * value / target))
        states.append(TrackerState(
            id=tcfg["id"], label=tcfg["label"], kind=tcfg["kind"],
            target=target, direction=tcfg["direction"], unit=tcfg["unit"],
            reset=tcfg["reset"],
            current_value=value, pct_of_target=pct,
            period_start=start, period_end=end,
            has_reading=has_reading,
        ))
        state_blob[tcfg["id"]] = value
    overall = _overall_pct(states, spec)
    today_complete = _evaluate_completion(states, spec)
    nudge_due, nudge_reason = _evaluate_nudge(
        states, spec, now=now, tz_offset_hours=tz_offset_hours,
    )
    return GoalEvalResult(
        goal_id=int(goal.get("id") or 0),
        trackers=states,
        overall_pct=overall,
        today_complete=today_complete,
        nudge_due=nudge_due,
        nudge_reason=nudge_reason,
        state_blob=state_blob,
    )


def _overall_pct(
    states: list[TrackerState], spec: dict[str, Any],
) -> float | None:
    """Blend per-tracker pct into one number. Trackers without targets
    don't contribute. If no tracker has a target, returns None."""
    scoreable = [s for s in states if s.pct_of_target is not None]
    if not scoreable:
        return None
    return round(sum(s.pct_of_target for s in scoreable) / len(scoreable), 1)


def _evaluate_completion(
    states: list[TrackerState], spec: dict[str, Any],
) -> bool:
    """Apply the goal's completion_rule to the current state."""
    rule = spec.get("completion_rule")
    if not isinstance(rule, dict):
        # Sensible default: 'today is done' when every counter with
        # reset=daily and direction=up is at or past target.
        daily_up = [
            s for s in states
            if s.reset == "daily" and s.direction == "up" and s.target is not None
        ]
        if not daily_up:
            return False
        return all(s.current_value >= (s.target or 0) for s in daily_up)
    kind = str(rule.get("kind") or "")
    if kind == "all_targets_met":
        names = rule.get("trackers") or []
        if not names:
            names = [s.id for s in states]
        for s in states:
            if s.id not in names:
                continue
            if s.target is None:
                continue
            # A gauge with no reading is N/A, not "passing." Treat the
            # whole goal as incomplete until the user supplies data.
            if not s.has_reading:
                return False
            if s.direction == "up" and s.current_value < s.target:
                return False
            if s.direction == "down" and s.current_value > s.target:
                return False
        return True
    return False


def _evaluate_nudge(
    states: list[TrackerState],
    spec: dict[str, Any],
    *,
    now: datetime,
    tz_offset_hours: int,
) -> tuple[bool, str | None]:
    """Should we nudge right now?

    Default rule: nudge if the goal isn't 'complete for today' AND
    we're inside the after-hours window declared on the goal (or
    after 14:00 local if none declared)."""
    rule = spec.get("nudge_rule")
    kind = str((rule or {}).get("kind") or "behind_schedule")
    if kind == "none":
        return False, "nudge_disabled"
    if not states:
        # No trackers means no signal to nag about.
        return False, "no_trackers"
    if _evaluate_completion(states, spec):
        return False, "already_complete"
    after_hour = int((rule or {}).get("after_local_hour") or 14)
    before_hour = int((rule or {}).get("before_local_hour") or 22)
    local_hour = (now + timedelta(hours=tz_offset_hours)).hour
    if local_hour < after_hour:
        return False, "before_nudge_window"
    if local_hour >= before_hour:
        return False, "after_nudge_window"
    if kind == "overdue":
        # Only fire if at least one tracker is at 0 progress
        zero = [s for s in states if s.current_value == 0]
        if not zero:
            return False, "some_progress_already"
    return True, None


# ── Friendly status line ─────────────────────────────────────────


def format_status_line(result: GoalEvalResult) -> str:
    """Render a one-paragraph human-readable status. Used by the
    'check progress' Telegram intent + the dashboard."""
    if not result.trackers:
        return (
            "No trackers configured for this goal yet. Tell me how "
            "you'd like me to measure it."
        )
    parts = []
    for s in result.trackers:
        if not s.has_reading:
            # Gauge with no source data — render as 'no reading' instead
            # of '0', which would mislead both the user and the LLM.
            if s.target is None:
                parts.append(f"{s.label}: no reading yet")
            elif s.direction == "down":
                target_str = _format_value(s.target)
                parts.append(
                    f"{s.label}: no reading yet (target ≤ {target_str} {s.unit})".rstrip()
                )
            else:
                target_str = _format_value(s.target)
                parts.append(
                    f"{s.label}: no reading yet (target {target_str} {s.unit})".rstrip()
                )
            continue
        cv = _format_value(s.current_value)
        if s.target is None:
            parts.append(f"{s.label}: {cv} {s.unit}".rstrip())
            continue
        target_str = _format_value(s.target)
        if s.direction == "down":
            parts.append(
                f"{s.label}: {cv} {s.unit} (target ≤ {target_str})".replace(
                    "  ", " "
                ).strip()
            )
        else:
            parts.append(
                f"{s.label}: {cv} of {target_str} {s.unit}".replace(
                    "  ", " "
                ).strip()
            )
    head = "Today: done." if result.today_complete else "Today: in progress."
    body = " · ".join(parts)
    if result.overall_pct is not None:
        body += f" Overall {round(result.overall_pct)}%."
    return f"{head} {body}".strip()


def _format_value(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.1f}"


# ── Convenience defaults for new goals without LLM ───────────────


def default_spec_for_workout_frequency(
    *, required_per_week: int, days_preferred: list[str] | None = None,
) -> dict[str, Any]:
    """A safe fallback spec used by the planner when the LLM call
    fails. Mirrors the old workout_budget behavior so legacy goals
    keep working."""
    return {
        "trackers": [
            {
                "id": "workouts_this_week",
                "label": "Workouts this week",
                "kind": "counter",
                "reset": "weekly",
                "target": int(required_per_week),
                "unit": "workout",
                "direction": "up",
            }
        ],
        "completion_rule": None,
        "nudge_rule": {
            "kind": "behind_schedule",
            "tracker": "workouts_this_week",
            "after_local_hour": 14,
            "before_local_hour": 22,
        },
        "log_hints": [
            {"if_mentions": ["workout", "lifted", "trained", "gym",
                              "ran", "session"],
             "increment": {"workouts_this_week": 1}}
        ],
    }
