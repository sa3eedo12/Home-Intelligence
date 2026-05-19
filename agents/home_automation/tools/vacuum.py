"""Vacuum (robot cleaner) control as first-class tools.

Closes proposal #49. Saeed has a Deebot exposed as ``vacuum.saeeds_deebot``
plus a constellation of ``sensor.saeeds_deebot_*`` attributes (battery,
area_cleaned, lifespans). Without a dedicated tool, requests like
"start the vacuum" or "is the Deebot charging?" fell through to chat.

Exposes:
- ``vacuum_status(entity_id=None)`` — state + battery + last clean
- ``vacuum_start(entity_id=None)`` — start a cleaning cycle
- ``vacuum_dock(entity_id=None)`` — send it back to the dock

Most homes only have one vacuum so ``entity_id`` is optional — the
tool picks the only ``vacuum.*`` entity when there's exactly one.
"""

from __future__ import annotations

from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.vacuum")


async def _list_vacuums() -> list[dict[str, Any]]:
    """Fetch all vacuum.* entities plus enriched attributes.

    Uses tojson for state/name fields to handle embedded special chars
    safely.
    """
    client = get_ha_client()
    template = (
        "[{% for s in states.vacuum if true %}"
        "{{ '{' }}"
        "\"entity_id\": {{ s.entity_id | tojson }}, "
        "\"name\": {{ (state_attr(s.entity_id, 'friendly_name') or s.entity_id) | tojson }}, "
        "\"area\": {{ (area_name(s.entity_id) or '') | tojson }}, "
        "\"state\": {{ s.state | tojson }}, "
        "\"battery_level\": {{ state_attr(s.entity_id, 'battery_level') or 'null' }}, "
        "\"fan_speed\": {{ (state_attr(s.entity_id, 'fan_speed') or '') | tojson }}, "
        "\"status\": {{ (state_attr(s.entity_id, 'status') or '') | tojson }}"
        "{{ '}' }}"
        "{% if not loop.last %},{% endif %}"
        "{% endfor %}]"
    )
    import json as _json

    rendered = await client.render_template(template)
    try:
        return _json.loads(rendered)
    except _json.JSONDecodeError:
        logger.warning("vacuum_list_bad_json", rendered=rendered[:200])
        return []


def _resolve(
    entries: list[dict[str, Any]], entity_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if entity_id:
        for e in entries:
            if e["entity_id"] == entity_id:
                return e, None
        return None, {
            "ok": False,
            "error": "no_vacuum_found",
            "entity_id": entity_id,
            "available": [e["entity_id"] for e in entries],
        }
    if len(entries) == 1:
        return entries[0], None
    if not entries:
        return None, {"ok": False, "error": "no_vacuums_in_home"}
    return None, {
        "ok": False,
        "error": "multiple_vacuums",
        "available": [
            {"entity_id": e["entity_id"], "name": e["name"]} for e in entries
        ],
        "hint": "Pass entity_id to disambiguate.",
    }


@tool("vacuum_status")
async def vacuum_status(entity_id: str | None = None) -> dict[str, Any]:
    """Report vacuum state, battery, and last/current cleaning status.

    Args:
        entity_id: Optional ``vacuum.*`` entity. Defaults to the only
            vacuum in the home if there's exactly one.

    Returns ``{ok, vacuums: [...]}`` when no entity_id is provided, or
    ``{ok, entity_id, name, state, battery_level, status, ...}`` for
    a single resolved vacuum. ``state`` is typically ``cleaning``,
    ``docked``, ``returning``, ``idle``, ``paused`` or ``unavailable``.
    """
    entries = await _list_vacuums()
    if entity_id is None and len(entries) != 1:
        return {"ok": True, "vacuums": entries}
    entry, err = _resolve(entries, entity_id)
    if err is not None:
        return err
    assert entry is not None
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "area": entry.get("area"),
        "state": entry.get("state"),
        "battery_level": entry.get("battery_level"),
        "fan_speed": entry.get("fan_speed"),
        "status": entry.get("status"),
    }


@tool("vacuum_start", side_effects=True)
async def vacuum_start(entity_id: str | None = None) -> dict[str, Any]:
    """Start a cleaning cycle on the vacuum.

    Returns ``{ok, entity_id, name, previous_state, message}``. When
    the vacuum is currently ``unavailable`` (offline, docked outside
    its zone, etc.) the call still returns ok=True because HA will
    queue the action — we surface the previous_state so callers can
    decide whether to warn the user.
    """
    entries = await _list_vacuums()
    entry, err = _resolve(entries, entity_id)
    if err is not None:
        return err
    assert entry is not None
    client = get_ha_client()
    await client.call_service(
        "vacuum", "start", {"entity_id": entry["entity_id"]}
    )
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "previous_state": entry.get("state"),
        "message": f"Starting {entry['name']}.",
    }


@tool("vacuum_dock", side_effects=True)
async def vacuum_dock(entity_id: str | None = None) -> dict[str, Any]:
    """Send the vacuum back to its dock.

    Returns same shape as ``vacuum_start``.
    """
    entries = await _list_vacuums()
    entry, err = _resolve(entries, entity_id)
    if err is not None:
        return err
    assert entry is not None
    client = get_ha_client()
    await client.call_service(
        "vacuum", "return_to_base", {"entity_id": entry["entity_id"]}
    )
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "previous_state": entry.get("state"),
        "message": f"Sending {entry['name']} back to dock.",
    }
