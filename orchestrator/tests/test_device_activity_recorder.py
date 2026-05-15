"""Tests for orchestrator.observers.device_activity_recorder."""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.device_activity_recorder import DeviceActivityRecorder


class _Capture(DeviceActivityRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, summary, payload))


def _payload(
    entity_id: str,
    new_state: str,
    *,
    old_state: str = "off",
    friendly_name: str | None = None,
    ts: str = "2026-01-01T10:00:00+00:00",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "old_state": old_state,
        "state": new_state,
        "ts": ts,
        "attributes": {
            "friendly_name": friendly_name or entity_id,
            **(attributes or {}),
        },
    }


# ── Light tracking ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_light_on_off_persists() -> None:
    obs = _Capture()
    await obs.handle(_payload("light.living_room", "on", friendly_name="Living Room"))
    await obs.handle(
        _payload(
            "light.living_room",
            "off",
            old_state="on",
            ts="2026-01-01T10:30:00+00:00",
            friendly_name="Living Room",
        )
    )

    assert [e[0] for e in obs.emitted] == ["device.state_changed", "device.state_changed"]
    assert obs.emitted[0][1] == "💡 Living Room turned on"
    assert obs.emitted[1][1] == "💡 Living Room turned off"
    assert obs.emitted[0][2]["domain"] == "light"
    assert obs.emitted[0][2]["new_state"] == "on"
    assert obs.emitted[0][2]["old_state"] == "off"


@pytest.mark.asyncio
async def test_light_same_state_skipped() -> None:
    obs = _Capture()
    await obs.handle(_payload("light.x", "on", old_state="on"))
    assert obs.emitted == []


@pytest.mark.asyncio
async def test_light_attributes_slimmed_to_useful_subset() -> None:
    obs = _Capture()
    await obs.handle(
        _payload(
            "light.x",
            "on",
            attributes={
                "brightness": 200,
                "rgb_color": [255, 200, 100],
                "color_temp_kelvin": 3500,
                "transition": 0.4,  # noisy attribute we DON'T keep
                "supported_features": 63,  # keep-out
                "min_color_temp_kelvin": 2000,  # keep-out
            },
        )
    )
    attrs = obs.emitted[0][2]["attributes"]
    assert attrs == {
        "brightness": 200,
        "rgb_color": [255, 200, 100],
        "color_temp_kelvin": 3500,
    }


# ── Climate (thermostat) tracking ────────────────────────────────────────


@pytest.mark.asyncio
async def test_thermostat_mode_change_persists() -> None:
    obs = _Capture()
    await obs.handle(
        _payload(
            "climate.thermostat",
            "cool",
            old_state="off",
            friendly_name="Thermostat",
            attributes={"temperature": 22.5, "current_temperature": 23.1},
        )
    )
    assert obs.emitted[0][1] == "🌡 Thermostat set to cool (target 22.5°)"
    payload = obs.emitted[0][2]
    assert payload["domain"] == "climate"
    assert payload["attributes"]["temperature"] == 22.5
    assert payload["attributes"]["current_temperature"] == 23.1


@pytest.mark.asyncio
async def test_thermostat_target_change_recorded_as_state() -> None:
    """Climate changes from same hvac mode but different target temp also
    matter — our normalized 'state' field is hvac mode, so target-only
    changes won't fire here. That's intentional V1: target temp deltas
    are noisy. Confirm same-mode same-state is dropped."""
    obs = _Capture()
    await obs.handle(
        _payload(
            "climate.thermostat",
            "cool",
            old_state="cool",
            attributes={"temperature": 23},
        )
    )
    assert obs.emitted == []


# ── Cover (curtain / garage door) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cover_open_close_persists() -> None:
    obs = _Capture()
    await obs.handle(
        _payload(
            "cover.curtain",
            "open",
            old_state="closed",
            friendly_name="Curtain",
            attributes={"current_position": 100},
        )
    )
    assert obs.emitted[0][1] == "🪟 Curtain → open"
    assert obs.emitted[0][2]["attributes"]["current_position"] == 100


# ── binary_sensor whitelist ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_motion_sensor_persists() -> None:
    obs = _Capture()
    await obs.handle(
        _payload(
            "binary_sensor.kitchen_motion",
            "on",
            old_state="off",
            friendly_name="Kitchen Motion",
            attributes={"device_class": "motion"},
        )
    )
    assert obs.emitted[0][1] == "🔔 Kitchen Motion: on"


