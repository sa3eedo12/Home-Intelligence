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
        # The observer now only matches canonical operation state entities
        # (sensor.*_machine_state etc.) — not the dozens of HA sub-entities
        # that share the appliance's name (sensor.washer_power, etc.). See
        # observers/utils.py:is_canonical_state_entity.
        "entity_id": "sensor.lg_washer_machine_state",
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
    assert payload["entity_id"] == "sensor.lg_washer_machine_state"
    assert payload["program"] == "Cottons"
    assert payload["duration_seconds"] == 3300


@pytest.mark.asyncio
async def test_samsung_washer_stop_state_completes_cycle() -> None:
    """Regression: Samsung's washer reports 'stop' between cycles, not 'idle'/'off'.
    The observer must treat 'stop' as a cycle-end transition."""
    observer = _CaptureWasher()
    eid = "sensor.washer_machine_state"
    fname = "Washer machine state"
    await observer.handle({
        "entity_id": eid, "state": "stop", "ts": "2026-01-01T09:00:00+00:00",
        "attributes": {"friendly_name": fname},
    })
    await observer.handle({
        "entity_id": eid, "state": "run", "ts": "2026-01-01T09:05:00+00:00",
        "attributes": {"friendly_name": fname},
    })
    await observer.handle({
        "entity_id": eid, "state": "stop", "ts": "2026-01-01T09:35:00+00:00",
        "attributes": {"friendly_name": fname},
    })
    assert len(observer.emitted) == 1
    assert observer.emitted[0][0] == "appliance.cycle_completed"


@pytest.mark.asyncio
async def test_washer_ignores_non_canonical_sub_entities() -> None:
    """Sub-entities like sensor.washer_power, switch.washer_bubble_soak,
    binary_sensor.washer_remote_control should NOT trigger cycle events even
    though they contain 'washer' in their names — they're not authoritative
    for cycle state."""
    observer = _CaptureWasher()
    for entity in [
        "sensor.washer_power",
        "switch.washer_bubble_soak",
        "binary_sensor.washer_remote_control",
        "select.washer_water_temperature",
        "number.washer_rinse_cycles",
    ]:
        await observer.handle({
            "entity_id": entity, "state": "running", "ts": "2026-01-01T09:00:00+00:00",
            "attributes": {"friendly_name": entity.split(".")[1].replace("_", " ").title()},
        })
        await observer.handle({
            "entity_id": entity, "state": "off", "ts": "2026-01-01T09:30:00+00:00",
            "attributes": {"friendly_name": entity.split(".")[1].replace("_", " ").title()},
        })
    assert observer.emitted == []


def _payload_for(entity_id: str, state: str, ts: str, friendly: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "ts": ts,
        "attributes": {"friendly_name": friendly},
    }


@pytest.mark.asyncio
async def test_washer_dedupes_multi_state_entities_within_window() -> None:
    """Even after the canonical-only matcher narrows down to *_state entities,
    a single washer can still expose multiple canonical state entities
    (machine_state, job_state, operation_state). The cooldown dedup ensures we
    only emit one cycle event per device, regardless of which canonical entity
    fires first."""
    observer = _CaptureWasher()
    entities = [
        ("sensor.washer_machine_state", "Washer machine state"),
        ("sensor.washer_job_state", "Washer job state"),
        ("sensor.washer_operation_state", "Washer operation state"),
    ]
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "run", "2026-01-01T09:00:00+00:00", name))
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "stop", "2026-01-01T09:30:00+00:00", name))

    assert len(observer.emitted) == 1
    assert observer.emitted[0][0] == "appliance.cycle_completed"


@pytest.mark.asyncio
async def test_washer_emits_again_after_dedup_window() -> None:
    observer = _CaptureWasher()
    eid = "sensor.washer_machine_state"
    # First cycle
    await observer.handle(_payload_for(eid, "run", "2026-01-01T09:00:00+00:00", "Washer"))
    await observer.handle(_payload_for(eid, "stop", "2026-01-01T09:30:00+00:00", "Washer"))
    # Second cycle 11 minutes later (past 10-min dedup window)
    await observer.handle(_payload_for(eid, "run", "2026-01-01T09:35:00+00:00", "Washer"))
    await observer.handle(_payload_for(eid, "stop", "2026-01-01T09:46:00+00:00", "Washer"))
    assert len(observer.emitted) == 2


