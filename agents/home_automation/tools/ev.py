"""Electric-vehicle (BYD HAN) status as a first-class tool.

Closes proposal #58 (collapses #59 + #60 duplicates). The user asked
"what is the battery percentage of my car?" and the escalator gave up
after discovering related entities but never composed a clean reply —
the HAN exposes ~20 entities (sensor.han_battery_level,
binary_sensor.han_charging, lock.han_lock, climate.han_climate, etc.)
and the router LLM has no way to know which ones constitute "car
status" without a dedicated tool.

This module provides a single consolidated read tool plus the
handful of useful write actions HA's BYD HAN integration exposes:

- ``ev_status(vehicle=None)`` — battery, range, charging, locked,
  doors/windows, sentry, online, climate state.
- ``ev_start_charging(vehicle=None)`` — press the start-charging button.
- ``ev_close_windows(vehicle=None)`` — press the close-windows button.
- ``ev_flash_lights(vehicle=None)`` — press the flash-lights button
  (useful for locating in a car park).
- ``ev_find_car(vehicle=None)`` — press the find-car button.

The ``vehicle`` slug is the part after the ``_`` in entity ids
(``han`` for BYD HAN, ``model3`` for a hypothetical second car).
Defaults to the only car when there's exactly one.
"""

from __future__ import annotations

import re
from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.ev")

# Entity-id suffix → field in the status payload. Each row is
# (suffix, output_key, kind) where kind is 'numeric' | 'binary' | 'text'
# | 'lock'. The discovery template emits ALL ev sensors + binary_sensors
# + locks + climates and we filter / shape down to this set.
_STATUS_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("battery_level", "battery_level", "numeric"),
    ("range", "range_km", "numeric"),
    ("odometer", "odometer_km", "numeric"),
    ("charging", "charging", "binary"),
    ("online", "online", "binary"),
    ("locked", "locked", "binary"),
    ("doors", "doors_open", "binary"),
    ("windows", "windows_open", "binary"),
    ("sentry_mode", "sentry_mode", "binary"),
)


def _slug_from_entity(entity_id: str) -> str | None:
    """Extract the vehicle slug from an entity id like 'sensor.han_battery_level'.

    The slug is the first token after the domain ('han'), which is how
    HA's BYD HAN integration namespaces every entity per vehicle. Multi-
    car households would have e.g. ``han`` and ``model3``.
    """
    match = re.match(r"^[^.]+\.([a-z0-9]+)_", entity_id)
    return match.group(1) if match else None


async def _discover_ev_entities(vehicle: str | None) -> dict[str, list[dict[str, Any]]]:
    """Scan HA for ``{sensor,binary_sensor,lock,climate,button,device_tracker}.*``
    entities that look like an EV (BYD HAN), grouped by vehicle slug.

    Returns ``{vehicle_slug: [entity_dict, ...]}``.
    """
    client = get_ha_client()
    domains = (
        "sensor",
        "binary_sensor",
        "lock",
        "climate",
        "button",
        "device_tracker",
    )
    import json as _json

    all_entities: list[dict[str, Any]] = []
    # Render one domain at a time — concatenating multiple Jinja for-loops
    # into a single template produced JSON that periodically failed to
    # parse when a state value contained quotes/newlines, since the
    # cleanup ``.replace(",]", "]")`` only fixed the outer list.
    for d in domains:
        template = (
            "[{% for s in states." + d + " if true %}"
            "{{ '{' }}"
            "\"entity_id\": {{ s.entity_id | tojson }}, "
            "\"state\": {{ s.state | tojson }}, "
            "\"name\": {{ (state_attr(s.entity_id, 'friendly_name') "
            "or s.entity_id) | tojson }}"
            "{{ '}' }}"
            "{% if not loop.last %},{% endif %}"
            "{% endfor %}]"
        )
        rendered = await client.render_template(template)
        try:
            parsed = _json.loads(rendered)
        except _json.JSONDecodeError:
            logger.warning(
                "ev_list_bad_json", domain=d, rendered=rendered[:300]
            )
            continue
        if isinstance(parsed, list):
            all_entities.extend(parsed)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entity in all_entities:
        slug = _slug_from_entity(entity.get("entity_id", ""))
        if slug is None:
            continue
        grouped.setdefault(slug, []).append(entity)

    # Heuristic: an EV slug is one that has BOTH sensor.<slug>_battery_level
    # AND sensor.<slug>_odometer. This avoids treating e.g. a battery-
    # powered button as a car.
    keep: dict[str, list[dict[str, Any]]] = {}
    for slug, ents in grouped.items():
        ids = {e["entity_id"] for e in ents}
        if (
            f"sensor.{slug}_battery_level" in ids
            and f"sensor.{slug}_odometer" in ids
        ):
            keep[slug] = ents
    if vehicle:
        slug = vehicle.strip().casefold()
        if slug in keep:
            return {slug: keep[slug]}
        return {}
    return keep


