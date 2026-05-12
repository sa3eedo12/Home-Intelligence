from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.observers import Observer, ObserverRunner


class _FakeBus:
    def __init__(self) -> None:
        self.subscriptions: dict[str, Any] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.client = None

    async def subscribe(self, stream, handler, group=None):
        self.subscriptions[stream] = {"handler": handler, "group": group}
        await asyncio.Future()

    async def publish(self, stream, payload):
        self.published.append((stream, payload))
        return "1-0"


class _FakeStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record_event(self, **kwargs):
        self.events.append(kwargs)
        return {"ok": True, "event": kwargs}


class _RecorderObserver(Observer):
    name = "recorder"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict[str, Any]] = []

    async def handle(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


class _SystemObserver(_RecorderObserver):
    name = "system"
    subscribed_streams = ["events.system"]


@pytest.mark.asyncio
async def test_observer_runner_routes_by_stream() -> None:
    bus = _FakeBus()
    home = _RecorderObserver()
    system = _SystemObserver()
    runner = ObserverRunner(bus=bus, event_log_store=_FakeStore(), observers=[home, system])

    await runner.start()
    try:
        await bus.subscriptions["events.home"]["handler"]({"entity_id": "light.kitchen"})
        await bus.subscriptions["events.system"]["handler"]({"topic": "disk"})
    finally:
        await runner.stop()

    assert home.payloads == [{"entity_id": "light.kitchen"}]
    assert system.payloads == [{"topic": "disk"}]
    assert bus.subscriptions["events.home"]["group"].startswith("orchestrator:observers")


@pytest.mark.asyncio
async def test_observer_emit_event_records_and_publishes() -> None:
    bus = _FakeBus()
    store = _FakeStore()
    observer = _RecorderObserver()
    observer.bind(bus=bus, event_log_store=store)

    await observer.emit_event("kind.test", "Test summary", {"value": 1})

    assert store.events[0]["agent"] == "observer.recorder"
    assert store.events[0]["capability"] == "kind.test"
    assert [stream for stream, _payload in bus.published] == ["events.observed", "events.activity"]
    assert bus.published[1][1]["agent"] == "observer.recorder"
