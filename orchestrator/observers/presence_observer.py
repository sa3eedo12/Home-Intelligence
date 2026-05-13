from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import Observer
from .utils import domain_of, extract_state_change, normalized_state, remember_bounded

MAX_TRACKED_ENTITIES = 256

# Substrings that indicate an entity_id/friendly_name is NOT a person, even
# though it lives in the device_tracker.* domain. HA pulls every WiFi-attached
# device into device_tracker.*, including hubs, routers, doorbells, smart
# appliances (Samsung registers its washer/dryer as device_trackers!), and
# audio gear. The user gets noise-storm notifications without this filter.
NON_PERSON_KEYWORDS: tuple[str, ...] = (
    "hub",
    "gateway",
    "router",
    "switch",
    "anker",
    "espressif",
    "raspberry",
    "doorbell",
    "ring",
    "samsung_washer",
    "samsung_dryer",
    "samsung_tv",
    "tv",
    "oled",
    "monitor",
    "playstation",
    "xbox",
    "nintendo",
    "speaker",
    "echo",
    "homepod",
    "chromecast",
    "appletv",
    "apple_tv",
    "apple-tv",
    "express",
    "unifi",
    "deebot",
    "roomba",
    "vacuum",
)


@dataclass
class _PresenceState:
    state: str | None = None


class PresenceObserver(Observer):
    name = "presence"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _PresenceState] = OrderedDict()
        # Optional opt-in allowlist via env var (comma-separated entity IDs).
        # When set, ONLY these entities trigger presence.changed events.
        # When empty, falls back to the heuristic NON_PERSON_KEYWORDS filter.
        self._allowlist: frozenset[str] = frozenset(
            e.strip()
            for e in os.environ.get("PRESENCE_ALLOWLIST", "").split(",")
            if e.strip()
        )

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None:
            return
        domain = domain_of(change.entity_id)
        if domain not in {"device_tracker", "person"}:
            return
        if not self._is_person(change.entity_id, change.friendly_name, domain):
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

    def _is_person(self, entity_id: str, friendly_name: str, domain: str) -> bool:
        # person.* domain entries are always considered people (HA spec).
        if domain == "person":
            return True
        # Explicit allowlist wins.
        if self._allowlist:
            return entity_id in self._allowlist
        # Heuristic: reject device_trackers whose name/entity_id contains a
        # known non-person keyword. This kills Aqara hubs, gateways, smart
        # appliances, TVs, speakers etc. that HA otherwise treats as people.
        haystack = (entity_id + " " + (friendly_name or "")).casefold()
        return not any(kw in haystack for kw in NON_PERSON_KEYWORDS)


def _presence_state(state: str | None) -> str | None:
    normalized = normalized_state(state)
    if normalized == "home":
        return "home"
    if normalized in {"not_home", "away", "offline"}:
        return "not_home"
    return None


def build() -> PresenceObserver:
    return PresenceObserver()
