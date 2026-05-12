"""Appliance activity introspection via Home Assistant.

Resolves a colloquial appliance name (e.g. "washer", "dryer", "dishwasher",
"oven") to the matching HA entities, then reads their current state plus
the most recent few state changes via /api/history.

This keeps generic raw highlights while adding brand-aware summaries for
common appliance integrations (Bosch HC, Miele, Samsung, LG). The orchestrator's
response humanizer can use these fields for answers like "Last cycle: Cottons
60°C, finished 14:23."
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

BRAND_ATTRIBUTE_MAP: dict[str, dict[str, Any]] = {
    "bosch": {
        "brand": "Bosch Home Connect",
        "prefixes": ("bosch_", "home_connect_"),
        "keywords": ("bosch", "home connect"),
        "attrs": (
            "program",
            "selected_program",
            "active_program",
            "remote_start",
            "remaining_program_time",
        ),
    },
    "miele": {
        "brand": "Miele",
        "prefixes": ("miele_",),
        "keywords": ("miele",),
        "attrs": ("state", "program_phase", "time_remaining", "door_state", "light"),
    },
    "samsung": {
        "brand": "Samsung SmartThings",
        "prefixes": ("samsung_", "smartthings_"),
        "keywords": ("samsung", "smartthings", "smart things"),
        "attrs": ("mode", "course", "washingTime", "cycle"),
    },
    "lg": {
        "brand": "LG ThinQ",
        "prefixes": ("lg_", "thinq_"),
        "keywords": ("lg", "thinq", "thin q"),
        "attrs": ("state", "course", "target_dryer_mode", "remain_time"),
    },
}

# Attribute keys that carry "what cycle / program / state was running"
_BASE_INTERESTING_ATTRS = (
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
INTERESTING_ATTRS = tuple(
    dict.fromkeys(
        [
            *_BASE_INTERESTING_ATTRS,
            *(attr for brand in BRAND_ATTRIBUTE_MAP.values() for attr in brand["attrs"]),
        ]
    )
)


def _matches_appliance(entity_id: str, name: str, needles: list[str]) -> bool:
    haystack = (entity_id + " " + name).lower()
    return any(re.search(re.escape(n), haystack) for n in needles)


def _detect_brand_key(entity_id: str, attrs: dict[str, Any]) -> str:
    object_id = entity_id.split(".", 1)[-1].casefold()
    attr_haystack = " ".join(
        str(attrs.get(key) or "")
        for key in (
            "brand",
            "manufacturer",
            "integration",
            "attribution",
            "model",
            "friendly_name",
        )
    ).casefold()
    for brand_key, config in BRAND_ATTRIBUTE_MAP.items():
        prefixes = config["prefixes"]
        keywords = config["keywords"]
        if any(object_id.startswith(prefix) for prefix in prefixes):
            return brand_key
        if any(keyword in attr_haystack for keyword in keywords):
            return brand_key
    return "generic"


def _brand_label(brand_key: str) -> str:
    if brand_key == "generic":
        return "generic"
    return str(BRAND_ATTRIBUTE_MAP[brand_key]["brand"])


def _brand_highlights(brand_key: str, attrs: dict[str, Any]) -> dict[str, Any]:
    if brand_key == "generic":
        keys = INTERESTING_ATTRS
    else:
        keys = BRAND_ATTRIBUTE_MAP[brand_key]["attrs"]
    return {key: attrs[key] for key in keys if key in attrs and attrs[key] not in (None, "")}


def _first_highlight(highlights: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any] | None:
    for key in keys:
        value = highlights.get(key)
        if value not in (None, ""):
            return key, value
    return None


def _format_remaining(value: Any) -> str:
    if isinstance(value, int | float):
        return f"{int(value)} minutes remaining"

    text = str(value).strip()
    time_match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
    if time_match:
        hours = int(time_match.group(1) or 0)
        minutes = int(time_match.group(2))
        seconds = int(time_match.group(3))
        total_minutes = (hours * 60) + minutes + (1 if seconds >= 30 else 0)
        if total_minutes <= 0:
            return "less than a minute remaining"
        unit = "minute" if total_minutes == 1 else "minutes"
        return f"{total_minutes} {unit} remaining"

    if text.isdigit():
        minutes = int(text)
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} remaining"

    if "remaining" in text.casefold():
        return text
    return f"{text} remaining"


def _friendly_summary(
    name: str, current_state: Any, brand_key: str, brand_highlights: dict[str, Any]
) -> str:
    parts: list[str] = []
    program = _first_highlight(
        brand_highlights,
        (
            "active_program",
            "selected_program",
            "program",
            "course",
            "cycle",
            "mode",
            "target_dryer_mode",
        ),
    )
    if program:
        key, value = program
        label = "mode" if key in {"mode", "target_dryer_mode"} else "program"
        parts.append(f"{value} {label}")

    phase = _first_highlight(brand_highlights, ("program_phase",))
    if phase:
        parts.append(f"{phase[1]} phase")

    remaining = _first_highlight(
        brand_highlights,
        (
            "remaining_program_time",
            "time_remaining",
            "washingTime",
            "remain_time",
            "remaining_time",
        ),
    )
    if remaining:
        parts.append(_format_remaining(remaining[1]))

    door = brand_highlights.get("door_state")
    if door not in (None, ""):
        parts.append(f"door {door}")

    light = brand_highlights.get("light")
    if light not in (None, ""):
        parts.append(f"light {light}")

    state = brand_highlights.get("state") or current_state
    if not parts and state not in (None, ""):
        parts.append(f"state {state}")

    if brand_key == "generic" and not parts:
        parts.append("no program details available")

    return f"{name} — {', '.join(str(part) for part in parts)}"


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
        brand_key = _detect_brand_key(s["entity_id"], attrs)
        brand_highlights = _brand_highlights(brand_key, attrs)
        name = attrs.get("friendly_name") or s["entity_id"]
        summarized.append(
            {
                "entity_id": s["entity_id"],
                "name": name,
                "brand": _brand_label(brand_key),
                "current_state": s.get("state"),
                "last_changed": s.get("last_changed"),
                "highlights": highlights,
                "brand_highlights": brand_highlights,
                "friendly_summary": _friendly_summary(
                    str(name), s.get("state"), brand_key, brand_highlights or highlights
                ),
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
