from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.presence_observer import PresenceObserver


class _CapturePresence(PresenceObserver):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, payload))


@pytest.mark.asyncio
async def test_presence_changed_emits_home_and_not_home() -> None:
    observer = _CapturePresence()
    base = {
        "entity_id": "device_tracker.saeed_phone",
        "attributes": {"friendly_name": "Saeed"},
    }

    await observer.handle(
        {**base, "old_state": "not_home", "state": "home", "ts": "2026-01-01T09:00:00+00:00"}
    )
    await observer.handle(
        {**base, "old_state": "home", "state": "home", "ts": "2026-01-01T09:01:00+00:00"}
    )
    await observer.handle(
        {**base, "old_state": "home", "state": "not_home", "ts": "2026-01-01T10:00:00+00:00"}
    )

    assert [item[0] for item in observer.emitted] == ["presence.changed", "presence.changed"]
    assert observer.emitted[0][1]["state"] == "home"
    assert observer.emitted[0][1]["person"] == "Saeed"
    assert observer.emitted[1][1]["state"] == "not_home"
