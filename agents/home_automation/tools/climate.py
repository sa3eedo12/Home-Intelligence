"""Climate (thermostat / HVAC) control as a first-class tool.

Without this the router LLM had to compose a generic call_service call
with domain="climate", service="set_temperature", and the right entity
id — too many degrees of freedom for the small router model, so
thermostat requests like "reduce the bedroom temperature" silently fell
through to the conversational chat tool and got hallucinated failure
narratives instead of executing.

This module exposes:
- climate_status(area=None): list thermostats with current/target/mode
- climate_set_temperature(area=None, entity_id=None, temperature): set
- climate_set_mode(area=None, entity_id=None, mode): off|cool|heat|auto

Area matching uses HA's native area_name (set in HA per device) first,
then falls back to friendly_name substring. So 'bedroom' resolves to
whatever climate.* lives in the Bedroom area in HA.
"""

from __future__ import annotations

from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.climate")


async def _list_climate_with_areas() -> list[dict[str, Any]]:
    """Fetch all climate.* entities annotated with their HA area name."""
    client = get_ha_client()
    template = (
        "[{% for s in states.climate if true %}"
        "{{ '{' }}\"entity_id\": \"{{ s.entity_id }}\", "
        "\"name\": \"{{ state_attr(s.entity_id, 'friendly_name') or s.entity_id }}\", "
        "\"area\": \"{{ area_name(s.entity_id) or '' }}\", "
        "\"state\": \"{{ s.state }}\", "
        "\"current\": {{ state_attr(s.entity_id, 'current_temperature') or 'null' }}, "
        "\"target\": {{ state_attr(s.entity_id, 'temperature') or 'null' }}, "
        "\"min\": {{ state_attr(s.entity_id, 'min_temp') or 'null' }}, "
        "\"max\": {{ state_attr(s.entity_id, 'max_temp') or 'null' }}, "
        "\"hvac_modes\": {{ state_attr(s.entity_id, 'hvac_modes') | tojson if "
        "state_attr(s.entity_id, 'hvac_modes') else '[]' }}"
        "{{ '}' }}{% if not loop.last %},{% endif %}"
        "{% endfor %}]"
    )
    import json as _json
    rendered = await client.render_template(template)
    try:
        return _json.loads(rendered)
    except _json.JSONDecodeError:
        logger.warning("climate_list_bad_json", rendered=rendered[:200])
        return []


def _match_area(entries: list[dict[str, Any]], area: str) -> list[dict[str, Any]]:
    needle = area.strip().casefold()
    if not needle:
        return []
    exact = [e for e in entries if (e.get("area") or "").casefold() == needle]
    if exact:
        return exact
    return [
        e
        for e in entries
        if needle in (e.get("area") or "").casefold()
        or needle in (e.get("name") or "").casefold()
        or needle in (e.get("entity_id") or "").casefold()
    ]


@tool("climate_status")
async def climate_status(area: str | None = None) -> dict[str, Any]:
    """Report the state of one or more thermostats.

    Args:
        area: Optional area name (e.g. 'bedroom', 'living room'). When
            omitted, returns all thermostats grouped by area.

    Returns ``{ok, thermostats: [{entity_id, name, area, state,
    current, target, hvac_modes}]}``. The LLM should prefer this over
    get_entity_state for thermostat questions because it correctly
    resolves the area without needing the entity_id.
    """
    entries = await _list_climate_with_areas()
    if area:
        matched = _match_area(entries, area)
        if not matched:
            return {
                "ok": False,
                "error": "no_thermostat_in_area",
                "area": area,
                "available_areas": sorted({e.get("area") or "Unassigned" for e in entries}),
            }
        return {"ok": True, "thermostats": matched}
    return {"ok": True, "thermostats": entries}


