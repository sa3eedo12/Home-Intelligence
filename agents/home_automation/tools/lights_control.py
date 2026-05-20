"""Light control tools — turn lights on/off via HA service calls.

Operates across BOTH ``light.*`` entities (Hue bulbs, smart bulbs, etc.)
AND ``switch.*`` entities that control lights (Aqara wall switches, smart
plugs powering lamps, etc.). Without the switch coverage, "turn off all
the lights" silently left every wall-switched ceiling fixture on while
reporting success — see the May 19 incident.

Two tools:
  - lights_off (entity_ids: optional list, area: optional)
      Turn off specific lights, all lights in an area, or all lights.
  - lights_on (entity_ids, area, brightness, color)
      Symmetric. Brightness 0-255, color as RGB tuple or named color.

Both tools dispatch to the correct HA service per entity domain
(light.turn_off vs switch.turn_off) and return a structured result the
chat router can phrase into a confirmation.
"""
from __future__ import annotations

import asyncio
from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.lights_control")

# Same false-positive blocklist the lights_observer uses — backlights /
# indicators are technically light.* entities but the user almost never
# means them when they say "turn off the lights".
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

# Substrings that mark a switch.* entity as a "light switch" (Aqara wall
# switches, smart plugs powering lamps, etc.). When the user says "turn
# off all the lights", these get treated as lights too. Matched against
# entity_id + friendly_name.
LIGHT_SWITCH_KEYWORDS: tuple[str, ...] = (
    "wall_switch",
    "wall switch",
    "light",
    "lamp",
    "bulb",
    "led",
    "sconce",
    "chandelier",
    "ceiling",
    "spotlight",
    "downlight",
    "uplight",
)

# Substrings that EXCLUDE a switch.* from being a light switch, even if
# its name happens to brush against a LIGHT_SWITCH_KEYWORD. e.g.
# switch.sound_sensor_tv_sound_detection — sensor, not a light.
NON_LIGHT_SWITCH_KEYWORDS: tuple[str, ...] = (
    "pc_power",
    "headphones",
    "speakers",
    "charger",
    "usb",
    "wifi",
    "network",
    "pairing_mode",
    "api_usage",
    "polling",
    "a_c",
    "ac_on",
    "heating",
    "ventilation",
    "wrinkle_prevent",
    "bubble_soak",
    "sound_sensor",
    "light_sensor",  # the sensor, not a controllable light
    "camera",
    "use_image_sensor_camera",
    "prompt_sound",
    "thermostat",
    "mqtt",
)


def _is_real_light(entity_id: str, friendly_name: str | None) -> bool:
    haystack = (entity_id + " " + (friendly_name or "")).casefold()
    return not any(kw in haystack for kw in NON_BEDTIME_KEYWORDS)


def _is_light_switch(entity_id: str, friendly_name: str | None) -> bool:
    """True iff a ``switch.*`` entity should be treated as a light switch.

    A switch counts as a light switch only when it has a light-themed
    keyword AND none of the explicit exclusions. The exclusion list is
    kept generous because false negatives ("forgot to turn off a lamp")
    are far less annoying than false positives ("turned off the network
    when I asked for lights").
    """
    haystack = (entity_id + " " + (friendly_name or "")).casefold()
    if any(kw in haystack for kw in NON_LIGHT_SWITCH_KEYWORDS):
        return False
    return any(kw in haystack for kw in LIGHT_SWITCH_KEYWORDS)


