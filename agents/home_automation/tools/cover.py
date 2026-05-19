"""Cover (curtain / blind / roller shade) control as first-class tools.

Closes proposal #47. Without these the router LLM had to compose a
generic ``call_service`` with the right ``cover.open_cover`` /
``cover.close_cover`` / ``cover.set_cover_position`` service plus
the matching entity id — too many degrees of freedom for the small
router model. Requests like "open the left curtain" silently fell
through to the chat tool.

This module exposes:
- ``cover_status(area=None)`` — list all covers + state/position
- ``cover_open(area=None, entity_id=None)`` — fully open
- ``cover_close(area=None, entity_id=None)`` — fully close
- ``cover_set_position(area, position)`` — 0-100 (0=closed, 100=open)

Area / friendly-name resolution mirrors ``climate.py`` — exact area
match wins, then substring fuzzy match. Without this the user must
know the entity id (``cover.curtain_3``) which they never will.
"""

from __future__ import annotations

from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.cover")

_OPEN_LIKE_STATES = {"open", "opening"}
_CLOSED_LIKE_STATES = {"closed", "closing"}


async def _list_covers_with_areas() -> list[dict[str, Any]]:
    """Fetch all cover.* entities annotated with their HA area name.

    Uses Jinja's ``tojson`` filter so state/name values with embedded
    newlines or quotes are escaped correctly — without it, entities
    whose state contained a literal newline (e.g. geocoded locations)
    crashed the JSON parse.
    """
    client = get_ha_client()
    template = (
        "[{% for s in states.cover if true %}"
        "{{ '{' }}"
        "\"entity_id\": {{ s.entity_id | tojson }}, "
        "\"name\": {{ (state_attr(s.entity_id, 'friendly_name') or s.entity_id) | tojson }}, "
        "\"area\": {{ (area_name(s.entity_id) or '') | tojson }}, "
        "\"state\": {{ s.state | tojson }}, "
        "\"position\": {{ state_attr(s.entity_id, 'current_position') or 'null' }}, "
        "\"device_class\": {{ (state_attr(s.entity_id, 'device_class') or '') | tojson }}"
        "{{ '}' }}"
        "{% if not loop.last %},{% endif %}"
        "{% endfor %}]"
    )
    import json as _json

    rendered = await client.render_template(template)
    try:
        return _json.loads(rendered)
    except _json.JSONDecodeError:
        logger.warning("cover_list_bad_json", rendered=rendered[:200])
        return []


def _match(
    entries: list[dict[str, Any]], needle_raw: str
) -> list[dict[str, Any]]:
    needle = needle_raw.strip().casefold()
    if not needle:
        return []
    exact_area = [e for e in entries if (e.get("area") or "").casefold() == needle]
    if exact_area:
        return exact_area
    exact_name = [
        e for e in entries if (e.get("name") or "").casefold() == needle
    ]
    if exact_name:
        return exact_name
    return [
        e
        for e in entries
        if needle in (e.get("area") or "").casefold()
        or needle in (e.get("name") or "").casefold()
        or needle in (e.get("entity_id") or "").casefold()
    ]


def _resolve_single(
    entries: list[dict[str, Any]],
    area: str | None,
    entity_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pick the single targeted cover. Returns (entry, error_dict)."""
    if area:
        matched = _match(entries, area)
        if len(matched) == 1:
            return matched[0], None
        if len(matched) > 1:
            return None, {
                "ok": False,
                "error": "ambiguous_area",
                "area": area,
                "candidates": [
                    {
                        "entity_id": e["entity_id"],
                        "name": e["name"],
                        "area": e.get("area"),
                        "state": e.get("state"),
                    }
                    for e in matched
                ],
                "hint": (
                    "Multiple covers matched; pass a more specific area or "
                    "an entity_id to disambiguate."
                ),
            }
    if entity_id:
        for e in entries:
            if e["entity_id"] == entity_id:
                return e, None
    return None, {
        "ok": False,
        "error": "no_cover_found",
        "area": area,
        "entity_id": entity_id,
        "available": sorted(
            {e.get("area") or e.get("name") or "Unassigned" for e in entries}
        ),
    }


@tool("cover_status")
async def cover_status(area: str | None = None) -> dict[str, Any]:
    """List covers (curtains, blinds, roller shades) and their state.

    Args:
        area: Optional area name (e.g. 'bedroom', 'left') for narrowing.
            When omitted returns all covers grouped under their area.

    Returns ``{ok, covers: [{entity_id, name, area, state, position,
    device_class}]}``. ``position`` is 0-100 when the cover supports it,
    null otherwise.
    """
    entries = await _list_covers_with_areas()
    if area:
        matched = _match(entries, area)
        if not matched:
            return {
                "ok": False,
                "error": "no_cover_in_area",
                "area": area,
                "available_areas": sorted(
                    {e.get("area") or "Unassigned" for e in entries}
                ),
            }
        return {"ok": True, "covers": matched}
    return {"ok": True, "covers": entries}


@tool("cover_open", side_effects=True)
async def cover_open(
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Fully open a cover (curtain / blind).

    Returns ``{ok, entity_id, name, area, previous_state, message}``.
    """
    entries = await _list_covers_with_areas()
    entry, err = _resolve_single(entries, area, entity_id)
    if err is not None:
        return err
    assert entry is not None
    client = get_ha_client()
    await client.call_service(
        "cover", "open_cover", {"entity_id": entry["entity_id"]}
    )
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "area": entry.get("area"),
        "previous_state": entry.get("state"),
        "message": f"Opening {entry['name']}.",
    }


@tool("cover_close", side_effects=True)
async def cover_close(
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Fully close a cover.

    Returns the same shape as ``cover_open``.
    """
    entries = await _list_covers_with_areas()
    entry, err = _resolve_single(entries, area, entity_id)
    if err is not None:
        return err
    assert entry is not None
    client = get_ha_client()
    await client.call_service(
        "cover", "close_cover", {"entity_id": entry["entity_id"]}
    )
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "area": entry.get("area"),
        "previous_state": entry.get("state"),
        "message": f"Closing {entry['name']}.",
    }


@tool("cover_set_position", side_effects=True)
async def cover_set_position(
    position: int,
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Set a cover's position to ``position`` percent (0=closed, 100=open).

    Out-of-range values are clamped. Use this for requests like
    "open the left curtain halfway" → position=50.

    Returns ``{ok, entity_id, name, area, requested_position,
    set_position, clamped, message}``.
    """
    try:
        requested = int(position)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_position", "position": position}
    clamped = False
    if requested < 0:
        requested, clamped = 0, True
    elif requested > 100:
        requested, clamped = 100, True

    entries = await _list_covers_with_areas()
    entry, err = _resolve_single(entries, area, entity_id)
    if err is not None:
        return err
    assert entry is not None

    client = get_ha_client()
    await client.call_service(
        "cover",
        "set_cover_position",
        {"entity_id": entry["entity_id"], "position": requested},
    )
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "area": entry.get("area"),
        "requested_position": int(position),
        "set_position": requested,
        "clamped": clamped,
        "message": f"Setting {entry['name']} to {requested}% open.",
    }