@pytest.mark.asyncio
async def test_washer_does_not_dedup_across_different_devices() -> None:
    observer = _CaptureWasher()
    # Two distinct washers in different rooms — different first-slug device keys.
    entities = [
        ("sensor.basement_washer_machine_state", "Basement Washer"),
        ("sensor.bathroom_washer_machine_state", "Bathroom Washer"),
    ]
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "run", "2026-01-01T09:00:00+00:00", name))
    for eid, name in entities:
        await observer.handle(_payload_for(eid, "stop", "2026-01-01T09:30:00+00:00", name))
    assert len(observer.emitted) == 2


# ── Cycle-name capture from sensor.<x>_cycle ─────────────────────────────


@pytest.mark.asyncio
async def test_washer_observer_captures_cycle_name_into_payload() -> None:
    """User wired sensor.washer_cycle in HA which exposes the actual
    cycle name (e.g. 'Colors', 'Bedding'). The observer should remember
    it and attach it to the cycle_completed payload so the inference
    layer doesn't have to guess from duration."""
    observer = _CaptureWasher()

    # The HA sensor.washer_cycle goes from idle → "Colors" before the
    # cycle starts running.
    await observer.handle({
        "entity_id": "sensor.washer_cycle",
        "old_state": "none",
        "state": "Colors",
        "ts": "2026-05-15T10:00:00+00:00",
        "attributes": {"friendly_name": "Washer Cycle"},
    })
    # Then the canonical machine_state goes to running.
    await observer.handle({
        "entity_id": "sensor.washer_machine_state",
        "old_state": "stop",
        "state": "run",
        "ts": "2026-05-15T10:00:30+00:00",
        "attributes": {"friendly_name": "Washer Machine state"},
    })
    # Cycle completes.
    await observer.handle({
        "entity_id": "sensor.washer_machine_state",
        "old_state": "run",
        "state": "stop",
        "ts": "2026-05-15T11:30:00+00:00",
        "attributes": {"friendly_name": "Washer Machine state"},
    })

    # The cycle_completed event should carry cycle_name AND have it in
    # the program field (program=cycle_name when both supplied) so the
    # inference layer prefers it.
    completions = [e for e in observer.emitted if e[0] == "appliance.cycle_completed"]
    assert len(completions) == 1
    payload = completions[0][2]
    assert payload["cycle_name"] == "Colors"
    assert payload["program"] == "Colors"


@pytest.mark.asyncio
async def test_washer_observer_ignores_garbage_cycle_states() -> None:
    """sensor.<x>_cycle = 'none' / 'unknown' / 'unavailable' must not
    overwrite a previously valid name."""
    observer = _CaptureWasher()

    await observer.handle({
        "entity_id": "sensor.washer_cycle",
        "old_state": "none",
        "state": "Bedding",
        "ts": "2026-05-15T10:00:00+00:00",
        "attributes": {"friendly_name": "Washer Cycle"},
    })
    await observer.handle({
        "entity_id": "sensor.washer_cycle",
        "old_state": "Bedding",
        "state": "unknown",
        "ts": "2026-05-15T10:00:01+00:00",
        "attributes": {"friendly_name": "Washer Cycle"},
    })
    await observer.handle({
        "entity_id": "sensor.washer_machine_state",
        "old_state": "stop",
        "state": "run",
        "ts": "2026-05-15T10:00:30+00:00",
        "attributes": {"friendly_name": "Washer Machine state"},
    })
    await observer.handle({
        "entity_id": "sensor.washer_machine_state",
        "old_state": "run",
        "state": "stop",
        "ts": "2026-05-15T11:30:00+00:00",
        "attributes": {"friendly_name": "Washer Machine state"},
    })
    payload = observer.emitted[-1][2]
    assert payload["cycle_name"] == "Bedding"
