from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest

from orchestrator.observers.tv_observer import TvObserver


class _CaptureTv(TvObserver):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, summary, payload))


def _tv_payload(
    state: str,
    ts: str,
    entity_id: str = "media_player.living_room_tv",
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "ts": ts,
        "attributes": {"friendly_name": "Living Room TV"},
    }


def _presence_payload(state: str, ts: str = "2026-01-01T12:00:00+00:00") -> dict[str, Any]:
    return {
        "entity_id": "person.saeed",
        "state": state,
        "ts": ts,
        "attributes": {"friendly_name": "Saeed"},
    }


@pytest.mark.asyncio
async def test_tv_left_on_emits_after_threshold_when_nobody_home() -> None:
    observer = _CaptureTv(max_on_hours=6, cooldown=timedelta(hours=6))

    await observer.handle(_presence_payload("not_home", "2026-01-01T10:00:00+00:00"))
    await observer.handle(_tv_payload("on", "2026-01-01T10:00:00+00:00"))
    await observer.handle(
        {
            "entity_id": "sensor.temperature",
            "state": "22",
            "ts": "2026-01-01T15:59:00+00:00",
        }
    )
    assert observer.emitted == []

    await observer.handle(
        {
            "entity_id": "sensor.temperature",
            "state": "23",
            "ts": "2026-01-01T16:05:00+00:00",
        }
    )

    assert len(observer.emitted) == 1
    kind, _, payload = observer.emitted[0]
    assert kind == "entertainment.left_on"
    assert payload["entity_id"] == "media_player.living_room_tv"
    assert payload["friendly_name"] == "Living Room TV"
    assert payload["reason"] == "nobody_home"
    assert payload["on_since"] == "2026-01-01T10:00:00+00:00"
    assert payload["on_hours"] == 6.08


@pytest.mark.asyncio
async def test_tv_left_on_cooldown_dedupes_same_device() -> None:
    observer = _CaptureTv(max_on_hours=1, cooldown=timedelta(hours=2))

    await observer.handle(_presence_payload("not_home", "2026-01-01T10:00:00+00:00"))
    await observer.handle(_tv_payload("on", "2026-01-01T10:00:00+00:00"))
    await observer.handle(_tv_payload("on", "2026-01-01T11:01:00+00:00"))
    await observer.handle(_tv_payload("on", "2026-01-01T11:30:00+00:00"))

    assert len(observer.emitted) == 1

    await observer.handle(_tv_payload("on", "2026-01-01T13:02:00+00:00"))
    assert len(observer.emitted) == 2


@pytest.mark.asyncio
async def test_tv_left_on_requires_at_rest_condition() -> None:
    observer = _CaptureTv(max_on_hours=1, sleep_times=[])

    await observer.handle(_presence_payload("home", "2026-01-01T10:00:00+00:00"))
    await observer.handle(_tv_payload("on", "2026-01-01T10:00:00+00:00"))
    await observer.handle(_tv_payload("on", "2026-01-01T12:00:00+00:00"))
    assert observer.emitted == []

    await observer.handle(_presence_payload("not_home", "2026-01-01T12:01:00+00:00"))
    assert len(observer.emitted) == 1
    assert observer.emitted[0][2]["reason"] == "nobody_home"


@pytest.mark.asyncio
async def test_tv_left_on_uses_bedtime_when_someone_home() -> None:
    observer = _CaptureTv(max_on_hours=1, sleep_times=[time(22, 0)])

    await observer.handle(_presence_payload("home", "2026-01-01T21:00:00+00:00"))
    await observer.handle(_tv_payload("paused", "2026-01-01T21:00:00+00:00"))
    await observer.handle(_tv_payload("paused", "2026-01-01T22:05:00+00:00"))

    assert len(observer.emitted) == 1
    assert observer.emitted[0][2]["reason"] == "past_bedtime"


@pytest.mark.asyncio
async def test_tv_observer_tracks_switch_and_light_tv_entities() -> None:
    observer = _CaptureTv(max_on_hours=1, sleep_times=[time(22, 0)])

    await observer.handle(_tv_payload("on", "2026-01-01T21:00:00+00:00", "switch.den_tv_power"))
    await observer.handle(
        _tv_payload("on", "2026-01-01T21:00:00+00:00", "light.office_monitor_backlight")
    )
    await observer.handle(
        {
            "entity_id": "sensor.temperature",
            "state": "22",
            "ts": "2026-01-01T22:05:00+00:00",
        }
    )

    assert len(observer.emitted) == 2
    assert {item[2]["entity_id"] for item in observer.emitted} == {
        "switch.den_tv_power",
        "light.office_monitor_backlight",
    }
