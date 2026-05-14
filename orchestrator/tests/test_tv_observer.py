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


# ── _time_is_in_sleep_window: the past_bedtime regression family ─────────


def test_time_in_window_handles_midnight_crossing_window() -> None:
    """A typical late-bedtime window like 23:00 → 07:00 wraps midnight."""
    from orchestrator.observers.tv_observer import _time_is_in_sleep_window

    sleep, wake = time(23, 0), time(7, 0)
    assert _time_is_in_sleep_window(time(23, 30), sleep, wake) is True
    assert _time_is_in_sleep_window(time(2, 0), sleep, wake) is True
    assert _time_is_in_sleep_window(time(6, 59), sleep, wake) is True
    assert _time_is_in_sleep_window(time(7, 0), sleep, wake) is False
    assert _time_is_in_sleep_window(time(18, 42), sleep, wake) is False


def test_time_in_window_handles_after_midnight_bedtime() -> None:
    """REGRESSION: sleep_time=00:30, wake_time=09:00 must NOT count
    18:42 (six-and-a-bit hours BEFORE bedtime) as past-bedtime.

    Before the fix the observer treated any sleep_time < 18:00 as a
    'morning bedtime' and triggered immediately past the wall-clock,
    so the TV produced "past the usual bedtime" notifications all day.
    """
    from orchestrator.observers.tv_observer import _time_is_in_sleep_window

    sleep, wake = time(0, 30), time(9, 0)
    # Inside the window
    assert _time_is_in_sleep_window(time(0, 30), sleep, wake) is True
    assert _time_is_in_sleep_window(time(3, 0), sleep, wake) is True
    assert _time_is_in_sleep_window(time(8, 59), sleep, wake) is True
    # Outside the window — these are the false-positives that fired the bug
    assert _time_is_in_sleep_window(time(9, 0), sleep, wake) is False
    assert _time_is_in_sleep_window(time(12, 0), sleep, wake) is False
    assert _time_is_in_sleep_window(time(18, 42), sleep, wake) is False
    assert _time_is_in_sleep_window(time(23, 59), sleep, wake) is False


def test_time_in_window_same_sleep_and_wake_treated_as_no_window() -> None:
    """Defensive: a degenerate (sleep == wake) window matches nothing."""
    from orchestrator.observers.tv_observer import _time_is_in_sleep_window

    assert _time_is_in_sleep_window(time(0, 0), time(7, 0), time(7, 0)) is False
    assert _time_is_in_sleep_window(time(7, 0), time(7, 0), time(7, 0)) is False
    assert _time_is_in_sleep_window(time(23, 59), time(7, 0), time(7, 0)) is False


@pytest.mark.asyncio
async def test_tv_observer_does_not_fire_at_evening_when_bedtime_is_after_midnight() -> None:
    """End-to-end regression for the live bug: Saeed has sleep_time=00:30 /
    wake_time=09:00. At 18:42 on a TV that's been on for hours, the observer
    must NOT report 'past_bedtime' (someone home, evening, hours till sleep).
    """
    observer = _CaptureTv(
        max_on_hours=1,
        sleep_times=[(time(0, 30), time(9, 0))],
    )

    await observer.handle(_presence_payload("home", "2026-01-01T12:00:00+00:00"))
    await observer.handle(_tv_payload("playing", "2026-01-01T12:00:00+00:00"))
    # Tickle the observer at 18:42 local — was triggering past_bedtime before.
    await observer.handle(
        {
            "entity_id": "sensor.temperature",
            "state": "22",
            "ts": "2026-01-01T18:42:00+00:00",
        }
    )
    assert observer.emitted == []


@pytest.mark.asyncio
async def test_tv_observer_fires_past_bedtime_when_inside_after_midnight_window() -> None:
    """Mirror of the above: at 02:00 local, with sleep_time=00:30, the
    TV is genuinely past bedtime and the observer SHOULD emit."""
    observer = _CaptureTv(
        max_on_hours=1,
        sleep_times=[(time(0, 30), time(9, 0))],
    )

    await observer.handle(_presence_payload("home", "2026-01-01T22:00:00+00:00"))
    await observer.handle(_tv_payload("playing", "2026-01-01T22:00:00+00:00"))
    await observer.handle(_tv_payload("playing", "2026-01-02T02:00:00+00:00"))

    assert len(observer.emitted) == 1
    assert observer.emitted[0][2]["reason"] == "past_bedtime"


def test_normalize_sleep_windows_pairs_bare_times_with_default_wake() -> None:
    """Backwards-compat: the old shape ``sleep_times=[time(22, 0)]`` still
    works and pairs each bedtime with the default 07:00 wake time."""
    from orchestrator.observers.tv_observer import (
        DEFAULT_WAKE_TIME,
        _normalize_sleep_windows,
    )

    windows = _normalize_sleep_windows([time(22, 0), "23:30"])
    assert windows == [(time(22, 0), DEFAULT_WAKE_TIME), (time(23, 30), DEFAULT_WAKE_TIME)]