async def _resolve_lights(
    entity_ids: list[str] | None,
    area: str | None,
    *,
    include_switches: bool = True,
) -> list[dict[str, Any]]:
    """Return controllable "light" entities (each ``{entity_id,
    friendly_name, state, domain}``) matching the caller's intent.

    Walks both ``light.*`` and ``switch.*`` so wall-switched ceiling
    fixtures (the Aqara wall-switch case) get included when the user
    says "turn off all the lights". Pass ``include_switches=False`` to
    restrict to native ``light.*`` only.

    Returns ALL real lights when both ``entity_ids`` and ``area`` are
    None. Tag each entry with ``domain`` so the caller can dispatch
    to the correct HA service (``light.turn_off`` vs
    ``switch.turn_off``).
    """
    client = get_ha_client()
    domains_to_fetch = ["light"]
    if include_switches:
        domains_to_fetch.append("switch")
    grouped = await asyncio.gather(
        *(client.list_states(domain=d) for d in domains_to_fetch)
    )

    real: list[dict[str, Any]] = []
    for domain, states in zip(domains_to_fetch, grouped):
        for s in states:
            attrs = s.get("attributes") or {}
            friendly = attrs.get("friendly_name")
            eid = s["entity_id"]
            if domain == "light":
                if not _is_real_light(eid, friendly):
                    continue
            else:  # switch
                if not _is_light_switch(eid, friendly):
                    continue
            real.append(
                {
                    "entity_id": eid,
                    "friendly_name": friendly or eid,
                    "state": s.get("state"),
                    "domain": domain,
                }
            )

    if entity_ids:
        wanted = {eid.casefold() for eid in entity_ids}
        return [r for r in real if r["entity_id"].casefold() in wanted]
    if area:
        area_lower = area.casefold()
        return [
            r
            for r in real
            if area_lower in r["friendly_name"].casefold()
            or area_lower in r["entity_id"].casefold()
        ]
    return real


