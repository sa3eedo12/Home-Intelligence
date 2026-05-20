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
    post_state_overrides: dict[str, str] | None = None,
):
    """Install a fake HA client with the given states + a recording call_service.

    When ``switch_states`` is provided, the fake returns it for
    ``list_states(domain="switch")`` calls; otherwise the switch domain
    returns an empty list (so legacy tests focused on light-domain
    behavior keep working without modification).

    ``post_state_overrides`` lets a test simulate "device didn't actually
    respond" for the post-state verification path — map entity_id to
    the state we want ``get_state`` to return after the service call.
    By default verification returns the expected post-state (i.e. an
    on→off call sees state='off'), so the verification passes silently.
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

    async def fake_get_state(entity_id: str) -> dict[str, Any]:
        overrides = post_state_overrides or {}
        if entity_id in overrides:
            return {"entity_id": entity_id, "state": overrides[entity_id]}
        # Default: assume the device responded — return the expected post-state.
        last_call = next(
            (c for c in reversed(calls) if c["data"].get("entity_id") == entity_id),
            None,
        )
        if last_call is None:
            return {"entity_id": entity_id, "state": "unknown"}
        return {
            "entity_id": entity_id,
            "state": "on" if last_call["service"] == "turn_on" else "off",
        }

    client.get_state = fake_get_state
    monkeypatch.setattr(lights_control, "get_ha_client", lambda: client)
    # Test-mode: skip the 1.5s settle delay so the suite stays fast.
    monkeypatch.setattr(lights_control, "_VERIFY_DELAY_S", 0.0)
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

    async def fake_get_state(entity_id: str) -> dict[str, Any]:
        # The successful call's entity transitions to off; the failed
        # call's entity stays on (HA never received the command).
        return {"entity_id": entity_id, "state": "off" if entity_id == "light.kitchen" else "on"}

    client.get_state = fake_get_state
    monkeypatch.setattr(lights_control, "get_ha_client", lambda: client)
    monkeypatch.setattr(lights_control, "_VERIFY_DELAY_S", 0.0)

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
    # Test specifically exercises the opt-in include_switches behavior +
    # whole-house guard bypass via confirm_all=True.
    result = await lights_control.lights_off(
        confirm_all=True, include_switches=True
    )

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
async def test_lights_off_default_targets_light_domain_only(
    monkeypatch,
) -> None:
    """Default behaviour: only light.* domain. User reclassifies wall
    switches as lights in HA itself (Show as Light) instead of relying
    on our heuristic. Old behavior (include_switches=True) is opt-in."""
    light_states = _ha_states(("light.lightbulb", "on", "Bulb"))
    switch_states = _ha_states(("switch.wall_switch", "on", "Wall switch"))
    calls = _patch_client(monkeypatch, light_states, switch_states=switch_states)
    # No include_switches kwarg → defaults to False now.
    result = await lights_control.lights_off()

    assert {t["entity_id"] for t in result["turned_off"]} == {"light.lightbulb"}
    assert all(c["domain"] == "light" for c in calls)


@pytest.mark.asyncio
async def test_lights_off_include_switches_true_opts_in(
    monkeypatch,
) -> None:
    """Power users (or legacy callers) can still pull in switch-domain
    lights with include_switches=True."""
    light_states = _ha_states(("light.lightbulb", "on", "Bulb"))
    switch_states = _ha_states(("switch.wall_switch", "on", "Wall switch"))
    calls = _patch_client(monkeypatch, light_states, switch_states=switch_states)
    result = await lights_control.lights_off(
        include_switches=True, confirm_all=True
    )

    assert {t["entity_id"] for t in result["turned_off"]} == {
        "light.lightbulb",
        "switch.wall_switch",
    }
    call_domains = {c["domain"] for c in calls}
    assert call_domains == {"light", "switch"}


@pytest.mark.asyncio
async def test_lights_on_does_not_pass_brightness_to_switch_domain(
    monkeypatch,
) -> None:
    """switch.turn_on has no brightness parameter — must not be sent."""
    light_states = _ha_states(("light.bulb", "off", "Bulb"))
    switch_states = _ha_states(("switch.wall_switch", "off", "Wall switch"))
    calls = _patch_client(monkeypatch, light_states, switch_states=switch_states)
    # Opt-in to switch coverage so this test exercises the dual-domain dispatch.
    result = await lights_control.lights_on(brightness=180, include_switches=True)

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
        # Regression: 'enabled' contains 'led' as a substring; the
        # word-boundary matcher for 'led' must not fire on it.
        ("switch.husamsaf", "Husamsaf Enabled"),
        ("switch.husamsaf_2", "Husamsaf Enabled"),
        # 'pulled', 'fulfilled', 'called' etc. — same family of
        # substring false positives.
        ("switch.scheduler_enabled", "Scheduler Enabled"),
        ("switch.api_disable", "API Disable"),
    ]:
        assert not lights_control._is_light_switch(eid, name), (
            f"{eid} should NOT be a light switch"
        )


# ── Post-state verification (May 20 'office light is now on' lie bug) ──


@pytest.mark.asyncio
async def test_lights_on_reports_failure_when_device_state_unchanged(
    monkeypatch,
) -> None:
    """The exact switch.office_light incident: HA returned 200 OK to
    switch.turn_on, but the Aqara device was offline and the entity
    stayed 'off'. The tool MUST report failure, not fabricate success."""
    light_states = _ha_states()  # no lights
    switch_states = _ha_states(("switch.office_light", "off", "Office Light"))
    calls = _patch_client(
        monkeypatch,
        light_states,
        switch_states=switch_states,
        # Simulate the offline Aqara: post-state stays 'off' even after
        # the turn_on call succeeded (HA returned 200).
        post_state_overrides={"switch.office_light": "off"},
    )
    result = await lights_control.lights_on(area="office", include_switches=True)

    assert result["ok"] is False
    # Nothing actually turned on — moved to failures with the honest reason.
    assert result["turned_on"] == []
    failures = [s for s in result["skipped"] if s.get("reason") == "state_unchanged_after_call"]
    assert len(failures) == 1
    assert failures[0]["entity_id"] == "switch.office_light"
    assert failures[0]["actual_state"] == "off"
    # Summary tells the user the truth.
    assert "didn't respond" in result["summary"] or "Couldn't turn on" in result["summary"]
    assert "Office Light" in result["summary"]
    # The service call still WAS attempted (verifies we tried before giving up).
    assert len(calls) == 1
    assert calls[0]["service"] == "turn_on"


@pytest.mark.asyncio
async def test_lights_off_reports_failure_when_device_state_unchanged(
    monkeypatch,
) -> None:
    """Mirror of the lights_on case for the turn-off path."""
    light_states = _ha_states(("light.dead_bulb", "on", "Dead Bulb"))
    calls = _patch_client(
        monkeypatch,
        light_states,
        post_state_overrides={"light.dead_bulb": "on"},  # stuck on
    )
    result = await lights_control.lights_off()

    assert result["ok"] is False
    assert result["turned_off"] == []
    failures = [s for s in result["skipped"] if s.get("reason") == "state_unchanged_after_call"]
    assert len(failures) == 1
    assert failures[0]["entity_id"] == "light.dead_bulb"


@pytest.mark.asyncio
async def test_lights_off_reports_partial_success_with_mixed_outcomes(
    monkeypatch,
) -> None:
    """Some devices respond, some don't — turned_off has the live ones,
    skipped names the stuck ones."""
    light_states = _ha_states(
        ("light.responsive_bulb", "on", "Responsive Bulb"),
        ("light.stuck_bulb", "on", "Stuck Bulb"),
    )
    calls = _patch_client(
        monkeypatch,
        light_states,
        post_state_overrides={"light.stuck_bulb": "on"},
    )
    result = await lights_control.lights_off()

    assert result["ok"] is False  # one failed verification
    assert {t["entity_id"] for t in result["turned_off"]} == {"light.responsive_bulb"}
    stuck = [s for s in result["skipped"] if s.get("reason") == "state_unchanged_after_call"]
    assert len(stuck) == 1
    assert stuck[0]["entity_id"] == "light.stuck_bulb"
    assert "Stuck Bulb" in result["summary"]


@pytest.mark.asyncio
async def test_lights_on_summary_calls_out_offline_devices(monkeypatch) -> None:
    """Even a partial success surfaces the offline devices in the summary
    so the user knows what didn't actually happen."""
    light_states = _ha_states(
        ("light.live", "off", "Live"),
        ("light.dead", "off", "Dead"),
    )
    _patch_client(
        monkeypatch,
        light_states,
        post_state_overrides={"light.dead": "off"},
    )
    result = await lights_control.lights_on()
    assert result["ok"] is False
    assert "Live" in result["summary"]
    assert "Dead" in result["summary"]
    assert "didn't respond" in result["summary"]


