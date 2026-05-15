"""Light control tools — turn lights on/off via HA service calls.

User asked the chat to "turn off all the lights" and got back "I couldn't
process your request" because no tool existed. This module fills that gap.

Two tools:
  - lights_off (entity_ids: optional list, area: optional)
      Turn off specific lights, all lights in an area, or all lights.
  - lights_on (entity_ids, area, brightness, color)
      Symmetric. Brightness 0-255, color as RGB tuple or named color.

Both tools call HA's light.turn_off / light.turn_on services and return
a structured result the chat router can phrase into a confirmation.
"""
from __future__ import annotations

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


def _is_real_light(entity_id: str, friendly_name: str | None) -> bool:
    haystack = (entity_id + " " + (friendly_name or "")).casefold()
    return not any(kw in haystack for kw in NON_BEDTIME_KEYWORDS)


async def _resolve_lights(
    entity_ids: list[str] | None,
    area: str | None,
) -> list[dict[str, Any]]:
    """Return a list of light entities (each {entity_id, friendly_name,
    state}) matching the caller's intent. If both args are None, returns
    every "real" light entity (filtering out backlights/indicators)."""
    client = get_ha_client()
    states = await client.list_states(domain="light")
    real = [
        {
            "entity_id": s["entity_id"],
            "friendly_name": (s.get("attributes") or {}).get(
                "friendly_name", s["entity_id"]
            ),
            "state": s.get("state"),
        }
        for s in states
        if _is_real_light(
            s["entity_id"], (s.get("attributes") or {}).get("friendly_name")
        )
    ]
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
) -> dict[str, Any]:
    """Turn off lights.

    Args:
        entity_ids: Specific light.* entity IDs. Wins over ``area``.
        area: Substring match against entity_id or friendly_name (e.g.
            'living room', 'kitchen'). Used when entity_ids is empty.
        only_on: If True (default), only act on lights currently 'on'
            so we don't waste a service call on already-off lights.
            Set False to force-off everything matched.

    Returns ``{ok, turned_off: [{entity_id, friendly_name}], skipped:
    [{entity_id, reason}], summary}``.
    """
    targets = await _resolve_lights(entity_ids, area)
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
        try:
            await client.call_service(
                "light", "turn_off", {"entity_id": target["entity_id"]}
            )
            turned_off.append(
                {"entity_id": target["entity_id"], "friendly_name": target["friendly_name"]}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lights_off_call_failed",
                entity_id=target["entity_id"],
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
) -> dict[str, Any]:
    """Turn on lights, optionally with a brightness 0-255.

    Same arg semantics as lights_off. Returns ``{ok, turned_on, skipped,
    summary}``.
    """
    targets = await _resolve_lights(entity_ids, area)
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
    payload: dict[str, Any] = {}
    if brightness is not None:
        try:
            payload["brightness"] = max(0, min(int(brightness), 255))
        except (TypeError, ValueError):
            pass

    client = get_ha_client()
    turned_on: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for target in actionable:
        try:
            await client.call_service(
                "light",
                "turn_on",
                {"entity_id": target["entity_id"], **payload},
            )
            turned_on.append(
                {"entity_id": target["entity_id"], "friendly_name": target["friendly_name"]}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lights_on_call_failed",
                entity_id=target["entity_id"],
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
