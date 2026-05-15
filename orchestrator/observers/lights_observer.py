"""Lights observer — tracks the on/off state of every light.* entity.

Unlike the appliance/TV observers this one does NOT emit per-state-change
events (lights flip on/off dozens of times a day — that would drown the
event_log). Instead it maintains an in-memory state map of every light
that's currently on, exposed via ``snapshot()`` for callers like the
late_bedtime_check that need to ask "how many lights are on right now?"

Future versions can emit aggregate signals like ``lights.all_off`` when
the count drops to zero, but the V1 contract is just: keep the state.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from . import Observer
from .utils import (
    domain_of,
    extract_state_change,
    normalized_state,
    parse_datetime,
    remember_bounded,
)

MAX_TRACKED_ENTITIES = 512
ON_STATE = "on"
# Entities the user typically WOULDN'T consider "a light I want bedtime
# nudges about" — backlights on TVs/monitors etc. flap with the device
# state and create noise. The detection mirrors tv_observer's haystack.
NON_BEDTIME_KEYWORDS: tuple[str, ...] = (
    "tv",
    "monitor",
    "backlight",
    "screen",
    "display",
    "indicator",
    "led_strip",
    "status",
)


class _LightState:
    __slots__ = ("on", "friendly_name", "since")

    def __init__(self) -> None:
        self.on: bool = False
        self.friendly_name: str = ""
        self.since: datetime | None = None


class LightsObserver(Observer):
    name = "lights"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        self._states: OrderedDict[str, _LightState] = OrderedDict()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None:
            return
        if domain_of(change.entity_id) != "light":
            return
        if _looks_like_bedtime_irrelevant(change.entity_id, change.friendly_name):
            return
        state = remember_bounded(
            self._states, change.entity_id, _LightState, MAX_TRACKED_ENTITIES
        )
        normalized = normalized_state(change.new_state)
        state.friendly_name = change.friendly_name or change.entity_id
        was_on = state.on
        state.on = normalized == ON_STATE
        if state.on and not was_on:
            state.since = parse_datetime(change.ts) or datetime.now(UTC)
        elif not state.on:
            state.since = None

    def seed_from_ha_states(self, states: list[dict[str, Any]]) -> int:
        """Bootstrap the in-memory state map from a snapshot of HA's
        /api/states response. Without this, the observer was blind to
        any light that was already on at orchestrator boot — the
        bedtime nudge said '1 light on' when HA actually had 5 on.

        Returns the count of lights seeded (any state, not just 'on').
        Idempotent — re-seeding overwrites the entry.
        """
        seeded = 0
        for raw in states or []:
            if not isinstance(raw, dict):
                continue
            entity_id = str(raw.get("entity_id") or "")
            if not entity_id or domain_of(entity_id) != "light":
                continue
            attrs = raw.get("attributes") or {}
            if isinstance(attrs, dict):
                friendly = str(attrs.get("friendly_name") or entity_id)
            else:
                friendly = entity_id
            if _looks_like_bedtime_irrelevant(entity_id, friendly):
                continue
            normalized = normalized_state(raw.get("state"))
            if normalized in {None, "", "unknown", "unavailable"}:
                continue
            state = remember_bounded(
                self._states, entity_id, _LightState, MAX_TRACKED_ENTITIES
            )
            state.friendly_name = friendly
            state.on = normalized == ON_STATE
            if state.on:
                # We don't know precisely when it turned on; use last_changed
                # if HA provided it, else now.
                state.since = parse_datetime(raw.get("last_changed")) or datetime.now(UTC)
            else:
                state.since = None
            seeded += 1
        return seeded

    def snapshot(self) -> dict[str, Any]:
        """Return a count + list of currently-on lights for callers that
        want to ask 'how many lights are on right now?' Used by the
        bedtime check to compose the "5 lights still on" suggestion."""
        on_lights = [
            {
                "entity_id": eid,
                "friendly_name": state.friendly_name or eid,
                "on_since": state.since.isoformat() if state.since else None,
            }
            for eid, state in self._states.items()
            if state.on
        ]
        return {
            "count": len(on_lights),
            "lights": on_lights,
            "tracked_entities": len(self._states),
        }


def _looks_like_bedtime_irrelevant(entity_id: str, friendly_name: str | None) -> bool:
    haystack = (entity_id + " " + (friendly_name or "")).casefold()
    return any(kw in haystack for kw in NON_BEDTIME_KEYWORDS)


def build() -> LightsObserver:
    return LightsObserver()
