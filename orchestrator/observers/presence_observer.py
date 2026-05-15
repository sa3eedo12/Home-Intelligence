from __future__ import annotations

import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import Observer
from .utils import domain_of, extract_state_change, normalized_state, remember_bounded

MAX_TRACKED_ENTITIES = 256
MEMBER_LINK_TTL_SECONDS = 60.0

# Substrings that indicate an entity is unambiguously a non-person device
# (a PC, laptop, desktop, etc.). These are HARD-BLOCKED — they never fire
# `presence.changed` regardless of whether they're in a member's tracker
# allowlist. This is stronger than the heuristic NON_PERSON_KEYWORDS list
# below because PCs and laptops genuinely DO move "home/away" as they go to
# sleep + WiFi flaps, but reporting that as a person event creates noisy
# downstream inferences ("👋 Welcome home, Saeed-PC"). If you really want
# Saeed's PC tracked, do it at the inference layer, not in presence.
HARD_NON_PERSON_SUBSTRINGS: tuple[str, ...] = (
    "_pc",
    "_laptop",
    "_macbook",
    "_imac",
    "_desktop",
    "_workstation",
    "judes_laptop",  # explicit legacy match for already-named entities
    "saeed_pc",
    "saeed-pc",
)

# Substrings that indicate an entity_id/friendly_name is NOT a person, even
# though it lives in the device_tracker.* domain. Used as a SECONDARY filter
# (only when no member-linked or env allowlist is configured) to suppress
# the obvious noise: hubs, gateways, appliances, TVs, etc.
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
        self._env_allowlist: frozenset[str] = frozenset(
            e.strip()
            for e in os.environ.get("PRESENCE_ALLOWLIST", "").split(",")
            if e.strip()
        )
        # Cache of household_member tracker links: entity_id → person name.
        # Populated by the observer ON DEMAND from app.state.pool, refreshed
        # when older than MEMBER_LINK_TTL_SECONDS. Means the user can edit
        # the household_members.attributes.tracker_entity_ids JSON in the DB
        # (or via the dashboard) and changes take effect within a minute,
        # no orchestrator restart required.
        self._member_links: dict[str, str] = {}
        # Per-member authoritative entity. When a member has a person.*
        # entity in their tracker list, that's the ONLY entity that fires
        # presence.changed events for them — individual device_trackers
        # become silent (still tracked for state consistency, but every
        # phone/PC/laptop transition stops spamming "Saeed is now home").
        # Without this, a 5-tracker setup like Saeed's emits a presence
        # event every time any one device flaps, producing bogus
        # welcome-home messages and masking real arrivals.
        self._authoritative_entity_for_member: dict[str, str] = {}
        self._member_links_ts: float = 0.0

    async def _refresh_member_links(self) -> None:
        """Pull tracker_entity_ids from every household_member row.

        Stored under attributes JSONB as either ``["device_tracker.x", ...]``
        or ``{"tracker_entity_ids": ["device_tracker.x", ...]}``.

        Also computes per-member authoritative entity: when a member has a
        ``person.*`` entity in their list, that becomes the SINGLE source
        of truth for their presence; other device_trackers stay tracked
        for state but stop firing emit_event.
        """
        pool = None
        if self.event_log_store is not None:
            pool = getattr(self.event_log_store, "pool", None)
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT name, attributes FROM household_members"
                )
        except Exception as exc:
            self.logger.warning("presence_member_link_query_failed", error=str(exc))
            return
        new_links: dict[str, str] = {}
        member_to_trackers: dict[str, list[str]] = {}
        for row in rows:
            attrs = row["attributes"] or {}
            if isinstance(attrs, str):
                try:
                    import json as _json

                    attrs = _json.loads(attrs)
                except Exception:
                    attrs = {}
            tracker_ids: list[str] = []
            raw = attrs.get("tracker_entity_ids") if isinstance(attrs, dict) else None
            if isinstance(raw, list):
                tracker_ids = [str(x) for x in raw if x]
            elif isinstance(raw, str):
                tracker_ids = [s.strip() for s in raw.split(",") if s.strip()]
            for eid in tracker_ids:
                new_links[eid] = row["name"]
            member_to_trackers[row["name"]] = tracker_ids
        # Compute per-member authoritative entity
        new_authority: dict[str, str] = {}
        for member_name, trackers in member_to_trackers.items():
            person_entities = [t for t in trackers if domain_of(t) == "person"]
            if person_entities:
                # Prefer the first person.* entity — there's normally just one
                new_authority[member_name] = person_entities[0]
        self._member_links = new_links
        self._authoritative_entity_for_member = new_authority
        self._member_links_ts = time.monotonic()

    async def _ensure_member_links_fresh(self) -> None:
        if time.monotonic() - self._member_links_ts > MEMBER_LINK_TTL_SECONDS:
            await self._refresh_member_links()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None:
            return
        domain = domain_of(change.entity_id)
        if domain not in {"device_tracker", "person"}:
            return
        await self._ensure_member_links_fresh()
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
        # Prefer the household_member.name when the entity is linked.
        person = self._member_links.get(change.entity_id) or change.friendly_name
        # Per-member authority gate: when the member has a person.* entity
        # linked, ONLY that entity is allowed to fire presence.changed.
        # Without this, every individual device_tracker.* transition (a
        # phone leaving WiFi, the saeed_pc going to sleep, etc.) emits
        # bogus "X is now home/not_home" events even though the
        # consolidated person.* state hasn't changed.
        authoritative = self._authoritative_entity_for_member.get(person)
        if authoritative and change.entity_id != authoritative:
            self.logger.debug(
                "presence_emit_suppressed_non_authoritative",
                person=person,
                entity_id=change.entity_id,
                authoritative=authoritative,
                new_state=new_state,
            )
            return
        await self.emit_event(
            "presence.changed",
            f"{person} is now {new_state}",
            {
                "person": person,
                "state": new_state,
                "entity_id": change.entity_id,
                "since": change.ts,
                "household_member_linked": change.entity_id in self._member_links,
            },
        )

    def _is_person(self, entity_id: str, friendly_name: str, domain: str) -> bool:
        # HARD BLOCK first — PCs, laptops, desktops, etc. are NEVER people,
        # no matter what the allowlist says. Their WiFi flaps as they sleep
        # generate enormous noise downstream (presence_returns full of
        # "Saeed-PC came home", LLM auto-inferences burning cycles).
        haystack = (entity_id + " " + (friendly_name or "")).casefold()
        if any(kw in haystack for kw in HARD_NON_PERSON_SUBSTRINGS):
            return False
        # person.* domain entries are always considered people (HA spec).
        if domain == "person":
            return True
        # Strongest signal: an explicit household_member link.
        if entity_id in self._member_links:
            return True
        # Env allowlist next.
        if self._env_allowlist:
            return entity_id in self._env_allowlist
        # If at least one member has linked at least one tracker, that means
        # the user has explicitly told us "these are the people" — fall back
        # to STRICT mode and only fire for those linked entities.
        if self._member_links:
            return False
        # Otherwise: heuristic safety net for users who haven't linked
        # anything. Filter out the obvious non-people keywords so we're not
        # spamming "Aqara_Hub_E1 is now home".
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
