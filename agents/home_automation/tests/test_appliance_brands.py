from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools import appliance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "query", "expected_brand", "summary_parts"),
    [
        (
            {
                "entity_id": "sensor.bosch_washer",
                "state": "running",
                "attributes": {
                    "friendly_name": "Bosch Washer",
                    "program": "Cottons",
                    "remaining_program_time": "00:38:00",
                },
            },
            "washer",
            "Bosch Home Connect",
            ("Bosch Washer", "Cottons program", "38 minutes remaining"),
        ),
        (
            {
                "entity_id": "sensor.miele_dishwasher",
                "state": "running",
                "attributes": {
                    "friendly_name": "Miele Dishwasher",
                    "program_phase": "Drying",
                    "time_remaining": "0:12:00",
                    "door_state": "closed",
                },
            },
            "dishwasher",
            "Miele",
            ("Miele Dishwasher", "Drying phase", "12 minutes remaining", "door closed"),
        ),
        (
            {
                "entity_id": "sensor.utility_washer",
                "state": "idle",
                "attributes": {"friendly_name": "Utility Washer"},
            },
            "washer",
            "generic",
            ("Utility Washer", "state idle"),
        ),
    ],
)
async def test_recent_appliance_activity_brand_detection(
    state: dict, query: str, expected_brand: str, summary_parts: tuple[str, ...], monkeypatch
) -> None:
    mock_client = AsyncMock()
    mock_client.list_states = AsyncMock(return_value=[state])
    mock_client.get_history_since = AsyncMock(return_value=[])
    monkeypatch.setattr(appliance, "get_ha_client", lambda: mock_client)

    result = await appliance.recent_appliance_activity(query)

    assert result["found"] is True
    entity = result["entities"][0]
    assert entity["brand"] == expected_brand
    assert isinstance(entity["highlights"], dict)
    for part in summary_parts:
        assert part in entity["friendly_summary"]
