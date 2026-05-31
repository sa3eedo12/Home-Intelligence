"""Tests for orchestrator.goal_engine — the generic tracker runtime."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator import goal_engine


# ── normalize_spec ──────────────────────────────────────────────


def test_normalize_spec_drops_malformed_trackers() -> None:
    spec = goal_engine.normalize_spec({
        "trackers": [
            {"id": "good", "kind": "counter", "reset": "daily",
             "target": 5, "direction": "up", "label": "Good", "unit": "x"},
            "not a dict",
            {"kind": "counter"},  # missing id
            {"id": "good"},  # duplicate id — must be dropped
            {"id": "weird", "kind": "weird_kind",
             "reset": "weird_reset", "direction": "sideways",
             "target": "not a number"},
        ],
    })
    assert [t["id"] for t in spec["trackers"]] == ["good", "weird"]
    # bad values fall back to safe defaults
    weird = spec["trackers"][1]
    assert weird["kind"] == "counter"
    assert weird["reset"] == "daily"
    assert weird["direction"] == "up"
    assert weird["target"] is None


def test_normalize_spec_handles_non_dict_input() -> None:
    out = goal_engine.normalize_spec(None)
    assert out["trackers"] == []
    assert out["completion_rule"] is None
    assert out["nudge_rule"] is None


# ── _period_window ──────────────────────────────────────────────


def test_period_window_daily_in_dubai() -> None:
    now = datetime(2026, 5, 31, 14, 0, tzinfo=UTC)  # 18:00 Dubai
    start, end = goal_engine._period_window("daily", now, tz_offset_hours=4)
    assert start == datetime(2026, 5, 30, 20, 0, tzinfo=UTC)  # midnight Dubai = 20:00 prev day UTC
    assert end == datetime(2026, 5, 31, 20, 0, tzinfo=UTC)


def test_period_window_weekly_starts_monday() -> None:
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)  # Sat
    start, end = goal_engine._period_window("weekly", now, tz_offset_hours=4)
    # Monday May 25 00:00 Dubai = May 24 20:00 UTC
    assert start == datetime(2026, 5, 24, 20, 0, tzinfo=UTC)
    assert (end - start).days == 7


def test_period_window_never_returns_none_none() -> None:
    assert goal_engine._period_window("never", datetime.now(UTC)) == (None, None)


# ── evaluate counter trackers ───────────────────────────────────


def test_evaluate_counter_sums_within_window() -> None:
    spec = {
        "trackers": [
            {"id": "sets", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)  # 18:00 Dubai
    today = datetime(2026, 5, 31, 11, tzinfo=UTC)  # 15:00 Dubai (in window)
    yesterday = datetime(2026, 5, 30, 11, tzinfo=UTC)  # out of window
    log_rows = [
        {"ts": today, "deltas": {"sets": 2}},
        {"ts": today, "deltas": {"sets": 1}},
        {"ts": yesterday, "deltas": {"sets": 10}},  # should NOT count
    ]
    result = goal_engine.evaluate(goal=goal, log_rows=log_rows, now=now)
    assert result.trackers[0].current_value == 3.0
    assert result.trackers[0].pct_of_target == 60.0
    assert result.overall_pct == 60.0
    assert result.today_complete is False


def test_evaluate_marks_complete_when_target_hit() -> None:
    spec = {
        "trackers": [
            {"id": "sets", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
        ],
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    log_rows = [
        {"ts": now, "deltas": {"sets": 5}},
    ]
    result = goal_engine.evaluate(goal=goal, log_rows=log_rows, now=now)
    assert result.today_complete is True
    assert result.overall_pct == 100.0


def test_evaluate_gauge_takes_most_recent_value() -> None:
    spec = {
        "trackers": [
            {"id": "weight_kg", "label": "Weight", "kind": "gauge",
             "reset": "daily", "target": 80, "unit": "kg", "direction": "down"},
        ],
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    log_rows = [
        {"ts": now - timedelta(hours=2), "deltas": {"weight_kg": 88}},
        {"ts": now - timedelta(hours=1), "deltas": {"weight_kg": 85}},  # newest in window
    ]
    result = goal_engine.evaluate(goal=goal, log_rows=log_rows, now=now)
    # most recent = 85
    assert result.trackers[0].current_value == 85.0
    # 85 > target 80 with direction down → not yet met, pct = 0
    assert result.trackers[0].pct_of_target == 0.0
    assert result.today_complete is False


def test_evaluate_ignores_malformed_log_entries() -> None:
    spec = {
        "trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 5, "direction": "up", "unit": "x"},
        ],
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    log_rows = [
        {"ts": "not a datetime", "deltas": {"x": 100}},
        {"ts": now, "deltas": "not a dict"},
        {"ts": now, "deltas": {"x": "nope"}},  # bad value
        {"ts": now, "deltas": {"x": 3}},  # the only good one
    ]
    result = goal_engine.evaluate(goal=goal, log_rows=log_rows, now=now)
    assert result.trackers[0].current_value == 3.0


def test_evaluate_no_trackers_returns_safe_result() -> None:
    goal = {"id": 1, "tracker_spec": None}
    result = goal_engine.evaluate(goal=goal, log_rows=[],
                                   now=datetime.now(UTC))
    assert result.trackers == []
    assert result.overall_pct is None
    assert result.today_complete is False
    assert result.nudge_due is False


# ── nudge_rule ───────────────────────────────────────────────────


def test_nudge_due_when_behind_inside_window() -> None:
    spec = {
        "trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 5, "direction": "up", "unit": "x"},
        ],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 14,
                        "before_local_hour": 22},
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)  # 18:00 Dubai (inside)
    result = goal_engine.evaluate(goal=goal, log_rows=[], now=now)
    assert result.nudge_due is True


def test_nudge_skipped_before_window() -> None:
    spec = {
        "trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 5, "direction": "up", "unit": "x"},
        ],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 18,
                        "before_local_hour": 22},
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 9, tzinfo=UTC)  # 13:00 Dubai (before)
    result = goal_engine.evaluate(goal=goal, log_rows=[], now=now)
    assert result.nudge_due is False
    assert result.nudge_reason == "before_nudge_window"


def test_nudge_skipped_after_window() -> None:
    spec = {
        "trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 5, "direction": "up", "unit": "x"},
        ],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 14,
                        "before_local_hour": 20},
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 18, tzinfo=UTC)  # 22:00 Dubai (after)
    result = goal_engine.evaluate(goal=goal, log_rows=[], now=now)
    assert result.nudge_due is False
    assert result.nudge_reason == "after_nudge_window"


def test_nudge_skipped_when_already_complete() -> None:
    spec = {
        "trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 5, "direction": "up", "unit": "x"},
        ],
        "nudge_rule": {"kind": "behind_schedule",
                        "after_local_hour": 0, "before_local_hour": 24},
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    log_rows = [{"ts": now, "deltas": {"x": 5}}]
    result = goal_engine.evaluate(goal=goal, log_rows=log_rows, now=now)
    assert result.nudge_due is False
    assert result.nudge_reason == "already_complete"


def test_nudge_kind_none_opts_out() -> None:
    spec = {
        "trackers": [
            {"id": "x", "label": "X", "kind": "counter",
             "reset": "daily", "target": 5, "direction": "up", "unit": "x"},
        ],
        "nudge_rule": {"kind": "none"},
    }
    goal = {"id": 1, "tracker_spec": spec}
    result = goal_engine.evaluate(
        goal=goal, log_rows=[], now=datetime(2026, 5, 31, 14, tzinfo=UTC),
    )
    assert result.nudge_due is False
    assert result.nudge_reason == "nudge_disabled"


# ── format_status_line ──────────────────────────────────────────


def test_format_status_line_renders_per_tracker_progress() -> None:
    spec = {
        "trackers": [
            {"id": "sets", "label": "Sets", "kind": "counter",
             "reset": "daily", "target": 5, "unit": "set", "direction": "up"},
            {"id": "reps", "label": "Reps", "kind": "counter",
             "reset": "daily", "target": 50, "unit": "rep", "direction": "up"},
        ],
    }
    goal = {"id": 1, "tracker_spec": spec}
    now = datetime(2026, 5, 31, 14, tzinfo=UTC)
    log_rows = [
        {"ts": now, "deltas": {"sets": 2, "reps": 30}},
    ]
    result = goal_engine.evaluate(goal=goal, log_rows=log_rows, now=now)
    line = goal_engine.format_status_line(result)
    assert "Sets: 2 of 5" in line
    assert "Reps: 30 of 50" in line
    assert "in progress" in line


def test_format_status_line_empty_trackers() -> None:
    goal = {"id": 1, "tracker_spec": None}
    result = goal_engine.evaluate(
        goal=goal, log_rows=[], now=datetime.now(UTC),
    )
    line = goal_engine.format_status_line(result)
    assert "No trackers" in line


# ── default_spec_for_workout_frequency ──────────────────────────


def test_default_spec_for_workout_frequency() -> None:
    spec = goal_engine.default_spec_for_workout_frequency(
        required_per_week=4, days_preferred=["mon", "wed", "fri"],
    )
    assert spec["trackers"][0]["id"] == "workouts_this_week"
    assert spec["trackers"][0]["target"] == 4
    assert spec["nudge_rule"]["kind"] == "behind_schedule"
    # Round-trip through normalize to verify it's valid input for the engine
    normalized = goal_engine.normalize_spec(spec)
    assert len(normalized["trackers"]) == 1
