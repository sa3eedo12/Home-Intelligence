from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import _union_recent_sleep_minutes, router


def test_health_dashboard_renders_with_synthetic_data() -> None:
    app = FastAPI()
    app.include_router(router)

    async def aggregate(metric: str, days: int = 30) -> list[dict]:
        if metric == "steps":
            return [{"day": "2026-05-13", "value": 8450, "unit": "steps"}]
        if metric == "sleep_asleep":
            return [{"day": "2026-05-13", "value": 430, "unit": "min"}]
        if metric == "active_energy":
            return [{"day": "2026-05-13", "value": 560, "unit": "kcal"}]
        if metric == "weight":
            return [{"day": "2026-05-13", "value": 82.4, "unit": "kg"}]
        if metric == "resting_heart_rate":
            return [{"day": "2026-05-13", "value": 57, "unit": "bpm"}]
        return []

    async def latest(metric: str) -> dict | None:
        if metric == "weight":
            return {"metric": "weight", "value": 82.4, "unit": "kg"}
        if metric == "workout":
            return {
                "metric": "workout",
                "value": 45,
                "unit": "min",
                "metadata": {"workout_type": "running"},
            }
        if metric == "resting_heart_rate":
            return {"metric": "resting_heart_rate", "value": 57, "unit": "bpm"}
        return None

    app.state.health_store = SimpleNamespace(
        summary=AsyncMock(
            return_value={
                "total_metrics": 12453,
                "last_received_at": "2026-05-13T08:00:00+00:00",
            }
        ),
        list_recent=AsyncMock(
            return_value=[
                {"metric": "sleep_deep", "value": 90},
                {"metric": "sleep_rem", "value": 80},
                {"metric": "sleep_core", "value": 260},
                {"metric": "sleep_awake", "value": 15},
            ]
        ),
        aggregate_daily=AsyncMock(side_effect=aggregate),
        latest=AsyncMock(side_effect=latest),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard/health")

    assert resp.status_code == 200
    assert "Apple Health" in resp.text
    assert "12,453 metrics in store" in resp.text
    assert "running" in resp.text
    assert "How to set up Health Auto Export" in resp.text
    assert "Recent raw metrics" in resp.text
    assert "7-day aggregates" in resp.text
    assert "healthkit-test-btn" in resp.text
    assert "No HealthKit data yet" not in resp.text  # rendered by JS when the API is empty
    assert "/static/_app.js" in resp.text
    assert "/static/health.css" in resp.text
    assert "/static/health.js" in resp.text


# ── Union-of-intervals sleep aggregation (closes the "34h 35min" bug) ────


def _sleep_row(metric: str, start: str, end: str, value: float) -> dict:
    return {
        "metric": metric,
        "started_at": datetime.fromisoformat(start),
        "ended_at": datetime.fromisoformat(end),
        "value": value,
    }


def test_union_drops_outer_envelope_longer_than_14h() -> None:
    """HAE periodically emits an outer-envelope row covering 22-24h
    that bundles unrelated sleep sessions. The dashboard must drop
    these the same way sleep_inference does — otherwise a 7h night
    gets reported as ~30h."""
    rows = [
        # 23.5h envelope from Saeed's actual May 18 data
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T01:16:15+00:00",
            "2026-05-19T00:46:56+00:00",
            1410.683,
        ),
        # Real sleep segment
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T21:28:29+00:00",
            "2026-05-19T00:46:56+00:00",
            198.45,
        ),
    ]
    # 21:28:29 → 00:46:56 = 11907s = 198.45 min, banker-rounded to 198.4
    assert _union_recent_sleep_minutes(rows, "sleep_asleep") == 198.4


def test_union_dedupes_resync_snapshots_of_same_session() -> None:
    """Saeed's exact failure mode: HAE re-syncs partway through the
    night and emits a second sleep_asleep row with the SAME started_at
    but a larger ended_at as the user keeps sleeping. Naive sum would
    double-count these — must collapse to the latest snapshot."""
    rows = [
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T21:28:29+00:00",
            "2026-05-19T00:46:56+00:00",
            198.45,
        ),
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T21:28:29+00:00",
            "2026-05-19T05:15:13+00:00",  # extended after wake
            466.733,
        ),
    ]
    # Should pick the longer one (466.7 minutes), not sum to 665.
    assert _union_recent_sleep_minutes(rows, "sleep_asleep") == 466.7


