from __future__ import annotations

from datetime import UTC, datetime

from tools.core import _next_due


def test_weekly_recurrence_rollover() -> None:
    base = datetime(2026, 5, 12, 8, 0, tzinfo=UTC)  # Tuesday
    due = _next_due(base, "weekly:tue,fri", base)
    assert due.weekday() in {1, 4}
    assert due > base


def test_daily_recurrence_rollover() -> None:
    base = datetime(2026, 5, 12, 8, 0, tzinfo=UTC)
    due = _next_due(base, "daily", base)
    assert due == datetime(2026, 5, 13, 8, 0, tzinfo=UTC)


def test_invalid_recurrence_falls_back_to_next_day() -> None:
    base = datetime(2026, 5, 12, 8, 0, tzinfo=UTC)
    due = _next_due(None, "custom-invalid", base)
    assert due == datetime(2026, 5, 13, 8, 0, tzinfo=UTC)
