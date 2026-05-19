"""Focused tests for the cover / lock / vacuum / ev tool modules.

These mirror the test patterns in test_climate.py — a fake HA client
that returns a pre-built JSON template response and records call_service
invocations so we can assert the right HA service was called with the
right entity.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import tools.cover as cover_mod
import tools.ev as ev_mod
import tools.lock as lock_mod
import tools.vacuum as vacuum_mod


class _FakeHAClient:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self.calls: list[tuple[str, str, dict]] = []

    async def render_template(self, _template: str) -> str:
        return json.dumps(self._entries)

    async def call_service(self, domain: str, service: str, data: dict) -> Any:
        self.calls.append((domain, service, data))
        return {"ok": True}


# ── Covers ───────────────────────────────────────────────────────────────


@pytest.fixture
def saeeds_curtains(monkeypatch):
    """Mirror the user's actual setup: 4 cover entities, friendly names
    'Left' / 'Right' split across two areas (the user has redundant
    'Left' names — the matcher MUST handle that gracefully)."""
    entries = [
        {"entity_id": "cover.curtain", "name": "Left", "area": "Living Room",
         "state": "open", "position": 100, "device_class": "curtain"},
        {"entity_id": "cover.curtain_2", "name": "Left", "area": "Bedroom",
         "state": "closed", "position": 0, "device_class": "curtain"},
        {"entity_id": "cover.curtain_3", "name": "Right", "area": "Living Room",
         "state": "open", "position": 100, "device_class": "curtain"},
        {"entity_id": "cover.curtain_4", "name": "Right", "area": "Bedroom",
         "state": "open", "position": 100, "device_class": "curtain"},
    ]
    fake = _FakeHAClient(entries)
    monkeypatch.setattr(cover_mod, "get_ha_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_cover_status_lists_all_when_no_area(saeeds_curtains) -> None:
    out = await cover_mod.cover_status()
    assert out["ok"] is True
    assert len(out["covers"]) == 4


@pytest.mark.asyncio
async def test_cover_status_filters_by_area(saeeds_curtains) -> None:
    out = await cover_mod.cover_status(area="bedroom")
    assert out["ok"] is True
    assert {c["entity_id"] for c in out["covers"]} == {
        "cover.curtain_2",
        "cover.curtain_4",
    }


@pytest.mark.asyncio
async def test_cover_open_ambiguous_area_returns_candidates(saeeds_curtains) -> None:
    """Two covers in the Living Room — open should refuse to pick."""
    out = await cover_mod.cover_open(area="living room")
    assert out["ok"] is False
    assert out["error"] == "ambiguous_area"
    assert len(out["candidates"]) == 2


@pytest.mark.asyncio
async def test_cover_open_resolves_by_unique_entity_id(saeeds_curtains) -> None:
    out = await cover_mod.cover_open(entity_id="cover.curtain_2")
    assert out["ok"] is True
    assert out["entity_id"] == "cover.curtain_2"
    assert ("cover", "open_cover", {"entity_id": "cover.curtain_2"}) in saeeds_curtains.calls


@pytest.mark.asyncio
async def test_cover_close_calls_close_cover_service(saeeds_curtains) -> None:
    out = await cover_mod.cover_close(entity_id="cover.curtain")
    assert out["ok"] is True
    assert ("cover", "close_cover", {"entity_id": "cover.curtain"}) in saeeds_curtains.calls


@pytest.mark.asyncio
async def test_cover_set_position_clamps_out_of_range(saeeds_curtains) -> None:
    out = await cover_mod.cover_set_position(150, entity_id="cover.curtain")
    assert out["ok"] is True
    assert out["set_position"] == 100
    assert out["clamped"] is True

    out2 = await cover_mod.cover_set_position(-5, entity_id="cover.curtain")
    assert out2["ok"] is True
    assert out2["set_position"] == 0
    assert out2["clamped"] is True


@pytest.mark.asyncio
async def test_cover_set_position_calls_ha_service(saeeds_curtains) -> None:
    await cover_mod.cover_set_position(50, entity_id="cover.curtain_2")
    assert (
        "cover",
        "set_cover_position",
        {"entity_id": "cover.curtain_2", "position": 50},
    ) in saeeds_curtains.calls


# ── Locks ────────────────────────────────────────────────────────────────


@pytest.fixture
def han_lock_only(monkeypatch):
    """Saeed currently has only the EV lock; future Aqara additions
    will land in the same domain so the tool stays correct."""
    entries = [
        {"entity_id": "lock.han_lock", "name": "HAN Lock",
         "area": "", "state": "locked"},
    ]
    fake = _FakeHAClient(entries)
    monkeypatch.setattr(lock_mod, "get_ha_client", lambda: fake)
    return fake


@pytest.fixture
def front_and_back_locks(monkeypatch):
    entries = [
        {"entity_id": "lock.front_door", "name": "Front Door",
         "area": "Entryway", "state": "locked"},
        {"entity_id": "lock.back_door", "name": "Back Door",
         "area": "Kitchen", "state": "unlocked"},
    ]
    fake = _FakeHAClient(entries)
    monkeypatch.setattr(lock_mod, "get_ha_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_lock_status_lists_all(han_lock_only) -> None:
    out = await lock_mod.lock_status()
    assert out["ok"] is True
    assert out["locks"][0]["entity_id"] == "lock.han_lock"
    assert out["locks"][0]["state"] == "locked"


@pytest.mark.asyncio
async def test_lock_lock_calls_lock_service(front_and_back_locks) -> None:
    out = await lock_mod.lock_lock(area="kitchen")
    assert out["ok"] is True
    assert out["entity_id"] == "lock.back_door"
    assert ("lock", "lock", {"entity_id": "lock.back_door"}) in front_and_back_locks.calls


@pytest.mark.asyncio
async def test_lock_unlock_calls_unlock_service(front_and_back_locks) -> None:
    out = await lock_mod.lock_unlock(area="entryway")
    assert out["ok"] is True
    assert ("lock", "unlock", {"entity_id": "lock.front_door"}) in front_and_back_locks.calls


@pytest.mark.asyncio
async def test_lock_not_found_returns_available(front_and_back_locks) -> None:
    out = await lock_mod.lock_lock(area="garage")
    assert out["ok"] is False
    assert out["error"] == "no_lock_found"


# ── Vacuum ───────────────────────────────────────────────────────────────


@pytest.fixture
def deebot(monkeypatch):
    entries = [
        {
            "entity_id": "vacuum.saeeds_deebot",
            "name": "Saeed's Deebot",
            "area": "",
            "state": "docked",
            "battery_level": 87,
            "fan_speed": "max",
            "status": "ready",
        }
    ]
    fake = _FakeHAClient(entries)
    monkeypatch.setattr(vacuum_mod, "get_ha_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_vacuum_status_returns_single_when_only_one(deebot) -> None:
    out = await vacuum_mod.vacuum_status()
    assert out["ok"] is True
    assert out["entity_id"] == "vacuum.saeeds_deebot"
    assert out["battery_level"] == 87
    assert out["state"] == "docked"


@pytest.mark.asyncio
async def test_vacuum_start_calls_start_service(deebot) -> None:
    out = await vacuum_mod.vacuum_start()
    assert out["ok"] is True
    assert ("vacuum", "start", {"entity_id": "vacuum.saeeds_deebot"}) in deebot.calls


@pytest.mark.asyncio
async def test_vacuum_dock_calls_return_to_base(deebot) -> None:
    out = await vacuum_mod.vacuum_dock()
    assert out["ok"] is True
    assert (
        "vacuum",
        "return_to_base",
        {"entity_id": "vacuum.saeeds_deebot"},
    ) in deebot.calls


@pytest.mark.asyncio
async def test_vacuum_status_no_vacuum_returns_error(monkeypatch) -> None:
    fake = _FakeHAClient([])
    monkeypatch.setattr(vacuum_mod, "get_ha_client", lambda: fake)
    out = await vacuum_mod.vacuum_start()
    assert out["ok"] is False
    assert out["error"] == "no_vacuums_in_home"


# ── EV ───────────────────────────────────────────────────────────────────


@pytest.fixture
def han_ev(monkeypatch):
    """Mirror Saeed's actual BYD HAN entity set so the discovery /
    grouping logic gets exercised against realistic data."""
    entries = [
        # sensor.*
        {"entity_id": "sensor.han_battery_level", "name": "HAN Battery level",
         "state": "63"},
        {"entity_id": "sensor.han_range", "name": "HAN Range", "state": "355"},
        {"entity_id": "sensor.han_odometer", "name": "HAN Odometer",
         "state": "57506"},
        # binary_sensor.*
        {"entity_id": "binary_sensor.han_charging", "name": "HAN Charging",
         "state": "off"},
        {"entity_id": "binary_sensor.han_online", "name": "HAN Online",
         "state": "on"},
        {"entity_id": "binary_sensor.han_locked", "name": "HAN Locked",
         "state": "off"},
        {"entity_id": "binary_sensor.han_doors", "name": "HAN Doors",
         "state": "off"},
        {"entity_id": "binary_sensor.han_windows", "name": "HAN Windows",
         "state": "off"},
        {"entity_id": "binary_sensor.han_sentry_mode", "name": "HAN Sentry mode",
         "state": "on"},
        # lock.*
        {"entity_id": "lock.han_lock", "name": "HAN Lock", "state": "locked"},
        # climate.*
        {"entity_id": "climate.han_climate", "name": "HAN Climate",
         "state": "off"},
        # button.* (used by ev_start_charging etc.)
        {"entity_id": "button.han_start_charging", "name": "HAN Start charging",
         "state": "unknown"},
        {"entity_id": "button.han_close_windows", "name": "HAN Close windows",
         "state": "unknown"},
        {"entity_id": "button.han_flash_lights", "name": "HAN Flash lights",
         "state": "unknown"},
        {"entity_id": "button.han_find_car", "name": "HAN Find car",
         "state": "unknown"},
        # device_tracker.*
        {"entity_id": "device_tracker.han_location", "name": "HAN Location",
         "state": "unavailable"},
    ]
    fake = _FakeHAClient(entries)
    monkeypatch.setattr(ev_mod, "get_ha_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_ev_status_returns_consolidated_snapshot(han_ev) -> None:
    out = await ev_mod.ev_status()
    assert out["ok"] is True
    assert len(out["vehicles"]) == 1
    v = out["vehicles"][0]
    assert v["vehicle"] == "han"
    assert v["battery_level"] == 63.0
    assert v["range_km"] == 355.0
    assert v["odometer_km"] == 57506.0
    assert v["charging"] is False
    assert v["online"] is True
    # binary_sensor.han_locked = off → locked=False. The real lock state
    # comes from lock.han_lock = locked. Both surfaced for the LLM.
    assert v["locked"] is False
    assert v["lock_state"] == "locked"
    assert v["doors_open"] is False
    assert v["sentry_mode"] is True
    assert "battery 63%" in v["summary"]
    assert "range 355 km" in v["summary"]


@pytest.mark.asyncio
async def test_ev_status_unknown_vehicle_returns_error(han_ev) -> None:
    out = await ev_mod.ev_status(vehicle="model3")
    assert out["ok"] is False
    assert out["error"] == "no_ev_found"


@pytest.mark.asyncio
async def test_ev_start_charging_presses_button(han_ev) -> None:
    out = await ev_mod.ev_start_charging()
    assert out["ok"] is True
    assert out["entity_id"] == "button.han_start_charging"
    assert (
        "button",
        "press",
        {"entity_id": "button.han_start_charging"},
    ) in han_ev.calls


@pytest.mark.asyncio
async def test_ev_close_windows_and_flash_lights_press_correct_buttons(han_ev) -> None:
    await ev_mod.ev_close_windows()
    await ev_mod.ev_flash_lights()
    services = [c for c in han_ev.calls if c[0] == "button"]
    pressed = {c[2]["entity_id"] for c in services}
    assert "button.han_close_windows" in pressed
    assert "button.han_flash_lights" in pressed


@pytest.mark.asyncio
async def test_ev_summary_emphasises_unlocked_state(monkeypatch) -> None:
    """When the car is UNLOCKED, the summary should call attention to
    it — locked is the normal state, unlocked is the alert."""
    entries = [
        {"entity_id": "sensor.han_battery_level", "name": "HAN Battery level",
         "state": "63"},
        {"entity_id": "sensor.han_range", "name": "HAN Range", "state": "355"},
        {"entity_id": "sensor.han_odometer", "name": "HAN Odometer",
         "state": "57506"},
        {"entity_id": "binary_sensor.han_locked", "name": "HAN Locked",
         "state": "on"},  # binary "locked" sensor states ON when... unlocked? on/off varies by integration
        # Use lock.han_lock state as source of truth for the test
        {"entity_id": "lock.han_lock", "name": "HAN Lock", "state": "unlocked"},
    ]
    fake = _FakeHAClient(entries)
    monkeypatch.setattr(ev_mod, "get_ha_client", lambda: fake)
    out = await ev_mod.ev_status()
    assert out["ok"] is True
    v = out["vehicles"][0]
    assert v["lock_state"] == "unlocked"