# ── Whole-house safety guard (May 20 incident — router lost area
#    context across turns and lights_off() bare killed 22 devices) ──


@pytest.mark.asyncio
async def test_lights_off_refuses_whole_house_without_confirm(monkeypatch) -> None:
    """A bare lights_off() (no entity_ids, no area) must refuse when
    it would affect more than _WHOLE_HOUSE_THRESHOLD devices. Replays
    the exact May 20 scenario: 22 lights would have been killed."""
    # 22 lights all on, no area filter
    light_states = _ha_states(*[
        (f"light.bulb_{i}", "on", f"Bulb {i}") for i in range(22)
    ])
    calls = _patch_client(monkeypatch, light_states)
    result = await lights_control.lights_off()

    assert result["ok"] is False
    assert result["error"] == "whole_house_requires_confirmation"
    assert result["would_affect"] == 22
    assert result["turned_off"] == []
    # No HA service call should have fired.
    assert calls == []
    # Summary names a sample so the LLM can read it back to the user.
    assert "Bulb 0" in result["summary"]


@pytest.mark.asyncio
async def test_lights_off_with_area_bypasses_whole_house_guard(monkeypatch) -> None:
    """Passing an area always works — the guard only catches bare calls."""
    light_states = _ha_states(*[
        (f"light.bulb_{i}", "on", f"Bulb {i}") for i in range(22)
    ])
    calls = _patch_client(monkeypatch, light_states)
    # area="bulb" matches everything via friendly_name fuzzy match, but
    # the guard fires only when BOTH entity_ids and area are empty.
    result = await lights_control.lights_off(area="bulb")
    assert result["ok"] is True
    assert len(result["turned_off"]) == 22
    assert len(calls) == 22