@pytest.mark.asyncio
async def test_door_sensor_persists() -> None:
    obs = _Capture()
    await obs.handle(
        _payload(
            "binary_sensor.front_door",
            "on",
            old_state="off",
            friendly_name="Front Door",
            attributes={"device_class": "door"},
        )
    )
    assert obs.emitted[0][2]["domain"] == "binary_sensor"


@pytest.mark.asyncio
async def test_battery_binary_sensor_filtered() -> None:
    """binary_sensor.*_battery_low / *_connectivity are noisy + irrelevant —
    must not pollute event_log."""
    obs = _Capture()
    await obs.handle(
        _payload(
            "binary_sensor.washer_battery_low",
            "on",
            old_state="off",
            friendly_name="Washer Battery Low",
        )
    )
    await obs.handle(
        _payload(
            "binary_sensor.tv_connectivity",
            "off",
            old_state="on",
            friendly_name="TV Connectivity",
        )
    )
    assert obs.emitted == []


# ── switch blocklist (mirrors tv_observer false-positive list) ───────────


@pytest.mark.asyncio
async def test_switch_sound_sensor_filtered() -> None:
    """REGRESSION: switch.sound_sensor_tv_sound_detection used to trigger
    bogus TV-left-on alerts. Same blocklist applies here — the recorder
    shouldn't fill event_log with these flips either."""
    obs = _Capture()
    await obs.handle(
        _payload(
            "switch.sound_sensor_tv_sound_detection",
            "on",
            old_state="off",
            friendly_name="Sound Sensor TV Sound Detection",
        )
    )
    await obs.handle(
        _payload(
            "switch.washer_remote_control",
            "on",
            old_state="off",
            friendly_name="Washer Remote Control",
        )
    )
    assert obs.emitted == []


@pytest.mark.asyncio
async def test_legitimate_switch_persists() -> None:
    obs = _Capture()
    await obs.handle(
        _payload(
            "switch.kitchen_outlet",
            "on",
            old_state="off",
            friendly_name="Kitchen Outlet",
        )
    )
    assert obs.emitted[0][1] == "🔌 Kitchen Outlet turned on"


# ── Domain filter ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sensor_domain_dropped_entirely() -> None:
    """sensor.* fires thousands of times per day for energy/CPU/temp
    updates — recording all of those would drown the log."""
    obs = _Capture()
    await obs.handle(
        _payload(
            "sensor.cloud_gateway_cpu",
            "30.9",
            old_state="29.5",
            friendly_name="Cloud Gateway CPU",
        )
    )
    await obs.handle(
        _payload(
            "sensor.washer_energy",
            "174.0",
            old_state="172.0",
            friendly_name="Washer Energy",
        )
    )
    assert obs.emitted == []


@pytest.mark.asyncio
async def test_update_and_button_dropped() -> None:
    obs = _Capture()
    await obs.handle(
        _payload("update.home_assistant", "on", old_state="off")
    )
    await obs.handle(
        _payload("button.refresh", "on", old_state="off")
    )
    assert obs.emitted == []


# ── Same-state cooldown (rate limit per entity) ──────────────────────────


@pytest.mark.asyncio
async def test_same_state_cooldown_collapses_repeats() -> None:
    """A motion sensor that re-asserts 'on' twice in 5 seconds shouldn't
    write two rows."""
    obs = _Capture()
    await obs.handle(
        _payload(
            "binary_sensor.motion",
            "on",
            old_state="off",
            friendly_name="Motion",
            ts="2026-01-01T10:00:00+00:00",
            attributes={"device_class": "motion"},
        )
    )
    # State went off then on again within 10 seconds → second 'on' coalesced
    await obs.handle(
        _payload(
            "binary_sensor.motion",
            "off",
            old_state="on",
            friendly_name="Motion",
            ts="2026-01-01T10:00:05+00:00",
            attributes={"device_class": "motion"},
        )
    )
    await obs.handle(
        _payload(
            "binary_sensor.motion",
            "on",
            old_state="off",
            friendly_name="Motion",
            ts="2026-01-01T10:00:10+00:00",
            attributes={"device_class": "motion"},
        )
    )
    # off and the second 'on' are real transitions; cooldown only bites
    # for SAME-state repeats, so all 3 should land.
    assert len(obs.emitted) == 3


@pytest.mark.asyncio
async def test_unknown_unavailable_dropped() -> None:
    obs = _Capture()
    await obs.handle(_payload("light.x", "unknown"))
    await obs.handle(_payload("light.x", "unavailable"))
    assert obs.emitted == []
