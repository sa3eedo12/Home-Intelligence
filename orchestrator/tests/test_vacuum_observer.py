from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.vacuum_observer import VacuumObserver


class _CaptureVacuum(VacuumObserver):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, payload))


def _payload(state: str, ts: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "entity_id": "vacuum.robot_cleaner",
        "state": state,
        "ts": ts,
        "attributes": {"friendly_name": "Robot Vacuum", **(attrs or {})},
    }


@pytest.mark.asyncio
async def test_vacuum_emits_once_per_cleaning_cycle() -> None:
    observer = _CaptureVacuum()

    await observer.handle(_payload("docked", "2026-01-01T08:00:00+00:00"))
    await observer.handle(_payload("cleaning", "2026-01-01T08:10:00+00:00"))
    await observer.handle(_payload("returning", "2026-01-01T08:45:00+00:00"))
    await observer.handle(
        _payload("docked", "2026-01-01T08:50:00+00:00", {"cleaned_rooms": ["Kitchen"]})
    )
    await observer.handle(_payload("docked", "2026-01-01T08:51:00+00:00"))

    assert [item[0] for item in observer.emitted] == ["cleaning.completed"]
    assert observer.emitted[0][1]["rooms"] == ["Kitchen"]
    assert observer.emitted[0][1]["duration_seconds"] == 2400
