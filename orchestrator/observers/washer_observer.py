from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import Observer
from .utils import (
    detect_brand,
    device_key_for,
    extract_state_change,
    first_attr,
    is_finishing_state,
    is_idle_state,
    is_running_state,
    matches_appliance,
    parse_datetime,
    remember_bounded,
    seconds_between,
)

MAX_TRACKED_ENTITIES = 256
DEVICE_DEDUP_WINDOW = timedelta(minutes=10)
# Device-name-relative entity_id suffix that exposes the WASHER cycle name
# (e.g. "Colors", "Delicates", "Bedding") on Samsung/LG SmartThings
# integrations. Tracked separately from the canonical machine_state so
# we can attach the precise cycle label to cycle_completed events
# instead of guessing from duration.
CYCLE_NAME_SUFFIX = "_cycle"


@dataclass
class _CycleState:
    phase: str = "idle"
    started_at: str | None = None
    attrs_at_start: dict[str, Any] = field(default_factory=dict)
    # Last observed value of sensor.<device>_cycle (e.g. "Colors").
    # Captured opportunistically — when the cycle name changes mid-run
    # this gets updated, and the most recent value at completion time
    # is the most accurate label we can offer the inference layer.
    last_cycle_name: str | None = None


class WasherObserver(Observer):
    name = "washer"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _CycleState] = OrderedDict()
        self._recent_completions: OrderedDict[str, datetime] = OrderedDict()
        # device_key → last observed cycle name. Survives across machine
        # state transitions so a name set during "stop" still applies if
        # the user starts the cycle after.
        self._cycle_names: OrderedDict[str, str] = OrderedDict()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None:
            return
        # Cycle-name sensor: track the value but don't drive cycle phases.
        if (
            matches_appliance(change, "washer", canonical_only=False)
            and change.entity_id.endswith(CYCLE_NAME_SUFFIX)
            and change.new_state
            and change.new_state not in {"none", "unknown", "unavailable", ""}
        ):
            device = device_key_for(change.entity_id)
            self._cycle_names[device] = str(change.new_state)
            self._cycle_names.move_to_end(device)
            while len(self._cycle_names) > MAX_TRACKED_ENTITIES:
                self._cycle_names.popitem(last=False)
            # The cycle-name sensor is NOT a canonical state entity so
            # the rest of this handler doesn't need to run for it.
            return
        # canonical_only=True: a Samsung/LG washer exposes ~30 entities, only
        # sensor.<x>_machine_state authoritatively tracks "running vs stop".
        if not matches_appliance(change, "washer", canonical_only=True):
            return
        entry = remember_bounded(self._states, change.entity_id, _CycleState, MAX_TRACKED_ENTITIES)
        if is_running_state(change.new_state):
            if entry.phase != "running":
                entry.started_at = change.ts
                entry.attrs_at_start = dict(change.attributes)
                # Snapshot the cycle name we know about for THIS device.
                entry.last_cycle_name = self._cycle_names.get(device_key_for(change.entity_id))
            entry.phase = "running"
            return
        if is_finishing_state(change.new_state) and entry.phase == "running":
            entry.phase = "finishing"
            return
        if is_idle_state(change.new_state) and entry.phase in {"running", "finishing"}:
            if self._recently_completed(change.entity_id, change.ts):
                entry.phase = "idle"
                entry.started_at = None
                entry.attrs_at_start = {}
                entry.last_cycle_name = None
                return
            await self._emit_completed(change, entry)
            self._mark_completed(change.entity_id, change.ts)
            entry.phase = "idle"
            entry.started_at = None
            entry.attrs_at_start = {}
            entry.last_cycle_name = None

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
        # bound memory: keep at most 64 device cooldowns
        while len(self._recent_completions) > 64:
            self._recent_completions.popitem(last=False)

    async def _emit_completed(self, change, entry: _CycleState) -> None:
        details = await self._recent_details(change.entity_id)
        detail_attrs = details.get("brand_highlights") or details.get("highlights") or {}
        attrs_at_finish = {**change.attributes}
        brand = str(details.get("brand") or detect_brand(change.entity_id, attrs_at_finish))
        program = first_attr(attrs_at_finish) or first_attr(detail_attrs)
        # Prefer the cycle-name sensor (e.g. "Colors", "Bedding") over the
        # heuristic 'program' attribute. The HA Samsung integration exposes
        # the user-facing cycle label via sensor.<x>_cycle which is set by
        # the appliance itself — far more accurate than guessing from
        # duration. Fall back to the heuristic when no cycle sensor is wired.
        device = device_key_for(change.entity_id)
        cycle_name = entry.last_cycle_name or self._cycle_names.get(device)
        event_payload: dict[str, Any] = {
            "appliance": "washer",
            "entity_id": change.entity_id,
            "brand": brand,
            "program": cycle_name or program,
            "cycle_name": cycle_name,
            "started_at": entry.started_at,
            "ended_at": change.ts,
            "duration_seconds": seconds_between(entry.started_at, change.ts),
            "attributes_at_finish": attrs_at_finish,
        }
        await self.emit_event(
            "appliance.cycle_completed",
            f"Washer cycle completed for {change.friendly_name}",
            event_payload,
        )

    async def _recent_details(self, entity_id: str) -> dict[str, Any]:
        result = await self.dispatch_capability(
            "home_automation",
            "recent_appliance_activity",
            {"appliance": "washer", "hours": 6},
        )
        for item in result.get("entities") or []:
            if isinstance(item, dict) and item.get("entity_id") == entity_id:
                return item
        return {}


def build() -> WasherObserver:
    return WasherObserver()
