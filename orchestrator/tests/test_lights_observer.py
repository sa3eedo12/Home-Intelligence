"""Tests for orchestrator.observers.lights_observer."""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.lights_observer import LightsObserver


def _light_payload(
    state: str,
    ts: str,
    entity_id: str = "light.living_room_lamp",
    friendly_name: str = "Living Room Lamp",
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "ts": ts,
        "attributes": {"friendly_name": friendly_name},
    }


@pytest.mark.asyncio
async def test_lights_observer_tracks_on_state() -> None:
    observer = LightsObserver()
    await observer.handle(_light_payload("on", "2026-05-14T22:00:00+00:00"))
    snap = observer.snapshot()
    assert snap["count"] == 1
    assert snap["lights"][0]["entity_id"] == "light.living_room_lamp"
    assert snap["lights"][0]["friendly_name"] == "Living Room Lamp"
    assert snap["lights"][0]["on_since"] is not None


@pytest.mark.asyncio
async def test_lights_observer_drops_off_state() -> None:
    observer = LightsObserver()
    await observer.handle(_light_payload("on", "2026-05-14T22:00:00+00:00"))
    await observer.handle(_light_payload("off", "2026-05-14T22:30:00+00:00"))
    snap = observer.snapshot()
    assert snap["count"] == 0
    # Still tracked but not 'on'
    assert snap["tracked_entities"] == 1


@pytest.mark.asyncio
async def test_lights_observer_ignores_non_light_domain() -> None:
    observer = LightsObserver()
    await observer.handle(
        {
            "entity_id": "switch.kitchen_socket",
            "state": "on",
            "ts": "2026-05-14T22:00:00+00:00",
            "attributes": {"friendly_name": "Kitchen Socket"},
        }
    )
    assert observer.snapshot()["count"] == 0


@pytest.mark.asyncio
async def test_lights_observer_filters_tv_backlight_entities() -> None:
    """Backlights and indicator strips on TVs/monitors flap with the
    device — they're NOT lights the user wants bedtime nudges about."""
    observer = LightsObserver()
    await observer.handle(
        _light_payload(
            "on",
            "2026-05-14T22:00:00+00:00",
            entity_id="light.living_room_tv_backlight",
            friendly_name="Living Room TV Backlight",
        )
    )
    await observer.handle(
        _light_payload(
            "on",
            "2026-05-14T22:00:00+00:00",
            entity_id="light.office_monitor_indicator",
            friendly_name="Office Monitor Indicator",
        )
    )
    assert observer.snapshot()["count"] == 0


@pytest.mark.asyncio
async def test_lights_observer_resets_since_on_off() -> None:
    """on_since must clear when light goes off so a later 'on' uses
    the new timestamp."""
    observer = LightsObserver()
    await observer.handle(_light_payload("on", "2026-05-14T22:00:00+00:00"))
    first_since = observer.snapshot()["lights"][0]["on_since"]
    await observer.handle(_light_payload("off", "2026-05-14T22:30:00+00:00"))
    await observer.handle(_light_payload("on", "2026-05-14T23:00:00+00:00"))
    second_since = observer.snapshot()["lights"][0]["on_since"]
    assert second_since != first_since


@pytest.mark.asyncio
async def test_lights_observer_handles_multiple_lights() -> None:
    observer = LightsObserver()
    await observer.handle(
        _light_payload(
            "on",
            "2026-05-14T22:00:00+00:00",
            entity_id="light.living_room",
            friendly_name="Living Room",
        )
    )
    await observer.handle(
        _light_payload(
            "on",
            "2026-05-14T22:00:00+00:00",
            entity_id="light.kitchen",
            friendly_name="Kitchen",
        )
    )
    await observer.handle(
        _light_payload(
            "on",
            "2026-05-14T22:00:00+00:00",
            entity_id="light.hallway",
            friendly_name="Hallway",
        )
    )
    snap = observer.snapshot()
    assert snap["count"] == 3
    names = {item["friendly_name"] for item in snap["lights"]}
    assert names == {"Living Room", "Kitchen", "Hallway"}
