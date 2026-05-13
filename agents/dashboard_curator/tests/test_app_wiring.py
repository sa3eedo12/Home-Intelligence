from __future__ import annotations

import asyncio

import pytest

import app as app_module


@pytest.mark.asyncio
async def test_startup_subscribes_to_activity_stream(monkeypatch) -> None:
    buses = []

    class _FakeBus:
        def __init__(self, redis_url: str) -> None:
            self.redis_url = redis_url
            self.subscriptions = []
            buses.append(self)

        async def connect(self) -> None:
            return None

        async def subscribe(self, stream, handler, group=None):  # noqa: ANN001
            self.subscriptions.append({"stream": stream, "handler": handler, "group": group})

    monkeypatch.setattr(app_module, "EventBus", _FakeBus)

    await app_module._startup()
    await asyncio.sleep(0)

    assert buses
    assert buses[0].subscriptions == [
        {
            "stream": "events.activity",
            "handler": app_module._on_activity_event,
            "group": "dashboard_curator:activity",
        }
    ]


@pytest.mark.asyncio
async def test_activity_event_handler_debounces_summarization(monkeypatch) -> None:
    app_module._activity_summary_task = None
    app_module._last_activity_summary_at = 0.0
    monkeypatch.setenv("DASHBOARD_ACTIVITY_DEBOUNCE_SECONDS", "0")

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _fake_summarize_activity(window_minutes: int = 15):
        calls.append(window_minutes)
        started.set()
        await release.wait()
        return {"stats": {"total_events": 1}}

    monkeypatch.setattr(app_module.core, "summarize_activity", _fake_summarize_activity)

    await app_module._on_activity_event({"agent": "home_automation"})
    task = app_module._activity_summary_task
    assert task is not None
    await started.wait()

    await app_module._on_activity_event({"agent": "system_health"})
    assert app_module._activity_summary_task is task

    release.set()
    await task
    assert calls == [15]
