from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.sleep_observer import SleepObserver


class _CaptureSleep(SleepObserver):
    def __init__(self) -> None:
        super().__init__(bedroom_area="Bedroom")
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, payload))


def _light(state: str, ts: str) -> dict[str, Any]:
    return {
        "entity_id": "light.bedroom_lamp",
        "state": state,
        "ts": ts,
        "area": "Bedroom",
        "attributes": {"friendly_name": "Bedroom Lamp", "area": "Bedroom"},
    }


def _tv(state: str, ts: str) -> dict[str, Any]:
    return {
        "entity_id": "media_player.master_tv",
        "state": state,
        "ts": ts,
        "area": "Bedroom",
        "attributes": {"friendly_name": "Master TV", "area": "Bedroom"},
    }


@pytest.mark.asyncio
async def test_sleep_observer_transitions_without_flapping() -> None:
    observer = _CaptureSleep()

    await observer.handle(_light("off", "2026-01-01T23:00:00+00:00"))
    assert observer.emitted == []

    await observer.handle(_tv("off", "2026-01-01T23:01:00+00:00"))
    await observer.handle(_tv("off", "2026-01-01T23:02:00+00:00"))
    await observer.handle(_light("on", "2026-01-01T23:03:00+00:00"))
    await observer.handle(_light("off", "2026-01-01T23:04:00+00:00"))

    assert [item[0] for item in observer.emitted] == [
        "sleep.likely_asleep",
        "sleep.likely_awake",
        "sleep.likely_asleep",
    ]
    assert observer.emitted[0][1]["signals"]["bedroom_lights_off"] is True
    assert observer.emitted[0][1]["signals"]["tv_off"] is True