def test_union_handles_real_saeed_data_correctly() -> None:
    """End-to-end: the exact 3-row dataset that produced '34h 35min'.
    Should yield ~466 min (the latest-snapshot of the single real
    sleep session) instead of 1410 + 198 + 466 = 2075 min."""
    rows = [
        # Envelope row (drop)
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T01:16:15+00:00",
            "2026-05-19T00:46:56+00:00",
            1410.683,
        ),
        # Early snapshot of real session
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T21:28:29+00:00",
            "2026-05-19T00:46:56+00:00",
            198.45,
        ),
        # Late snapshot of SAME session (supersedes)
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T21:28:29+00:00",
            "2026-05-19T05:15:13+00:00",
            466.733,
        ),
    ]
    minutes = _union_recent_sleep_minutes(rows, "sleep_asleep")
    assert minutes == 466.7  # ~7h 46m, not 34h 35m


def test_union_drops_awake_envelope_yielding_zero() -> None:
    """The 23h sleep_awake envelope (the '23h 30m awake' bug) had no
    other corroborating rows. After filtering, the awake total is 0."""
    rows = [
        _sleep_row(
            "sleep_awake",
            "2026-05-18T01:16:15+00:00",
            "2026-05-19T00:46:56+00:00",
            1410.683,
        ),
    ]
    assert _union_recent_sleep_minutes(rows, "sleep_awake") == 0.0


def test_union_returns_only_most_recent_sleep_session() -> None:
    """When the 36h query window contains TWO distinct sleep sessions
    (yesterday's night + today's night, or a nap + a night), the
    dashboard tile must show ONLY the most recent — the user reads it
    as 'last night's sleep,' not 'sum of every sleep event we have.'

    Production bug: two consecutive 7h nights summed to 14h 15min.
    Fix: 2h wake-tolerance clusters intervals into nights; return
    just the last night's duration."""
    rows = [
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T13:00:00+00:00",
            "2026-05-18T14:00:00+00:00",
            60,
        ),
        _sleep_row(
            "sleep_asleep",
            "2026-05-18T22:00:00+00:00",
            "2026-05-19T05:00:00+00:00",
            420,
        ),
    ]
    # Two sessions, separated by 8h. The night (420min) is the most
    # recent and is what gets reported. The 60min nap is dropped.
    assert _union_recent_sleep_minutes(rows, "sleep_asleep") == 420.0


def test_union_two_consecutive_nights_returns_only_last() -> None:
    """The exact production bug: 36h window contains last night
    (7h10m) AND this morning's night (7h05m). Previously summed to
    14h15m. Must now return only this morning's 425min."""
    rows = [
        _sleep_row(
            "sleep_asleep",
            "2026-06-04T21:22:00+00:00",
            "2026-06-05T04:33:00+00:00",  # 7h11m
            431,
        ),
        _sleep_row(
            "sleep_asleep",
            "2026-06-05T22:29:00+00:00",
            "2026-06-06T05:34:00+00:00",  # 7h05m
            425,
        ),
    ]
    minutes = _union_recent_sleep_minutes(rows, "sleep_asleep")
    # Only this morning's session — the 8h gap between yesterday's
    # wake-up and tonight's bedtime breaks them into distinct nights.
    assert 420 <= minutes <= 430


def test_union_brief_midnight_wakeup_stays_same_night() -> None:
    """A 30-min mid-night wake (kid woke up, bathroom, etc.) splits
    HealthKit's sleep_asleep into two intervals within the same night.
    The 2h wake-tolerance should keep them as ONE night, not drop the
    earlier half."""
    rows = [
        _sleep_row(
            "sleep_asleep",
            "2026-06-05T22:00:00+00:00",
            "2026-06-06T01:30:00+00:00",  # 3.5h
            210,
        ),
        _sleep_row(
            "sleep_asleep",
            "2026-06-06T02:00:00+00:00",  # 30min gap — within tolerance
            "2026-06-06T06:00:00+00:00",  # 4h
            240,
        ),
    ]
    minutes = _union_recent_sleep_minutes(rows, "sleep_asleep")
    # 22:00 → 06:00 = 8h. The 30-min wake is absorbed into the night.
    assert minutes == 480.0


def test_union_accepts_iso_string_datetimes() -> None:
    """list_recent returns started_at/ended_at as ISO strings (not
    datetime objects) because the store's _row_dict json-serializes
    them. The union helper must accept both shapes — without this,
    every dashboard render silently returned 0."""
    rows = [
        {
            "metric": "sleep_asleep",
            "started_at": "2026-05-18T21:28:29+00:00",
            "ended_at": "2026-05-19T05:15:13+00:00",
            "value": 466.733,
        }
    ]
    minutes = _union_recent_sleep_minutes(rows, "sleep_asleep")
    # 21:28:29 → 05:15:13 = 28004s ≈ 466.73 min, banker-rounded to 466.7
    assert minutes == 466.7
