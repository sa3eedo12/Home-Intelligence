"""Regression tests for climate (thermostat) tools."""
from __future__ import annotations

import json
from typing import Any

import pytest

import tools.climate as climate_mod
from tools.climate import (
    climate_set_mode,
    climate_set_temperature,
    climate_status,
)


class _FakeHAClient:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self.calls: list[tuple[str, str, dict]] = []

    async def render_template(self, _template: str) -> str:
        return json.dumps(self._entries)

    async def call_service(self, domain: str, service: str, data: dict) -> Any:
        self.calls.append((domain, service, data))
        return {"ok": True}


@pytest.fixture
def three_thermostats(monkeypatch):
    """Mirror Saeed's actual HA setup: three thermostats with friendly
    name 'Thermostat', distinguished only by area."""
    entries = [
        {
            "entity_id": "climate.thermostat",
            "name": "Thermostat",
            "area": "Office",
            "state": "cool",
            "current": 25,
            "target": 24,
            "min": 16,
            "max": 32,
            "hvac_modes": ["off", "cool"],
        },
        {
            "entity_id": "climate.thermostat_2",
            "name": "Thermostat",
            "area": "Bedroom",
            "state": "cool",
            "current": 24,
            "target": 23.5,
            "min": 16,
            "max": 32,
            "hvac_modes": ["off", "cool"],
        },
        {
            "entity_id": "climate.thermostat_3",
            "name": "Thermostat",
            "area": "Living Room",
            "state": "off",
            "current": 26,
            "target": 22,
            "min": 16,
            "max": 32,
            "hvac_modes": ["off", "cool"],
        },
    ]
    fake = _FakeHAClient(entries)
    monkeypatch.setattr(climate_mod, "get_ha_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_climate_status_lists_all_when_no_area(three_thermostats) -> None:
    out = await climate_status()
    assert out["ok"] is True
    assert len(out["thermostats"]) == 3
    areas = {t["area"] for t in out["thermostats"]}
    assert areas == {"Office", "Bedroom", "Living Room"}


@pytest.mark.asyncio
async def test_climate_status_resolves_area_case_insensitive(three_thermostats) -> None:
    out = await climate_status(area="bedroom")
    assert out["ok"] is True
    assert len(out["thermostats"]) == 1
    assert out["thermostats"][0]["entity_id"] == "climate.thermostat_2"


@pytest.mark.asyncio
async def test_climate_status_unknown_area_returns_available(three_thermostats) -> None:
    out = await climate_status(area="garage")
    assert out["ok"] is False
    assert out["error"] == "no_thermostat_in_area"
    assert "Bedroom" in out["available_areas"]


@pytest.mark.asyncio
async def test_set_temperature_targets_correct_thermostat_by_area(three_thermostats) -> None:
    """The headline regression: 'reduce the bedroom temperature' must
    hit climate.thermostat_2 (Bedroom), not _1 (Office) or _3
    (Living Room) — all three have friendly_name 'Thermostat'."""
    out = await climate_set_temperature(temperature=21, area="bedroom")
    assert out["ok"] is True
    assert out["entity_id"] == "climate.thermostat_2"
    assert out["area"] == "Bedroom"
    assert out["new_target"] == 21
    assert three_thermostats.calls == [
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.thermostat_2", "temperature": 21.0},
        )
    ]


@pytest.mark.asyncio
async def test_set_temperature_clamps_to_device_range(three_thermostats) -> None:
    out = await climate_set_temperature(temperature=50, area="bedroom")
    assert out["ok"] is True
    assert out["clamped"] is True
    assert out["new_target"] == 32  # max
    out2 = await climate_set_temperature(temperature=10, area="bedroom")
    assert out2["clamped"] is True
    assert out2["new_target"] == 16  # min


@pytest.mark.asyncio
async def test_set_temperature_no_match_returns_helpful_error(three_thermostats) -> None:
    out = await climate_set_temperature(temperature=22, area="kitchen")
    assert out["ok"] is False
    assert out["error"] == "no_thermostat_found"
    assert "Bedroom" in out["available_areas"]
    assert three_thermostats.calls == []


@pytest.mark.asyncio
async def test_set_temperature_falls_back_to_entity_id(three_thermostats) -> None:
    out = await climate_set_temperature(
        temperature=22, entity_id="climate.thermostat_3"
    )
    assert out["ok"] is True
    assert out["area"] == "Living Room"
    assert out["new_target"] == 22


@pytest.mark.asyncio
async def test_set_mode_rejects_unsupported_mode(three_thermostats) -> None:
    out = await climate_set_mode(mode="heat", area="bedroom")
    assert out["ok"] is False
    assert out["error"] == "unsupported_mode"
    assert "heat" not in [m.casefold() for m in out["supported"]]
    assert three_thermostats.calls == []


@pytest.mark.asyncio
async def test_set_mode_off_targets_correct_thermostat(three_thermostats) -> None:
    out = await climate_set_mode(mode="off", area="bedroom")
    assert out["ok"] is True
    assert out["entity_id"] == "climate.thermostat_2"
    assert three_thermostats.calls == [
        (
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.thermostat_2", "hvac_mode": "off"},
        )
    ]