@tool("lights_off", side_effects=True)
async def lights_off(
    entity_ids: list[str] | None = None,
    area: str | None = None,
    only_on: bool = True,
    include_switches: bool = True,
) -> dict[str, Any]:
    """Turn off lights.

    Args:
        entity_ids: Specific light.*/switch.* entity IDs. Wins over ``area``.
        area: Substring match against entity_id or friendly_name (e.g.
            'living room', 'kitchen'). Used when entity_ids is empty.
        only_on: If True (default), only act on lights currently 'on'
            so we don't waste a service call on already-off lights.
            Set False to force-off everything matched.
        include_switches: If True (default), also targets ``switch.*``
            entities that look like light switches (Aqara wall switches,
            smart plugs powering lamps). Set False to restrict to native
            ``light.*`` only.

    Returns ``{ok, turned_off: [{entity_id, friendly_name, domain}],
    skipped: [{entity_id, reason}], summary}``.
    """
    targets = await _resolve_lights(entity_ids, area, include_switches=include_switches)
    if only_on:
        actionable = [t for t in targets if t.get("state") == "on"]
        skipped = [
            {"entity_id": t["entity_id"], "reason": "already_off"}
            for t in targets
            if t.get("state") != "on"
        ]
    else:
        actionable = targets
        skipped = []

    if not actionable:
        return {
            "ok": True,
            "turned_off": [],
            "skipped": skipped,
            "summary": (
                "No lights are currently on."
                if only_on
                else "No lights matched your request."
            ),
        }

    client = get_ha_client()
    turned_off: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for target in actionable:
        domain = target.get("domain") or "light"
        try:
            # Dispatch to the entity's own domain so switch.* lamps
            # get switch.turn_off, not light.turn_off (HA would
            # silently no-op the wrong-domain call).
            await client.call_service(
                domain, "turn_off", {"entity_id": target["entity_id"]}
            )
            turned_off.append(
                {
                    "entity_id": target["entity_id"],
                    "friendly_name": target["friendly_name"],
                    "domain": domain,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lights_off_call_failed",
                entity_id=target["entity_id"],
                domain=domain,
                error=str(exc),
            )
            failures.append({"entity_id": target["entity_id"], "reason": str(exc)})

    summary = _summarize_off(turned_off, failures, area=area)
    return {
        "ok": not failures,
        "turned_off": turned_off,
        "skipped": skipped + failures,
        "summary": summary,
    }


@tool("lights_on", side_effects=True)
async def lights_on(
    entity_ids: list[str] | None = None,
    area: str | None = None,
    brightness: int | None = None,
    include_switches: bool = True,
) -> dict[str, Any]:
    """Turn on lights, optionally with a brightness 0-255.

    Same arg semantics as lights_off — including the
    ``include_switches`` flag, which decides whether to also target
    ``switch.*`` light entities. ``brightness`` is ignored for the
    switch domain (HA's switch service has no brightness concept).

    Returns ``{ok, turned_on, skipped, summary}``.
    """
    targets = await _resolve_lights(entity_ids, area, include_switches=include_switches)
    actionable = [t for t in targets if t.get("state") != "on"]
    skipped = [
        {"entity_id": t["entity_id"], "reason": "already_on"}
        for t in targets
        if t.get("state") == "on"
    ]
    if not actionable:
        return {
            "ok": True,
            "turned_on": [],
            "skipped": skipped,
            "summary": "All matching lights were already on.",
        }
    light_payload: dict[str, Any] = {}
    if brightness is not None:
        try:
            light_payload["brightness"] = max(0, min(int(brightness), 255))
        except (TypeError, ValueError):
            pass

    client = get_ha_client()
    turned_on: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for target in actionable:
        domain = target.get("domain") or "light"
        # switch.turn_on has no brightness parameter — pass it only to light.*.
        data = {"entity_id": target["entity_id"]}
        if domain == "light":
            data.update(light_payload)
        try:
            await client.call_service(domain, "turn_on", data)
            turned_on.append(
                {
                    "entity_id": target["entity_id"],
                    "friendly_name": target["friendly_name"],
                    "domain": domain,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lights_on_call_failed",
                entity_id=target["entity_id"],
                domain=domain,
                error=str(exc),
            )
            failures.append({"entity_id": target["entity_id"], "reason": str(exc)})

    summary = _summarize_on(turned_on, failures, area=area, brightness=brightness)
    return {
        "ok": not failures,
        "turned_on": turned_on,
        "skipped": skipped + failures,
        "summary": summary,
    }


@tool("lights_status")
async def lights_status() -> dict[str, Any]:
    """Return a count + list of currently-on lights. Cheap enough that the
    chat router can call it before deciding what to suggest."""
    targets = await _resolve_lights(None, None)
    on_lights = [
        {"entity_id": t["entity_id"], "friendly_name": t["friendly_name"]}
        for t in targets
        if t.get("state") == "on"
    ]
    return {
        "ok": True,
        "on_count": len(on_lights),
        "total_count": len(targets),
        "on_lights": on_lights,
    }


def _summarize_off(
    turned_off: list[dict[str, str]],
    failures: list[dict[str, str]],
    *,
    area: str | None,
) -> str:
    if not turned_off and failures:
        return f"Failed to turn off {len(failures)} light(s)."
    n = len(turned_off)
    if area:
        scope = f" in {area}"
    else:
        scope = ""
    if n == 0:
        return f"No lights were on{scope}."
    if n == 1:
        return f"Turned off {turned_off[0]['friendly_name']}."
    if n <= 3:
        names = ", ".join(t["friendly_name"] for t in turned_off)
        return f"Turned off {n} lights{scope}: {names}."
    sample = ", ".join(t["friendly_name"] for t in turned_off[:3])
    return f"Turned off {n} lights{scope} (e.g. {sample})."


def _summarize_on(
    turned_on: list[dict[str, str]],
    failures: list[dict[str, str]],
    *,
    area: str | None,
    brightness: int | None,
) -> str:
    if not turned_on and failures:
        return f"Failed to turn on {len(failures)} light(s)."
    n = len(turned_on)
    if area:
        scope = f" in {area}"
    else:
        scope = ""
    bright = f" at brightness {brightness}" if brightness is not None else ""
    if n == 0:
        return f"No lights matched{scope}."
    if n == 1:
        return f"Turned on {turned_on[0]['friendly_name']}{bright}."
    if n <= 3:
        names = ", ".join(t["friendly_name"] for t in turned_on)
        return f"Turned on {n} lights{scope}{bright}: {names}."
    sample = ", ".join(t["friendly_name"] for t in turned_on[:3])
    return f"Turned on {n} lights{scope}{bright} (e.g. {sample})."
