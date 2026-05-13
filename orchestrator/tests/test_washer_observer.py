from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.washer_observer import WasherObserver


class _CaptureWasher(WasherObserver):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, summary, payload))


def _payload(state: str, ts: str = "2026-01-01T10:00:00+00:00") -> dict[str, Any]:
    return {
        "entity_id": "sensor.lg_washer",
        "state": state,
        "ts": ts,
        "attributes": {"friendly_name": "LG Washer", "program": "Cottons"},
    }


@pytest.mark.asyncio
async def test_washer_emits_once_per_cycle() -> None:
    observer = _CaptureWasher()

    await observer.handle(_payload("idle", "2026-01-01T09:00:00+00:00"))
    await observer.handle(_payload("running", "2026-01-01T09:05:00+00:00"))
    await observer.handle(_payload("running", "2026-01-01T09:20:00+00:00"))
    await observer.handle(_payload("off", "2026-01-01T10:00:00+00:00"))
    await observer.handle(_payload("off", "2026-01-01T10:01:00+00:00"))

    assert [item[0] for item in observer.emitted] == ["appliance.cycle_completed"]
    payload = observer.emitted[0][2]
    assert payload["appliance"] == "washer"
    assert payload["entity_id"] == "sensor.lg_washer"
    assert payload["program"] == "Cottons"
    assert payload["duration_seconds"] == 3300


def _payload_for(entity_id: str, state: str, ts: str, friendly: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "ts": ts,
        "attributes": {"friendly_name": friendly},
    }


@pytest.mark.asyncio
async def test_washer_dedupes_multi_entity_devices_within_window() -> None:
    """HA exposes one washer as N entities (Power, Remote control, Bubble Soak).
    All flip running→idle when the cycle ends — observer should emit ONCE."""
    observer = _CaptureWasher()
    # Same physical device — entity_ids share the "washer" first slug
    entities = [
        ("sensor.washer_power", "Washer Power"),
        ("sensor.washer_remote_control", "Washer Remote control"),
        ("sensor.washer_bubble_soak", "Washer Bubble Soak"),
    ]
    # All start running at the same time
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "running", "2026-01-01T09:00:00+00:00", name))
    # All transition to idle within seconds of each other (same cycle ending)
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "off", "2026-01-01T09:30:00+00:00", name))

    # Exactly one notification, not three
    assert len(observer.emitted) == 1
    assert observer.emitted[0][0] == "appliance.cycle_completed"


@pytest.mark.asyncio
async def test_washer_emits_again_after_dedup_window() -> None:
    observer = _CaptureWasher()
    eid = "sensor.washer_power"
    # First cycle
    await observer.handle(_payload_for(eid, "running", "2026-01-01T09:00:00+00:00", "Washer"))
    await observer.handle(_payload_for(eid, "off", "2026-01-01T09:30:00+00:00", "Washer"))
    # Second cycle 11 minutes later (past 10-min dedup window)
    await observer.handle(_payload_for(eid, "running", "2026-01-01T09:35:00+00:00", "Washer"))
    await observer.handle(_payload_for(eid, "off", "2026-01-01T09:46:00+00:00", "Washer"))
    assert len(observer.emitted) == 2


@pytest.mark.asyncio
async def test_washer_does_not_dedup_across_different_devices() -> None:
    observer = _CaptureWasher()
    # Two distinct washers: "washer" and "dryer" — different first-slug device keys
    # (matches_appliance("washer") matches both because both have "wash" in friendly,
    # but the device_key heuristic separates them properly).
    entities = [
        ("sensor.basement_washer_power", "Basement Washer"),
        ("sensor.bathroom_washer_power", "Bathroom Washer"),
    ]
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "running", "2026-01-01T09:00:00+00:00", name))
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "off", "2026-01-01T09:30:00+00:00", name))
    # Different first-slug ("basement" vs "bathroom") → both fire
    assert len(observer.emitted) == 2
