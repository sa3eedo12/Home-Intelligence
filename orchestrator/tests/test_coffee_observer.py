from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.coffee_observer import CoffeeObserver


class _CaptureCoffee(CoffeeObserver):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, payload))


@pytest.mark.asyncio
async def test_coffee_brewed_emits_once() -> None:
    observer = _CaptureCoffee()
    base = {
        "entity_id": "switch.espresso_machine",
        "attributes": {"friendly_name": "Kitchen Espresso"},
    }

    await observer.handle({**base, "state": "idle", "ts": "2026-01-01T07:00:00+00:00"})
    await observer.handle({**base, "state": "running", "ts": "2026-01-01T07:01:00+00:00"})
    await observer.handle({**base, "state": "idle", "ts": "2026-01-01T07:03:00+00:00"})
    await observer.handle({**base, "state": "idle", "ts": "2026-01-01T07:04:00+00:00"})

    assert [item[0] for item in observer.emitted] == ["coffee.brewed"]
    assert observer.emitted[0][1]["entity_id"] == "switch.espresso_machine"
    assert observer.emitted[0][1]["brew_at"] == "2026-01-01T07:03:00+00:00"
