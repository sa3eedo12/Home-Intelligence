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


def _patch_client(
    monkeypatch,
    states: list[dict[str, Any]],
    *,
    switch_states: list[dict[str, Any]] | None = None,
):
    """Install a fake HA client with the given states + a recording call_service.

    When ``switch_states`` is provided, the fake returns it for
    ``list_states(domain="switch")`` calls; otherwise the switch domain
    returns an empty list (so legacy tests focused on light-domain
    behavior keep working without modification).
    """
    calls: list[dict[str, Any]] = []
    client = AsyncMock()

    async def fake_list_states(domain: str | None = None, **_kw: Any) -> list[dict[str, Any]]:
        if domain == "switch":
            return list(switch_states or [])
        return list(states)

    client.list_states = fake_list_states

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

    async def fake_list_states(domain: str | None = None, **_kw: Any) -> list[dict[str, Any]]:
        # No switch.* in this test — only light.* matters for the failure path.
        return list(states) if domain != "switch" else []

    client.list_states = fake_list_states

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


# ── switch.* light-switch coverage (May 19 bug — "switch off all the
#    lights" silently left wall-switched ceiling fixtures on) ───────────


@pytest.mark.asyncio
async def test_lights_off_turns_off_switch_domain_lights_too(monkeypatch) -> None:
    """Wall switches that ARE lights (Aqara wall_switch_*, switch.office_light,
    smart plugs powering lamps) must turn off alongside light.* entities.
    Uses Saeed's exact entity list from the May 19 incident as the fixture."""
    light_states = _ha_states(
        ("light.lightbulb", "on", "Office lightbulb"),
        ("light.hue_play_gradient_lightstrip_1", "on", "Hue play gradient lightstrip 1"),
    )
    switch_states = _ha_states(
        ("switch.wall_switch", "on", "Wall switch"),
        ("switch.wall_switch_2", "on", "Wall switch"),
        ("switch.wall_switch_3", "on", "Wall switch"),
        ("switch.office_light", "on", "Office light"),
        # Non-light switches — MUST NOT be touched
        ("switch.pc_power", "on", "PC Power"),
        ("switch.headphones", "on", "Headphones"),
        ("switch.250w_prime_charger_usb_c_port_1", "on", "USB-C Port 1"),
        ("switch.unifi_network_ha", "on", "UniFi Network HA"),
        ("switch.han_a_c_on", "on", "HAN A/C"),
        ("switch.aqara_hub_e1_4e55_pairing_mode", "on", "Aqara Hub pairing"),
        ("switch.sound_sensor_tv_sound_detection", "on", "Sound sensor"),
        ("switch.light_sensor_tv", "on", "Light sensor TV"),
    )
    calls = _patch_client(monkeypatch, light_states, switch_states=switch_states)
    result = await lights_control.lights_off()

    turned_off_ids = {t["entity_id"] for t in result["turned_off"]}
    # All real lights + all wall switches + office_light must be off
    assert turned_off_ids == {
        "light.lightbulb",
        "light.hue_play_gradient_lightstrip_1",
        "switch.wall_switch",
        "switch.wall_switch_2",
        "switch.wall_switch_3",
        "switch.office_light",
    }
    # And the non-light switches MUST stay untouched
    untouched = {
        "switch.pc_power",
        "switch.headphones",
        "switch.250w_prime_charger_usb_c_port_1",
        "switch.unifi_network_ha",
        "switch.han_a_c_on",
        "switch.aqara_hub_e1_4e55_pairing_mode",
        "switch.sound_sensor_tv_sound_detection",
        "switch.light_sensor_tv",
    }
    assert untouched.isdisjoint(turned_off_ids)
    # Service calls dispatch to the correct domain per entity
    light_calls = [c for c in calls if c["data"]["entity_id"].startswith("light.")]
    switch_calls = [c for c in calls if c["data"]["entity_id"].startswith("switch.")]
    assert all(c["domain"] == "light" for c in light_calls)
    assert all(c["domain"] == "switch" for c in switch_calls)
    assert len(light_calls) == 2
    assert len(switch_calls) == 4


@pytest.mark.asyncio
async def test_lights_off_include_switches_false_restricts_to_light_domain(
    monkeypatch,
) -> None:
    """Caller opting out (include_switches=False) gets the old behaviour."""
    light_states = _ha_states(("light.lightbulb", "on", "Bulb"))
    switch_states = _ha_states(("switch.wall_switch", "on", "Wall switch"))
    calls = _patch_client(monkeypatch, light_states, switch_states=switch_states)
    result = await lights_control.lights_off(include_switches=False)

    assert {t["entity_id"] for t in result["turned_off"]} == {"light.lightbulb"}
    assert all(c["domain"] == "light" for c in calls)


@pytest.mark.asyncio
async def test_lights_on_does_not_pass_brightness_to_switch_domain(
    monkeypatch,
) -> None:
    """switch.turn_on has no brightness parameter — must not be sent."""
    light_states = _ha_states(("light.bulb", "off", "Bulb"))
    switch_states = _ha_states(("switch.wall_switch", "off", "Wall switch"))
    calls = _patch_client(monkeypatch, light_states, switch_states=switch_states)
    result = await lights_control.lights_on(brightness=180)

    assert result["ok"] is True
    light_call = next(c for c in calls if c["domain"] == "light")
    switch_call = next(c for c in calls if c["domain"] == "switch")
    assert light_call["data"].get("brightness") == 180
    assert "brightness" not in switch_call["data"]


def test_is_light_switch_excludes_non_light_switches() -> None:
    """Hard-coded test against the user's real switch entities so the
    keyword-list never regresses."""
    # SHOULD be detected as light switches
    for eid, name in [
        ("switch.wall_switch", "Wall switch"),
        ("switch.wall_switch_3", "Wall switch"),
        ("switch.office_light", "Office light"),
        ("switch.lamp_corner", "Corner Lamp"),
        ("switch.kitchen_ceiling_led", "Kitchen Ceiling LED"),
    ]:
        assert lights_control._is_light_switch(eid, name), (
            f"{eid} should be a light switch"
        )
    # SHOULD NOT be detected as light switches
    for eid, name in [
        ("switch.pc_power", "PC Power"),
        ("switch.headphones", "Headphones"),
        ("switch.speakers", "Speakers"),
        ("switch.250w_prime_charger_usb_c_port_1", "USB-C Port 1"),
        ("switch.unifi_network_ha", "UniFi Network HA"),
        ("switch.han_a_c_on", "HAN A/C"),
        ("switch.aqara_hub_e1_4e55_pairing_mode", "Aqara Hub pairing"),
        ("switch.sound_sensor_tv_sound_detection", "Sound sensor"),
        ("switch.light_sensor_tv", "Light sensor TV"),
        ("switch.washer_bubble_soak", "Bubble Soak"),
        ("switch.office_thermostat", "Office Thermostat"),
    ]:
        assert not lights_control._is_light_switch(eid, name), (
            f"{eid} should NOT be a light switch"
        )