@pytest.mark.asyncio
async def test_lights_off_with_confirm_all_bypasses_guard(monkeypatch) -> None:
    """Explicit confirm_all=True is the opt-in for the genuine
    'turn off every light' use case."""
    light_states = _ha_states(*[
        (f"light.bulb_{i}", "on", f"Bulb {i}") for i in range(22)
    ])
    calls = _patch_client(monkeypatch, light_states)
    result = await lights_control.lights_off(confirm_all=True)
    assert result["ok"] is True
    assert len(result["turned_off"]) == 22
    assert len(calls) == 22


@pytest.mark.asyncio
async def test_lights_off_bare_call_under_threshold_proceeds(monkeypatch) -> None:
    """Small homes (<= 5 lights) don't trigger the guard — 'turn off
    everything' is reasonable for a 3-bulb apartment."""
    light_states = _ha_states(*[
        (f"light.bulb_{i}", "on", f"Bulb {i}") for i in range(3)
    ])
    calls = _patch_client(monkeypatch, light_states)
    result = await lights_control.lights_off()
    assert result["ok"] is True
    assert len(result["turned_off"]) == 3
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_lights_on_refuses_whole_house_without_confirm(monkeypatch) -> None:
    """Mirror test for lights_on — same safety guard."""
    light_states = _ha_states(*[
        (f"light.bulb_{i}", "off", f"Bulb {i}") for i in range(22)
    ])
    calls = _patch_client(monkeypatch, light_states)
    result = await lights_control.lights_on()
    assert result["ok"] is False
    assert result["error"] == "whole_house_requires_confirmation"
    assert result["would_affect"] == 22
    assert calls == []
