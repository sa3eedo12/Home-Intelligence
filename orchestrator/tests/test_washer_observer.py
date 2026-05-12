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