def _coerce_number(raw: Any) -> float | None:
    if raw in (None, "", "unavailable", "unknown"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_binary(raw: Any) -> bool | None:
    if raw in (None, "", "unavailable", "unknown"):
        return None
    text = str(raw).strip().casefold()
    if text in {"on", "true", "1", "open", "charging"}:
        return True
    if text in {"off", "false", "0", "closed", "not_charging"}:
        return False
    return None


def _build_status(slug: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {e["entity_id"]: e for e in entities}
    out: dict[str, Any] = {"vehicle": slug}
    name_seed = by_id.get(f"sensor.{slug}_battery_level") or next(iter(by_id.values()))
    name_raw = str(name_seed.get("name") or "")
    # Strip the trailing " Battery level" / " Online" so we get a clean
    # vehicle name (e.g. "HAN Battery level" -> "HAN").
    out["name"] = name_raw.split(" ")[0] if name_raw else slug.upper()
    for suffix, key, kind in _STATUS_FIELDS:
        entity_id = (
            f"binary_sensor.{slug}_{suffix}"
            if kind == "binary"
            else f"sensor.{slug}_{suffix}"
        )
        ent = by_id.get(entity_id)
        if ent is None:
            out[key] = None
            continue
        if kind == "binary":
            out[key] = _coerce_binary(ent.get("state"))
        else:
            out[key] = _coerce_number(ent.get("state"))
    # Lock state from lock.<slug>_lock (HA exposes both binary_sensor and lock domain)
    lock = by_id.get(f"lock.{slug}_lock")
    if lock is not None:
        out["lock_state"] = lock.get("state")
    # Climate
    climate = by_id.get(f"climate.{slug}_climate")
    if climate is not None:
        out["climate_state"] = climate.get("state")
    # Location
    tracker = by_id.get(f"device_tracker.{slug}_location")
    if tracker is not None:
        loc_state = tracker.get("state")
        out["location"] = loc_state if loc_state not in ("unavailable", "unknown") else None
    return out


def _summary_for(status: dict[str, Any]) -> str:
    bits: list[str] = []
    bits.append(status.get("name") or status.get("vehicle", "Car"))
    if status.get("battery_level") is not None:
        bits.append(f"battery {int(status['battery_level'])}%")
    if status.get("range_km") is not None:
        bits.append(f"range {int(status['range_km'])} km")
    if status.get("charging") is True:
        bits.append("charging")
    if status.get("locked") is True:
        bits.append("locked")
    elif status.get("locked") is False:
        bits.append("UNLOCKED")
    if status.get("doors_open") is True:
        bits.append("doors open")
    if status.get("windows_open") is True:
        bits.append("windows open")
    if status.get("sentry_mode") is True:
        bits.append("sentry on")
    if status.get("online") is False:
        bits.append("offline")
    return ", ".join(bits) + "."


@tool("ev_status")
async def ev_status(vehicle: str | None = None) -> dict[str, Any]:
    """Return a consolidated snapshot of one or all EVs in the home.

    Args:
        vehicle: Optional vehicle slug (e.g. 'han'). Defaults to the
            only car when there's exactly one. Returns all when omitted
            and multiple cars exist.

    Returns ``{ok, vehicles: [{vehicle, name, battery_level, range_km,
    odometer_km, charging, online, locked, doors_open, windows_open,
    sentry_mode, lock_state, climate_state, location, summary}]}``.
    """
    grouped = await _discover_ev_entities(vehicle)
    if not grouped:
        return {
            "ok": False,
            "error": "no_ev_found" if vehicle else "no_evs_in_home",
            "vehicle": vehicle,
        }
    vehicles: list[dict[str, Any]] = []
    for slug, ents in grouped.items():
        status = _build_status(slug, ents)
        status["summary"] = _summary_for(status)
        vehicles.append(status)
    return {"ok": True, "vehicles": vehicles}


def _resolve_button(
    grouped: dict[str, list[dict[str, Any]]], button_suffix: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Find a button.<slug>_<suffix> entity. Returns (entity_id, error)."""
    if not grouped:
        return None, {"ok": False, "error": "no_evs_in_home"}
    if len(grouped) > 1:
        return None, {
            "ok": False,
            "error": "multiple_vehicles",
            "available": list(grouped.keys()),
            "hint": "Pass vehicle slug to disambiguate.",
        }
    slug = next(iter(grouped.keys()))
    entity_id = f"button.{slug}_{button_suffix}"
    available = {e["entity_id"] for e in grouped[slug]}
    if entity_id not in available:
        return None, {
            "ok": False,
            "error": "button_not_available",
            "expected_entity": entity_id,
        }
    return entity_id, None


async def _press_ev_button(
    vehicle: str | None, button_suffix: str, *, friendly_action: str
) -> dict[str, Any]:
    grouped = await _discover_ev_entities(vehicle)
    entity_id, err = _resolve_button(grouped, button_suffix)
    if err is not None:
        return err
    assert entity_id is not None
    client = get_ha_client()
    await client.call_service("button", "press", {"entity_id": entity_id})
    return {
        "ok": True,
        "entity_id": entity_id,
        "message": f"{friendly_action.capitalize()}.",
    }


@tool("ev_start_charging", side_effects=True)
async def ev_start_charging(vehicle: str | None = None) -> dict[str, Any]:
    """Press the EV's start-charging button.

    Returns ``{ok, entity_id, message}`` on success, or
    ``{ok: False, error: ...}`` on failure.
    """
    return await _press_ev_button(
        vehicle, "start_charging", friendly_action="starting charging"
    )


@tool("ev_close_windows", side_effects=True)
async def ev_close_windows(vehicle: str | None = None) -> dict[str, Any]:
    """Press the EV's close-windows button."""
    return await _press_ev_button(
        vehicle, "close_windows", friendly_action="closing windows"
    )


@tool("ev_flash_lights", side_effects=True)
async def ev_flash_lights(vehicle: str | None = None) -> dict[str, Any]:
    """Press the EV's flash-lights button (helps locate in a car park)."""
    return await _press_ev_button(
        vehicle, "flash_lights", friendly_action="flashing lights"
    )


@tool("ev_find_car", side_effects=True)
async def ev_find_car(vehicle: str | None = None) -> dict[str, Any]:
    """Press the EV's find-car button."""
    return await _press_ev_button(
        vehicle, "find_car", friendly_action="locating car"
    )
