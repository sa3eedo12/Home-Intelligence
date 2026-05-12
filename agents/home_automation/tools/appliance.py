"""Appliance activity introspection via Home Assistant.

Resolves a colloquial appliance name (e.g. "washer", "dryer", "dishwasher",
"oven") to the matching HA entities, then reads their current state plus
the most recent few state changes via /api/history.

This is intentionally generic: it doesn't assume a specific HA integration
(Bosch HC, Miele, Samsung, etc.). The orchestrator's response humanizer
turns whatever attributes we surface into a friendly answer like
"Last cycle: Cottons 60°C, finished 14:23."
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.appliance")

# Synonyms map colloquial names to substrings we'll match against entity_ids
# and friendly names. Order matters: more specific first.
APPLIANCE_SYNONYMS: dict[str, list[str]] = {
    "washer": ["washing_machine", "washer", "laundry"],
    "dryer": ["tumble_dryer", "dryer"],
    "dishwasher": ["dishwasher"],
    "oven": ["oven"],
    "fridge": ["fridge", "refrigerator"],
    "freezer": ["freezer"],
    "coffee": ["coffee_machine", "coffee_maker", "espresso"],
    "vacuum": ["vacuum", "robot_cleaner"],
    "ac": ["climate.", "air_conditioner", "ac_"],
    "tv": ["media_player.", "tv"],
}

# Attribute keys that carry "what cycle / program / state was running"
INTERESTING_ATTRS = (
    "program",
    "selected_program",
    "active_program",
    "operation_state",
    "operation_mode",
    "remote_start",
    "remaining_time",
    "time_remaining",
    "estimated_finish_time",
    "finish_time",
    "finish_at",
    "started_at",
    "cycle",
    "wash_temperature",
    "spin_speed",
)


def _matches_appliance(entity_id: str, name: str, needles: list[str]) -> bool:
    haystack = (entity_id + " " + name).lower()
    return any(re.search(re.escape(n), haystack) for n in needles)


@tool("recent_appliance_activity")
async def recent_appliance_activity(
    appliance: str, hours: int = 24
) -> dict[str, Any]:
    """Return the recent activity for an appliance.

    Args:
        appliance: Colloquial name like "washer", "dryer", "dishwasher", "oven".
        hours: Lookback window for state-change history (default 24h).
    """
    client = get_ha_client()
    needles = APPLIANCE_SYNONYMS.get(appliance.lower(), [appliance.lower()])

    all_states = await client.list_states()
    matched: list[dict[str, Any]] = []
    for s in all_states:
        attrs = s.get("attributes", {}) or {}
        name = str(attrs.get("friendly_name") or s.get("entity_id", ""))
        if _matches_appliance(s.get("entity_id", ""), name, needles):
            matched.append(s)

    if not matched:
        return {
            "appliance": appliance,
            "found": False,
            "message": f"No HA entities matched '{appliance}'.",
        }

    # Pull a small window of history to surface recent state changes.
    entity_ids = [s["entity_id"] for s in matched][:8]
    try:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        history = await client.get_history_since(entity_ids, since)
    except Exception as exc:
        logger.warning("appliance_history_failed", error=str(exc))
        history = []

    # Distill each entity into the highlights the humanizer can rephrase.
    summarized: list[dict[str, Any]] = []
    for s in matched:
        attrs = s.get("attributes", {}) or {}
        highlights = {
            k: attrs[k] for k in INTERESTING_ATTRS if k in attrs and attrs[k] not in (None, "")
        }
        summarized.append(
            {
                "entity_id": s["entity_id"],
                "name": attrs.get("friendly_name") or s["entity_id"],
                "current_state": s.get("state"),
                "last_changed": s.get("last_changed"),
                "highlights": highlights,
            }
        )

    # Pull the most recent state transitions per entity from history.
    recent_changes: list[dict[str, Any]] = []
    for series in history:
        if not series:
            continue
        # series is a list of states for one entity_id, in chronological order.
        states_seen: list[dict[str, Any]] = []
        for entry in series[-8:]:
            states_seen.append(
                {
                    "state": entry.get("state"),
                    "ts": entry.get("last_changed"),
                }
            )
        recent_changes.append(
            {"entity_id": series[0].get("entity_id"), "history": states_seen}
        )

    return {
        "appliance": appliance,
        "found": True,
        "matched_count": len(matched),
        "entities": summarized,
        "recent_changes": recent_changes,
        "lookback_hours": hours,
    }
