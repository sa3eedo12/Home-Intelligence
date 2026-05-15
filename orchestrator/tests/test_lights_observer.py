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


# ── seed_from_ha_states (REGRESSION) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_from_ha_states_picks_up_already_on_lights() -> None:
    """REGRESSION: bedtime nudge said '1 light on' when HA actually had 5
    on, because the observer only saw state TRANSITIONS — already-on
    lights at boot were invisible."""
    obs = LightsObserver()
    seeded = obs.seed_from_ha_states(
        [
            {
                "entity_id": "light.living_room",
                "state": "on",
                "last_changed": "2026-05-15T20:00:00+00:00",
                "attributes": {"friendly_name": "Living Room"},
            },
            {
                "entity_id": "light.kitchen",
                "state": "off",
                "attributes": {"friendly_name": "Kitchen"},
            },
            {
                "entity_id": "light.tv_backlight",  # filtered: backlight
                "state": "on",
                "attributes": {"friendly_name": "TV Backlight"},
            },
            {
                "entity_id": "switch.kitchen_outlet",  # filtered: not a light
                "state": "on",
                "attributes": {"friendly_name": "Outlet"},
            },
        ]
    )

    snap = obs.snapshot()
    # Backlight + non-light filtered out
    assert seeded == 2  # Living Room + Kitchen
    assert snap["count"] == 1  # Only Living Room is ON
    assert snap["lights"][0]["entity_id"] == "light.living_room"
    assert snap["lights"][0]["on_since"] is not None


@pytest.mark.asyncio
async def test_seed_skips_unavailable_states() -> None:
    obs = LightsObserver()
    obs.seed_from_ha_states(
        [
            {
                "entity_id": "light.broken",
                "state": "unavailable",
                "attributes": {"friendly_name": "Broken"},
            },
            {
                "entity_id": "light.unknown_one",
                "state": "unknown",
                "attributes": {"friendly_name": "Unknown"},
            },
        ]
    )
    assert obs.snapshot()["count"] == 0