@tool("climate_set_temperature", side_effects=True)
async def climate_set_temperature(
    temperature: float,
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Set the target temperature on a thermostat.

    Args:
        temperature: Target temperature in degrees (Celsius unless the
            thermostat is configured otherwise in HA). The tool clamps
            to the thermostat's min/max range.
        area: Area name to resolve a single thermostat. Wins over
            entity_id if both are provided. Use this for natural
            requests like 'bedroom', 'living room'.
        entity_id: Specific climate.* entity id, used when area is
            absent or ambiguous.

    Returns ``{ok, entity_id, name, area, old_target, new_target,
    clamped, message}``.
    """
    entries = await _list_climate_with_areas()
    target_entry: dict[str, Any] | None = None
    if area:
        matched = _match_area(entries, area)
        if len(matched) == 1:
            target_entry = matched[0]
        elif len(matched) > 1:
            return {
                "ok": False,
                "error": "ambiguous_area",
                "area": area,
                "candidates": [
                    {"entity_id": e["entity_id"], "name": e["name"], "area": e.get("area")}
                    for e in matched
                ],
                "hint": "Multiple thermostats matched; pass entity_id to disambiguate.",
            }
    if target_entry is None and entity_id:
        for e in entries:
            if e["entity_id"] == entity_id:
                target_entry = e
                break
    if target_entry is None:
        return {
            "ok": False,
            "error": "no_thermostat_found",
            "area": area,
            "entity_id": entity_id,
            "available_areas": sorted({e.get("area") or "Unassigned" for e in entries}),
        }

    raw_target = float(temperature)
    min_t = target_entry.get("min")
    max_t = target_entry.get("max")
    final_target = raw_target
    clamped = False
    if isinstance(min_t, (int, float)) and final_target < min_t:
        final_target = float(min_t)
        clamped = True
    if isinstance(max_t, (int, float)) and final_target > max_t:
        final_target = float(max_t)
        clamped = True

    client = get_ha_client()
    await client.call_service(
        "climate",
        "set_temperature",
        {"entity_id": target_entry["entity_id"], "temperature": final_target},
    )
    return {
        "ok": True,
        "entity_id": target_entry["entity_id"],
        "name": target_entry["name"],
        "area": target_entry.get("area"),
        "old_target": target_entry.get("target"),
        "new_target": final_target,
        "clamped": clamped,
        "message": (
            f"Set {target_entry['name']} ({target_entry.get('area') or 'unassigned area'}) "
            f"to {final_target}°."
        ),
    }


@tool("climate_set_mode", side_effects=True)
async def climate_set_mode(
    mode: str,
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Set the HVAC mode (off, cool, heat, auto, dry, fan_only) on a thermostat.

    Use ``climate_status`` first to find which modes the device supports —
    this tool validates against the entity's ``hvac_modes`` attribute
    and returns ``error: unsupported_mode`` with the supported list when
    the requested mode isn't available.
    """
    entries = await _list_climate_with_areas()
    target_entry: dict[str, Any] | None = None
    if area:
        matched = _match_area(entries, area)
        if len(matched) == 1:
            target_entry = matched[0]
        elif len(matched) > 1:
            return {
                "ok": False,
                "error": "ambiguous_area",
                "area": area,
                "candidates": [
                    {"entity_id": e["entity_id"], "name": e["name"], "area": e.get("area")}
                    for e in matched
                ],
            }
    if target_entry is None and entity_id:
        for e in entries:
            if e["entity_id"] == entity_id:
                target_entry = e
                break
    if target_entry is None:
        return {
            "ok": False,
            "error": "no_thermostat_found",
            "area": area,
            "entity_id": entity_id,
            "available_areas": sorted({e.get("area") or "Unassigned" for e in entries}),
        }

    requested = mode.strip().casefold()
    supported = [str(m).casefold() for m in target_entry.get("hvac_modes") or []]
    if supported and requested not in supported:
        return {
            "ok": False,
            "error": "unsupported_mode",
            "entity_id": target_entry["entity_id"],
            "requested": mode,
            "supported": target_entry.get("hvac_modes"),
        }

    client = get_ha_client()
    await client.call_service(
        "climate",
        "set_hvac_mode",
        {"entity_id": target_entry["entity_id"], "hvac_mode": requested},
    )
    return {
        "ok": True,
        "entity_id": target_entry["entity_id"],
        "name": target_entry["name"],
        "area": target_entry.get("area"),
        "mode": requested,
        "message": (
            f"Set {target_entry['name']} ({target_entry.get('area') or 'unassigned area'}) "
            f"to {requested} mode."
        ),
    }
