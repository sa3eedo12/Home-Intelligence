from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import Observer
from .utils import (
    detect_brand,
    extract_state_change,
    is_idle_state,
    is_running_state,
    matches_appliance,
    remember_bounded,
)

MAX_TRACKED_ENTITIES = 256


@dataclass
class _BrewState:
    phase: str = "idle"


class CoffeeObserver(Observer):
    name = "coffee"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _BrewState] = OrderedDict()

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


def build() -> CoffeeObserver:
    return CoffeeObserver()
