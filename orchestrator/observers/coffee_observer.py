from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import Observer
from .utils import (
    detect_brand,
    device_key_for,
    extract_state_change,
    is_idle_state,
    is_running_state,
    matches_appliance,
    parse_datetime,
    remember_bounded,
)

MAX_TRACKED_ENTITIES = 256
DEVICE_DEDUP_WINDOW = timedelta(minutes=10)


@dataclass
class _BrewState:
    phase: str = "idle"


class CoffeeObserver(Observer):
    name = "coffee"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _BrewState] = OrderedDict()
        self._recent_completions: OrderedDict[str, datetime] = OrderedDict()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None or not matches_appliance(change, "coffee"):
            return
        entry = remember_bounded(self._states, change.entity_id, _BrewState, MAX_TRACKED_ENTITIES)
        if is_running_state(change.new_state):
            entry.phase = "running"
            return
        if is_idle_state(change.new_state) and entry.phase == "running":
            entry.phase = "idle"
            if self._recently_completed(change.entity_id, change.ts):
                return
            self._mark_completed(change.entity_id, change.ts)
            brand = detect_brand(change.entity_id, change.attributes, change.friendly_name)
            await self.emit_event(
                "coffee.brewed",
                f"Coffee brewed by {change.friendly_name}",
                {
                    "entity_id": change.entity_id,
                    "brand": brand,
                    "brew_at": change.ts,
                },
            )

    def _recently_completed(self, entity_id: str, ts: str | None) -> bool:
        device = device_key_for(entity_id)
        last = self._recent_completions.get(device)
        if last is None:
            return False
        now = parse_datetime(ts) or datetime.now(UTC)
        return (now - last) < DEVICE_DEDUP_WINDOW

    def _mark_completed(self, entity_id: str, ts: str | None) -> None:
        device = device_key_for(entity_id)
        self._recent_completions[device] = parse_datetime(ts) or datetime.now(UTC)
        self._recent_completions.move_to_end(device)
        while len(self._recent_completions) > 64:
            self._recent_completions.popitem(last=False)


def build() -> CoffeeObserver:
    return CoffeeObserver()
