from __future__ import annotations

import pytest
from home_agents_sdk.cleaning_runs_store import CleaningRunsStore

from tools.cleaning_runs import (
    VALID_STATUSES,
    _infer_status,
    _keyboard_for,
    _rooms_from_payload,
    infer_cleaning_run,
)


def test_typical_rooms_detects_common_recent_rooms() -> None:
    rows = [
        {"reported_rooms": ["Living", "Kitchen", "Bedroom"]},
        {"reported_rooms": ["living", "kitchen", "bedroom", "office"]},
        {"reported_rooms": ["living", "kitchen"]},
    ]

    typical = CleaningRunsStore._typical_rooms_from_rows(rows)

    assert typical == ["living", "kitchen", "bedroom"]


def test_rooms_from_payload_normalises_and_deduplicates() -> None:
    rooms = _rooms_from_payload({"rooms": [" Living Room ", "living_room", "Kitchen"]})

    assert rooms == ["living room", "kitchen"]


def test_infer_status_full_when_expected_rooms_are_covered() -> None:
    status, missed, reason = _infer_status(
        ["living", "kitchen", "bedroom"], ["living", "kitchen", "bedroom"]
    )

    assert status == "full"
    assert missed == []
    assert "cover" in reason


def test_infer_status_partial_when_expected_rooms_are_missing() -> None:
    status, missed, reason = _infer_status(["living", "kitchen"], ["living", "kitchen", "bedroom"])

    assert status == "partial"
    assert missed == ["bedroom"]
    assert "bedroom" in reason


def test_infer_status_unusual_when_reported_rooms_do_not_overlap() -> None:
    status, missed, reason = _infer_status(["garage"], ["living", "kitchen", "bedroom"])

    assert status == "unusual"
    assert missed == ["living", "kitchen", "bedroom"]
    assert "overlap" in reason


def test_keyboard_has_clean_callbacks_and_skip_button() -> None:
    keyboard = _keyboard_for(42, "partial")
    flat = [btn["callback"] for row in keyboard for btn in row]

    assert keyboard[0][0]["callback"] == "clean:42:partial"
    assert keyboard[0][0]["text"].startswith("✅ ")
    for status in VALID_STATUSES:
        assert f"clean:42:{status}" in flat
    assert "clean:42:_skip" in flat


def test_full_keyboard_is_quiet_acknowledge_only() -> None:
    assert _keyboard_for(7, "full") == [[{"text": "Acknowledge", "callback": "clean:7:_skip"}]]


@pytest.mark.asyncio
async def test_infer_cleaning_run_persists_partial_with_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class FakeStore:
        def __init__(self, pool: object) -> None:
            self.pool = pool

        async def typical_rooms(self, limit_history: int = 10) -> list[str]:
            return ["living", "kitchen", "bedroom"]

        async def insert_run(self, **kwargs: object) -> int:
            captured.update(kwargs)
            return 99

    async def fake_pool() -> object:
        return object()

    monkeypatch.setattr("tools.cleaning_runs._pool", fake_pool)
    monkeypatch.setattr("tools.cleaning_runs.CleaningRunsStore", FakeStore)

    result = await infer_cleaning_run(
        {
            "entity_id": "vacuum.roomba",
            "rooms": ["living", "kitchen"],
            "duration_seconds": "1800",
            "ended_at": "2026-05-13T19:00:00Z",
        }
    )

    assert result["status"] == "partial"
    assert result["missed_rooms"] == ["bedroom"]
    assert result["cleaning_run_id"] == 99
    assert result["keyboard"][0][0]["callback"] == "clean:99:partial"
    assert captured["reported_rooms"] == ["living", "kitchen"]
    assert captured["expected_rooms"] == ["living", "kitchen", "bedroom"]
    assert captured["guessed_status"] == "partial"
