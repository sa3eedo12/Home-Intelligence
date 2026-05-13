from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

import app as app_module


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, stream: str, payload: dict[str, Any]) -> None:
        self.published.append((stream, payload))


@pytest.mark.asyncio
async def test_startup_subscribes_to_system_and_home(monkeypatch) -> None:
    buses = []

    class _StartupBus:
        def __init__(self, redis_url: str) -> None:
            self.redis_url = redis_url
            self.subscriptions = []
            buses.append(self)

        async def connect(self) -> None:
            return None

        async def subscribe(self, stream, handler, group=None):  # noqa: ANN001
            self.subscriptions.append({"stream": stream, "handler": handler, "group": group})

    monkeypatch.setattr(app_module, "EventBus", _StartupBus)

    await app_module._startup()
    await asyncio.sleep(0)

    assert [item["stream"] for item in buses[0].subscriptions] == ["events.system", "events.home"]
    assert [item["group"] for item in buses[0].subscriptions] == [
        "personal_assistant:system",
        "personal_assistant:home",
    ]


@pytest.mark.asyncio
async def test_home_arrival_publishes_due_reminder_notification(monkeypatch) -> None:
    async def _due_reminders(limit: int = 3):
        assert limit == 3
        return [
            {"id": 1, "text": "Take medicine", "due_at": datetime.now(UTC), "status": "pending"}
        ]

    monkeypatch.setattr(app_module.core, "due_reminders", _due_reminders)
    bus = _FakeBus()

    await app_module._on_home_event(
        {
            "type": "state_changed",
            "entity_id": "person.saeed",
            "data": {"old_state": {"state": "not_home"}, "new_state": {"state": "home"}},
        },
        bus,
    )

    assert bus.published[0][0] == "notify.outbound"
    payload = bus.published[0][1]
    assert payload["topic"] == "reminders.presence"
    assert "Take medicine" in payload["text"]
    assert payload["agent"] == "personal_assistant"
