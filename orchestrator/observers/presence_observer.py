from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import Observer
from .utils import domain_of, extract_state_change, normalized_state, remember_bounded

MAX_TRACKED_ENTITIES = 256


@dataclass
class _PresenceState:
    state: str | None = None


class PresenceObserver(Observer):
    name = "presence"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _PresenceState] = OrderedDict()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None or domain_of(change.entity_id) not in {"device_tracker", "person"}:
            return
        new_state = _presence_state(change.new_state)
        if new_state is None:
            return
        entry = remember_bounded(
            self._states,
            change.entity_id,
            _PresenceState,
            MAX_TRACKED_ENTITIES,
        )
        previous = entry.state or _presence_state(change.old_state)
        entry.state = new_state
        if previous is None or previous == new_state:
            return
        await self.emit_event(
            "presence.changed",
            f"{change.friendly_name} is now {new_state}",
            {
                "person": change.friendly_name,
                "state": new_state,
                "entity_id": change.entity_id,
                "since": change.ts,
            },
        )


def _presence_state(state: str | None) -> str | None:
    normalized = normalized_state(state)
    if normalized == "home":
        return "home"
    if normalized in {"not_home", "away", "offline"}:
        return "not_home"
    return None


def build() -> PresenceObserver:
    return PresenceObserver()
