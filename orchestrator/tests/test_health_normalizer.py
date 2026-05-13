from __future__ import annotations

from orchestrator.health import HealthAutoExportNormalizer


def test_normalizes_quantity_samples() -> None:
    payload = {
        "data": {
            "metrics": [
                {
                    "type": "HKQuantityTypeIdentifierStepCount",
                    "unit": "count",
                    "data": [{"date": "2026-05-13T08:00:00Z", "qty": 1234}],
                }
            ]
        }
    }

    rows = HealthAutoExportNormalizer.normalize(payload, default_member_id=7)

    assert rows[0]["metric"] == "steps"
    assert rows[0]["value"] == 1234
    assert rows[0]["unit"] == "steps"
    assert rows[0]["member_id"] == 7


def test_normalizes_heart_rate_and_weight_units() -> None:
    payload = {
        "data": {
            "metrics": [
                {
                    "type": "HKQuantityTypeIdentifierRestingHeartRate",
                    "data": [{"date": "2026-05-13T08:00:00Z", "qty": 58}],
                },
                {
                    "type": "HKQuantityTypeIdentifierBodyMass",
                    "data": [{"date": "2026-05-13T08:01:00Z", "qty": "82.5"}],
                },
            ]
        }
    }

    rows = HealthAutoExportNormalizer.normalize(payload)

    assert [row["metric"] for row in rows] == ["resting_heart_rate", "weight"]
    assert rows[0]["unit"] == "bpm"
    assert rows[1]["unit"] == "kg"
    assert rows[1]["value"] == 82.5


def test_splits_sleep_stages_into_stage_metrics() -> None:
    payload = {
        "data": {
            "metrics": [
                {
                    "type": "HKCategoryTypeIdentifierSleepAnalysis",
                    "data": [
                        {
                            "startDate": "2026-05-12T22:00:00Z",
                            "endDate": "2026-05-13T02:00:00Z",
                            "value": "HKCategoryValueSleepAnalysisAsleepDeep",
                        },
                        {
                            "startDate": "2026-05-13T02:00:00Z",
                            "endDate": "2026-05-13T03:00:00Z",
                            "value": "HKCategoryValueSleepAnalysisAwake",
                        },
                    ],
                }
            ]
        }
    }

    rows = HealthAutoExportNormalizer.normalize(payload)

    assert [row["metric"] for row in rows] == ["sleep_deep", "sleep_awake"]
    assert rows[0]["value"] == 240
    assert rows[0]["unit"] == "min"
    assert rows[0]["metadata"]["sleep_stage"] == "AsleepDeep"


def test_normalizes_workouts_with_type_metadata() -> None:
    payload = {
        "data": {
            "workouts": [
                {
                    "startDate": "2026-05-13T06:00:00Z",
                    "endDate": "2026-05-13T06:45:00Z",
                    "workoutActivityType": "running",
                    "activeEnergy": 420,
                }
            ]
        }
    }

    rows = HealthAutoExportNormalizer.normalize(payload)

    assert rows[0]["metric"] == "workout"
    assert rows[0]["value"] == 45
    assert rows[0]["metadata"]["workout_type"] == "running"
    assert rows[0]["metadata"]["activeEnergy"] == 420


def test_unknown_types_are_preserved_as_other_metric() -> None:
    payload = {
        "data": {
            "metrics": [
                {
                    "type": "HKQuantityTypeIdentifierFlightsClimbed",
                    "data": [{"date": "2026-05-13T08:00:00Z", "qty": 9}],
                }
            ]
        }
    }

    rows = HealthAutoExportNormalizer.normalize(payload)

    assert rows[0]["metric"] == "other.HKQuantityTypeIdentifierFlightsClimbed"
    assert rows[0]["raw"]["sample"]["qty"] == 9


def test_skips_rows_missing_timestamp() -> None:
    payload = {
        "data": {
            "metrics": [
                {
                    "type": "HKQuantityTypeIdentifierHeartRate",
                    "data": [{"qty": 72}, {"date": "2026-05-13T08:00:00Z", "qty": 70}],
                }
            ]
        }
    }

    rows = HealthAutoExportNormalizer.normalize(payload)

    assert len(rows) == 1
    assert rows[0]["metric"] == "heart_rate"


def test_normalizes_mindful_sessions_as_minutes() -> None:
    payload = {
        "data": {
            "metrics": [
                {
                    "type": "HKCategoryTypeIdentifierMindfulSession",
                    "data": [
                        {
                            "startDate": "2026-05-13T05:00:00Z",
                            "endDate": "2026-05-13T05:10:00Z",
                        }
                    ],
                }
            ]
        }
    }

    rows = HealthAutoExportNormalizer.normalize(payload)

    assert rows[0]["metric"] == "mindfulness"
    assert rows[0]["value"] == 10
