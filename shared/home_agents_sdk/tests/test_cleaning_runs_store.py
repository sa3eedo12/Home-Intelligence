from __future__ import annotations

from home_agents_sdk.cleaning_runs_store import CleaningRunsStore


def test_typical_rooms_requires_repeated_rooms_once_history_grows() -> None:
    rows = [
        {"reported_rooms": ["living", "kitchen", "bedroom"]},
        {"reported_rooms": ["living", "kitchen", "office"]},
        {"reported_rooms": ["living", "kitchen"]},
    ]

    assert CleaningRunsStore._typical_rooms_from_rows(rows) == ["living", "kitchen"]


def test_typical_rooms_uses_single_run_as_seed_pattern() -> None:
    assert CleaningRunsStore._typical_rooms_from_rows(
        [{"reported_rooms": ["Living Room", "kitchen"]}]
    ) == ["living room", "kitchen"]
