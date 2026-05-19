"""Lock (door / vehicle) control as first-class tools.

Closes proposal #48. Currently the only ``lock.*`` entity in the user's
HA is ``lock.han_lock`` (the BYD HAN EV door lock); the Aqara Smart Lock
A100 referenced in the proposal isn't yet exposed via HA. These tools
are entity-domain generic so they work for the EV lock today AND any
future Aqara additions without code changes.

Security note: ``lock_lock`` / ``lock_unlock`` default to
``require_confirmation=True`` in the manifest because unlocking a door
from chat is higher-stakes than turning on a light.

Exposes:
- ``lock_status(area=None, entity_id=None)`` — list locks + state
- ``lock_lock(area=None, entity_id=None)`` — lock
- ``lock_unlock(area=None, entity_id=None)`` — unlock
"""

from __future__ import annotations

from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.lock")


async def _list_locks_with_areas() -> list[dict[str, Any]]:
    client = get_ha_client()
    template = (
        "[{% for s in states.lock if true %}"
        "{{ '{' }}\"entity_id\": \"{{ s.entity_id }}\", "
        "\"name\": \"{{ state_attr(s.entity_id, 'friendly_name') or s.entity_id }}\", "
        "\"area\": \"{{ area_name(s.entity_id) or '' }}\", "
        "\"state\": \"{{ s.state }}\""
        "{{ '}' }}{% if not loop.last %},{% endif %}"
        "{% endfor %}]"
    )
    import json as _json

    rendered = await client.render_template(template)
    try:
        return _json.loads(rendered)
    except _json.JSONDecodeError:
        logger.warning("lock_list_bad_json", rendered=rendered[:200])
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
    if area:
        matched = _match(entries, area)
        if len(matched) == 1:
            return matched[0], None
        if len(matched) > 1:
            return None, {
                "ok": False,
                "error": "ambiguous_lock",
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
            }
    if entity_id:
        for e in entries:
            if e["entity_id"] == entity_id:
                return e, None
    return None, {
        "ok": False,
        "error": "no_lock_found",
        "area": area,
        "entity_id": entity_id,
        "available": sorted(
            {e.get("area") or e.get("name") or "Unassigned" for e in entries}
        ),
    }


@tool("lock_status")
async def lock_status(
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Report the state of one or all locks.

    Returns ``{ok, locks: [{entity_id, name, area, state}]}``. State is
    typically ``locked``, ``unlocked``, ``locking``, ``unlocking`` or
    ``unavailable``.
    """
    entries = await _list_locks_with_areas()
    if area or entity_id:
        entry, err = _resolve_single(entries, area, entity_id)
        if err is not None:
            return err
        assert entry is not None
        return {"ok": True, "locks": [entry]}
    return {"ok": True, "locks": entries}


@tool("lock_lock", side_effects=True)
async def lock_lock(
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Lock a door / vehicle.

    Returns ``{ok, entity_id, name, area, previous_state, message}``.
    """
    entries = await _list_locks_with_areas()
    entry, err = _resolve_single(entries, area, entity_id)
    if err is not None:
        return err
    assert entry is not None
    client = get_ha_client()
    await client.call_service("lock", "lock", {"entity_id": entry["entity_id"]})
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "area": entry.get("area"),
        "previous_state": entry.get("state"),
        "message": f"Locking {entry['name']}.",
    }


@tool("lock_unlock", side_effects=True)
async def lock_unlock(
    area: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Unlock a door / vehicle.

    Returns ``{ok, entity_id, name, area, previous_state, message}``.
    """
    entries = await _list_locks_with_areas()
    entry, err = _resolve_single(entries, area, entity_id)
    if err is not None:
        return err
    assert entry is not None
    client = get_ha_client()
    await client.call_service(
        "lock", "unlock", {"entity_id": entry["entity_id"]}
    )
    return {
        "ok": True,
        "entity_id": entry["entity_id"],
        "name": entry["name"],
        "area": entry.get("area"),
        "previous_state": entry.get("state"),
        "message": f"Unlocking {entry['name']}.",
    }
