from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import Observer
from .utils import (
    device_key_for,
    extract_state_change,
    is_finishing_state,
    is_idle_state,
    is_running_state,
    matches_appliance,
    normalized_state,
    parse_datetime,
    remember_bounded,
    seconds_between,
)

MAX_TRACKED_ENTITIES = 256
DEVICE_DEDUP_WINDOW = timedelta(minutes=10)
VACUUM_IDLE_STATES = {"docked", "idle", "off", "standby"}
VACUUM_RUNNING_STATES = {"cleaning", "on", "running"}
VACUUM_FINISHING_STATES = {"returning", "returning_home", "finished", "finishing"}


@dataclass
class _CycleState:
    phase: str = "idle"
    started_at: str | None = None


class VacuumObserver(Observer):
    name = "vacuum"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _CycleState] = OrderedDict()
        self._recent_completions: OrderedDict[str, datetime] = OrderedDict()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None or not matches_appliance(change, "vacuum"):
            return
        state = normalized_state(change.new_state)
        entry = remember_bounded(self._states, change.entity_id, _CycleState, MAX_TRACKED_ENTITIES)
        if state in VACUUM_RUNNING_STATES or is_running_state(change.new_state):
            if entry.phase != "running":
                entry.started_at = change.ts
            entry.phase = "running"
            return
        if state in VACUUM_FINISHING_STATES or is_finishing_state(change.new_state):
            if entry.phase == "running":
                entry.phase = "finishing"
            return
        if (state in VACUUM_IDLE_STATES or is_idle_state(change.new_state)) and entry.phase in {
            "running",
            "finishing",
        }:
            if not self._recently_completed(change.entity_id, change.ts):
                await self._emit_completed(change, entry)
                self._mark_completed(change.entity_id, change.ts)
            entry.phase = "idle"
            entry.started_at = None

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

    async def _emit_completed(self, change, entry: _CycleState) -> None:
        event_payload: dict[str, Any] = {
            "appliance": "vacuum",
            "entity_id": change.entity_id,
            "started_at": entry.started_at,
            "ended_at": change.ts,
            "duration_seconds": seconds_between(entry.started_at, change.ts),
        }
        rooms = _rooms_from_attrs(change.attributes)
        if rooms:
            event_payload["rooms"] = rooms
        await self.emit_event(
            "cleaning.completed",
            f"Vacuum cleaning completed for {change.friendly_name}",
            event_payload,
        )


def _rooms_from_attrs(attrs: dict[str, Any]) -> list[str]:
    for key in ("cleaned_rooms", "rooms", "current_room"):
        value = attrs.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    return []


def build() -> VacuumObserver:
    return VacuumObserver()
