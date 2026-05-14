"""Tests for the macOS HealthKit Shortcuts forwarder.

These run against deploy/mac/healthkit-shortcuts/forwarder.py without the
LaunchAgent / Shortcuts CLI / TrueNAS — they validate that the snapshot
shape the Shortcut emits is correctly transformed into the nested
Health Auto Export envelope the orchestrator's normalizer accepts.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FORWARDER_PATH = REPO_ROOT / "deploy/mac/healthkit-shortcuts/forwarder.py"


@pytest.fixture(scope="module")
def forwarder():
    spec = importlib.util.spec_from_file_location("hk_shortcuts_forwarder", FORWARDER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_quantity_metrics_become_health_auto_export_shape(forwarder) -> None:
    snap = {
        "ts": "2026-05-14T08:00:00Z",
        "window_min": 60,
        "steps": "1234",          # Shortcuts often emits numbers as strings
        "active_energy": 87.5,
        "heart_rate": 72.4,
        "resting_heart_rate": 58,
        "weight": 82.3,
    }
    body = forwarder._build_metrics_payload(snap)
    metrics = body["data"]["metrics"]
    by_type = {m["type"]: m for m in metrics}

    assert "HKQuantityTypeIdentifierStepCount" in by_type
    assert by_type["HKQuantityTypeIdentifierStepCount"]["data"][0]["qty"] == 1234.0
    assert by_type["HKQuantityTypeIdentifierStepCount"]["units"] == "steps"

    assert "HKQuantityTypeIdentifierBodyMass" in by_type
    assert by_type["HKQuantityTypeIdentifierBodyMass"]["units"] == "kg"
    assert by_type["HKQuantityTypeIdentifierBodyMass"]["data"][0]["qty"] == 82.3

    # All quantity samples should carry the same capture timestamp.
    for metric in metrics:
        for sample in metric["data"]:
            assert sample["date"] == "2026-05-14T08:00:00Z"


def test_missing_and_null_values_are_skipped(forwarder) -> None:
    snap = {
        "ts": "2026-05-14T08:00:00Z",
        "steps": 100,
        "heart_rate": "",      # Shortcuts empty string → skip
        "weight": None,        # explicit null → skip
        "blood_oxygen": "null",  # literal "null" string from Shortcuts → skip
    }
    body = forwarder._build_metrics_payload(snap)
    types = [m["type"] for m in body["data"]["metrics"]]
    assert "HKQuantityTypeIdentifierStepCount" in types
    assert "HKQuantityTypeIdentifierHeartRate" not in types
    assert "HKQuantityTypeIdentifierBodyMass" not in types
    assert "HKQuantityTypeIdentifierOxygenSaturation" not in types


def test_sleep_window_synthesized_from_minutes_only(forwarder) -> None:
    snap = {"ts": "2026-05-14T08:00:00Z", "sleep_asleep_min": 412}
    body = forwarder._build_metrics_payload(snap)
    sleep = [m for m in body["data"]["metrics"]
             if m["type"] == "HKCategoryTypeIdentifierSleepAnalysis"]
    assert len(sleep) == 1
    sample = sleep[0]["data"][0]
    assert sample["startDate"] == "2026-05-14T08:00:00Z"
    assert sample["endDate"] == "2026-05-14T08:00:00Z"
    assert sample["qty"] == 412.0
    assert sample["stage"] == "asleep"


def test_sleep_window_uses_explicit_start_end(forwarder) -> None:
    snap = {
        "ts": "2026-05-14T08:00:00Z",
        "sleep_asleep_min": 412,
        "sleep_window": {
            "start": "2026-05-13T23:30:00Z",
            "end": "2026-05-14T07:15:00Z",
            "asleep_min": 420,   # window's own value should win over the top-level one
        },
    }
    body = forwarder._build_metrics_payload(snap)
    sample = next(
        m["data"][0] for m in body["data"]["metrics"]
        if m["type"] == "HKCategoryTypeIdentifierSleepAnalysis"
    )
    assert sample["startDate"] == "2026-05-13T23:30:00Z"
    assert sample["endDate"] == "2026-05-14T07:15:00Z"
    assert sample["qty"] == 420.0


def test_workouts_are_normalized(forwarder) -> None:
    snap = {
        "ts": "2026-05-14T08:00:00Z",
        "workouts": [
            {
                "type": "Walking",
                "start": "2026-05-14T07:30:00Z",
                "end": "2026-05-14T07:58:00Z",
                "duration_min": "28",
                "active_energy": "142.0",
                "distance_m": "2400",
            },
            {  # missing duration → skipped silently
                "type": "Running",
                "start": "2026-05-14T07:00:00Z",
                "end": "2026-05-14T07:30:00Z",
            },
        ],
    }
    body = forwarder._build_metrics_payload(snap)
    workouts = body["data"]["workouts"]
    assert len(workouts) == 1
    walk = workouts[0]
    assert walk["name"] == "Walking"
    assert walk["duration"] == 28.0
    assert walk["activeEnergy"] == 142.0
    assert walk["distance"] == 2400.0


def test_empty_snapshot_yields_empty_metrics(forwarder) -> None:
    body = forwarder._build_metrics_payload({})
    # ts is auto-filled but no metrics or workouts when nothing is provided
    assert body["data"]["metrics"] == []
    assert "workouts" not in body["data"]


def test_payload_passes_orchestrator_normalizer(forwarder) -> None:
    """End-to-end: the JSON the forwarder produces must be acceptable to the
    real HealthAutoExportNormalizer in the orchestrator. This catches drift
    between the two sides if either ever changes its key names."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from orchestrator.health import HealthAutoExportNormalizer
    finally:
        sys.path.pop(0)

    snap = {
        "ts": "2026-05-14T08:00:00Z",
        "steps": 1234,
        "heart_rate": 72.0,
        "weight": 82.3,
        "sleep_asleep_min": 412,
        "workouts": [{
            "type": "Walking",
            "start": "2026-05-14T07:30:00Z",
            "end": "2026-05-14T07:58:00Z",
            "duration_min": 28,
            "active_energy": 142.0,
        }],
    }
    body = forwarder._build_metrics_payload(snap)
    rows = HealthAutoExportNormalizer.normalize(body, default_member_id=1)

    # Steps + heart_rate + weight + sleep + workout = 5 normalized rows.
    metric_names = {row.get("metric") for row in rows}
    assert "steps" in metric_names
    assert "heart_rate" in metric_names
    assert "weight" in metric_names
    # Sleep produces a sleep_asleep row with non-zero asleep_min.
    sleep_rows = [r for r in rows if r.get("metric", "").startswith("sleep")]
    assert sleep_rows, f"expected a sleep_* row in {metric_names}"
    workout_rows = [r for r in rows if r.get("metric") == "workout"]
    assert len(workout_rows) == 1


def test_build_uses_now_when_ts_missing(forwarder, monkeypatch) -> None:
    fixed = datetime(2026, 5, 14, 8, 0, 0, tzinfo=UTC)

    class _FixedDT:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(forwarder, "datetime", _FixedDT)
    body = forwarder._build_metrics_payload({"steps": 1})
    sample = body["data"]["metrics"][0]["data"][0]
    assert sample["date"] == "2026-05-14T08:00:00Z"


def test_read_snapshot_rejects_non_object(forwarder, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", StringIO("[1, 2, 3]"))
    with pytest.raises(SystemExit) as exc:
        forwarder._read_snapshot()
    assert exc.value.code == 3


def test_read_snapshot_exits_zero_on_empty_input(forwarder, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(""))
    with pytest.raises(SystemExit) as exc:
        forwarder._read_snapshot()
    assert exc.value.code == 0
