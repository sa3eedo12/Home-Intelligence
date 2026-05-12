from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import Observer
from .utils import domain_of, extract_state_change, normalized_state, parse_datetime

MAX_TRACKED_ENTITIES = 256


class SleepObserver(Observer):
    name = "sleep"
    subscribed_streams = ["events.home"]

    def __init__(
        self,
        *,
        bedroom_area: str | None = None,
        sleep_window: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        self.bedroom_area = bedroom_area or os.getenv("SLEEP_BEDROOM_AREA", "Bedroom")
        self.sleep_window = sleep_window or os.getenv("SLEEP_WINDOW", "22:00-02:00")
        self._now_fn = now_fn
        self._light_states: OrderedDict[str, str] = OrderedDict()
        self._tv_states: OrderedDict[str, str] = OrderedDict()
        self._likely_asleep = False

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None:
            return
        domain = domain_of(change.entity_id)
        if domain == "light" and _same_area(change.area, self.bedroom_area):
            _remember(self._light_states, change.entity_id, normalized_state(change.new_state))
        elif domain == "media_player" and _looks_like_master_tv(change):
            _remember(self._tv_states, change.entity_id, normalized_state(change.new_state))
        else:
            return

        signals = self._signals(change.ts)
        likely_asleep = all(
            [
                signals["bedroom_lights_off"],
                signals["tv_off"],
                _hour_in_window(signals["hour"], self.sleep_window),
            ]
        )
        if likely_asleep == self._likely_asleep:
            return
        self._likely_asleep = likely_asleep
        if likely_asleep:
            await self.emit_event(
                "sleep.likely_asleep",
                "Bedroom signals suggest everyone is likely asleep",
                {"detected_at": change.ts, "signals": signals},
            )
        else:
            await self.emit_event(
                "sleep.likely_awake",
                "Bedroom signals suggest someone is likely awake",
                {"detected_at": change.ts, "signals": signals},
            )

    def _signals(self, ts: str) -> dict[str, Any]:
        event_dt = parse_datetime(ts)
        if event_dt is None and self._now_fn is not None:
            event_dt = self._now_fn()
        if event_dt is None:
            event_dt = datetime.now().astimezone()
        local_dt = event_dt
        light_values = list(self._light_states.values())
        tv_values = list(self._tv_states.values())
        return {
            "bedroom_lights_off": bool(light_values)
            and all(state in {"off", "unavailable", "unknown"} for state in light_values),
            "tv_off": bool(tv_values)
            and all(
                state in {"off", "idle", "standby", "unavailable", "unknown"}
                for state in tv_values
            ),
            "hour": local_dt.hour + (local_dt.minute / 60),
        }


def _same_area(area: str | None, expected: str) -> bool:
    return str(area or "").strip().casefold() == expected.strip().casefold()


def _looks_like_master_tv(change) -> bool:
    haystack = f"{change.entity_id} {change.friendly_name} {change.area or ''}".casefold()
    return "tv" in haystack and ("master" in haystack or "bedroom" in haystack)


def _remember(states: OrderedDict[str, str], entity_id: str, state: str) -> None:
    if entity_id in states:
        states.move_to_end(entity_id)
    states[entity_id] = state
    if len(states) > MAX_TRACKED_ENTITIES:
        states.popitem(last=False)


def _hour_in_window(hour: float, window: str) -> bool:
    start_raw, _, end_raw = window.partition("-")
    start = _parse_hour(start_raw, 22.0)
    end = _parse_hour(end_raw, 2.0)
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _parse_hour(raw: str, default: float) -> float:
    try:
        parts = [int(part) for part in raw.strip().split(":", maxsplit=1)]
    except ValueError:
        return default
    if not parts:
        return default
    hours = max(0, min(parts[0], 23))
    minutes = max(0, min(parts[1] if len(parts) > 1 else 0, 59))
    return hours + (minutes / 60)


def build() -> SleepObserver:
    return SleepObserver()
