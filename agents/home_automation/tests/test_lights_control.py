"""Tests for the lights_control tools."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from tools import lights_control


def _ha_states(*lights: tuple[str, str, str]) -> list[dict[str, Any]]:
    """Each tuple is (entity_id, state, friendly_name)."""
    return [
        {
            "entity_id": eid,
            "state": state,
            "attributes": {"friendly_name": name},
        }
        for eid, state, name in lights
    ]


def _patch_client(monkeypatch, states: list[dict[str, Any]]):
    """Install a fake HA client with the given states + a recording call_service."""
    calls: list[dict[str, Any]] = []
    client = AsyncMock()
    client.list_states = AsyncMock(return_value=states)

    async def fake_call_service(domain: str, service: str, data: dict) -> dict:
        calls.append({"domain": domain, "service": service, "data": data})
        return {"ok": True}

    client.call_service = fake_call_service
    monkeypatch.setattr(lights_control, "get_ha_client", lambda: client)
    return calls


# ── lights_off ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lights_off_turns_off_all_currently_on(monkeypatch) -> None:
    calls = _patch_client(
        monkeypatch,
        _ha_states(
            ("light.living_room", "on", "Living Room"),
            ("light.kitchen", "on", "Kitchen"),
            ("light.bedroom", "off", "Bedroom"),
        ),
    )
    result = await lights_control.lights_off()
    assert result["ok"] is True
    assert len(result["turned_off"]) == 2
    # Bedroom skipped because it was already off
    assert any(s["reason"] == "already_off" for s in result["skipped"])
    # Two service calls fired
    assert len(calls) == 2
    assert {c["data"]["entity_id"] for c in calls} == {
        "light.living_room",
        "light.kitchen",
    }


@pytest.mark.asyncio
async def test_lights_off_filters_backlights_and_indicators(monkeypatch) -> None:
    """REGRESSION: 'turn off all the lights' shouldn't kill TV backlights."""
    calls = _patch_client(
        monkeypatch,
        _ha_states(
            ("light.living_room", "on", "Living Room"),
            ("light.tv_backlight", "on", "TV Backlight"),
            ("light.office_monitor_indicator", "on", "Office Monitor Indicator"),
        ),
    )
    result = await lights_control.lights_off()
    assert len(result["turned_off"]) == 1
    assert result["turned_off"][0]["entity_id"] == "light.living_room"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lights_off_with_area_filter(monkeypatch) -> None:
    calls = _patch_client(
        monkeypatch,
        _ha_states(
            ("light.kitchen_main", "on", "Kitchen Main"),
            ("light.living_room", "on", "Living Room"),
        ),
    )
    result = await lights_control.lights_off(area="kitchen")
    assert len(result["turned_off"]) == 1
    assert "Kitchen Main" in result["summary"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lights_off_explicit_entity_ids(monkeypatch) -> None:
    calls = _patch_client(
        monkeypatch,
        _ha_states(
            ("light.living_room", "on", "Living Room"),
            ("light.kitchen", "on", "Kitchen"),
        ),
    )
    result = await lights_control.lights_off(
        entity_ids=["light.kitchen"]
    )
    assert len(result["turned_off"]) == 1
    assert calls[0]["data"]["entity_id"] == "light.kitchen"


@pytest.mark.asyncio
async def test_lights_off_empty_when_nothing_on(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        _ha_states(
            ("light.living_room", "off", "Living Room"),
            ("light.kitchen", "off", "Kitchen"),
        ),
    )
    result = await lights_control.lights_off()
    assert result["ok"] is True
    assert result["turned_off"] == []
    assert "No lights are currently on" in result["summary"]


@pytest.mark.asyncio
async def test_lights_off_handles_service_call_failure(monkeypatch) -> None:
    """Per-light failure must not cancel the rest of the operation."""
    states = _ha_states(
        ("light.living_room", "on", "Living Room"),
        ("light.kitchen", "on", "Kitchen"),
    )
    client = AsyncMock()
    client.list_states = AsyncMock(return_value=states)

    call_count = {"n": 0}

    async def flaky(domain: str, service: str, data: dict) -> dict:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("HA timeout")
        return {"ok": True}

    client.call_service = flaky
    monkeypatch.setattr(lights_control, "get_ha_client", lambda: client)

    result = await lights_control.lights_off()
    assert result["ok"] is False  # one failed
    assert len(result["turned_off"]) == 1
    assert any("HA timeout" in s.get("reason", "") for s in result["skipped"])


# ── lights_on ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lights_on_with_brightness(monkeypatch) -> None:
    calls = _patch_client(
        monkeypatch,
        _ha_states(("light.bedroom", "off", "Bedroom")),
    )
    result = await lights_control.lights_on(
        entity_ids=["light.bedroom"], brightness=200
    )
    assert result["ok"] is True
    assert calls[0]["data"]["brightness"] == 200


@pytest.mark.asyncio
async def test_lights_on_clamps_brightness_to_valid_range(monkeypatch) -> None:
    calls = _patch_client(
        monkeypatch,
        _ha_states(("light.x", "off", "X")),
    )
    await lights_control.lights_on(entity_ids=["light.x"], brightness=999)
    assert calls[0]["data"]["brightness"] == 255


@pytest.mark.asyncio
async def test_lights_on_skips_already_on(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        _ha_states(("light.x", "on", "X")),
    )
    result = await lights_control.lights_on(entity_ids=["light.x"])
    assert result["turned_on"] == []
    assert "already on" in result["summary"]


# ── lights_status ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lights_status_returns_count_excluding_filtered(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        _ha_states(
            ("light.living_room", "on", "Living Room"),
            ("light.kitchen", "off", "Kitchen"),
            ("light.tv_backlight", "on", "TV Backlight"),
        ),
    )
    result = await lights_control.lights_status()
    assert result["on_count"] == 1
    assert result["total_count"] == 2
    assert result["on_lights"][0]["friendly_name"] == "Living Room"
