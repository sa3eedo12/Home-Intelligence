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


@dataclass
class _CycleState:
    phase: str = "idle"
    started_at: str | None = None
    attrs_at_start: dict[str, Any] = field(default_factory=dict)


class WasherObserver(Observer):
    name = "washer"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _CycleState] = OrderedDict()
        self._recent_completions: OrderedDict[str, datetime] = OrderedDict()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        # canonical_only=True: a Samsung/LG washer exposes ~30 entities, only
        # sensor.<x>_machine_state authoritatively tracks "running vs stop".
        if change is None or not matches_appliance(change, "washer", canonical_only=True):
            return
        entry = remember_bounded(self._states, change.entity_id, _CycleState, MAX_TRACKED_ENTITIES)
        if is_running_state(change.new_state):
            if entry.phase != "running":
                entry.started_at = change.ts
                entry.attrs_at_start = dict(change.attributes)
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
                return
            await self._emit_completed(change, entry)
            self._mark_completed(change.entity_id, change.ts)
            entry.phase = "idle"
            entry.started_at = None
            entry.attrs_at_start = {}

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
        event_payload: dict[str, Any] = {
            "appliance": "washer",
            "entity_id": change.entity_id,
            "brand": brand,
            "program": program,
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
