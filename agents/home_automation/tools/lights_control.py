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
import re
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
#
# Short tokens like "led" need word-boundary matching to avoid false
# positives like "enabled" (en-ab-LED) — handled in _is_light_switch.
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

# Tokens that must match on a word boundary (\b) rather than as a raw
# substring. "led" is the canonical case — without this we'd light-up
# (pun intended) anything containing "enabled", "fulfilled", "called",
# etc.
_WORD_BOUNDARY_KEYWORDS: frozenset[str] = frozenset({"led"})

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
    # Generic "enable/disable" toggles (e.g. switch.husamsaf "Enabled")
    # are config flags, not real device controls.
    "enable",
    "disable",
)


_WORD_BOUNDARY_PATTERNS = {
    kw: re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
    for kw in _WORD_BOUNDARY_KEYWORDS
}


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

    Short tokens (``_WORD_BOUNDARY_KEYWORDS``, e.g. "led") are matched
    with \\b word boundaries so they don't fire on things like
    "Husamsaf en**led** abled" (the original false positive that turned
    Husamsaf-Enabled config flags into "lights").
    """
    haystack = (entity_id + " " + (friendly_name or "")).casefold()
    if any(kw in haystack for kw in NON_LIGHT_SWITCH_KEYWORDS):
        return False
    for kw in LIGHT_SWITCH_KEYWORDS:
        if kw in _WORD_BOUNDARY_KEYWORDS:
            if _WORD_BOUNDARY_PATTERNS[kw].search(haystack):
                return True
        elif kw in haystack:
            return True
    return False


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


# How long to wait after firing a service call before re-querying the
# entity state. HA's service queue + the underlying Zigbee/Hue/Z-Wave
# round-trip typically settles within ~1s; 1.5s gives us margin without
# blowing the chat reply latency budget. Tunable via env for slow setups.
_VERIFY_DELAY_S = float(__import__("os").getenv("LIGHTS_VERIFY_DELAY_S", "1.5"))


async def _verify_state_changes(
    targets: list[dict[str, Any]],
    *,
    desired_state: str,
) -> dict[str, dict[str, Any]]:
    """Re-query each target after a brief settle delay and return a map
    of entity_id → {state, ok}.

    HA returns 200 to ``service.turn_on/off`` regardless of whether the
    underlying device actually transitioned (Aqara wall switches go
    offline, Hue bulbs lose Zigbee, smart plugs unplug). Without this
    post-call check, we'd happily tell the user "Office Light is now on"
    when the entity is still ``off`` because the device hasn't responded.

    Args:
        targets: list of {entity_id, ...} dicts to re-check
        desired_state: 'on' or 'off' — the state we expect to see
    """
    import asyncio as _asyncio

    if not targets:
        return {}
    await _asyncio.sleep(_VERIFY_DELAY_S)
    client = get_ha_client()
    out: dict[str, dict[str, Any]] = {}
    for t in targets:
        eid = t["entity_id"]
        try:
            current = await client.get_state(eid)
        except Exception as exc:  # noqa: BLE001
            out[eid] = {"state": None, "ok": False, "error": str(exc)}
            continue
        state = current.get("state")
        out[eid] = {"state": state, "ok": state == desired_state}
    return out


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

    Post-state verified: each entity is re-queried ~1.5s after the
    service call and moved from ``turned_off`` → ``skipped`` (with
    ``reason='state_unchanged_after_call'``) when the device didn't
    actually transition to ``off``. Prevents the "I turned it off" lie
    when an Aqara wall switch or Hue bulb is offline.
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

    # Post-state verification: re-query each entity that we tried to
    # turn off; if its state is still 'on' (or unavailable), move it
    # from turned_off → failures with an honest reason. Without this we
    # would report success while an offline Aqara switch sits stuck-on.
    verification = await _verify_state_changes(turned_off, desired_state="off")
    if verification:
        confirmed: list[dict[str, str]] = []
        for entry in turned_off:
            check = verification.get(entry["entity_id"])
            if check is None or check.get("ok"):
                confirmed.append(entry)
            else:
                failures.append({
                    "entity_id": entry["entity_id"],
                    "friendly_name": entry["friendly_name"],
                    "reason": "state_unchanged_after_call",
                    "actual_state": check.get("state"),
                })
                logger.warning(
                    "lights_off_state_unchanged",
                    entity_id=entry["entity_id"],
                    actual_state=check.get("state"),
                )
        turned_off = confirmed

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

    # Post-state verification — see _verify_state_changes. Live evidence:
    # switch.office_light returns 200 to switch.turn_on but the Aqara
    # device has been offline for 2 days; without this check we'd tell
    # the user "Office Light is now on" while the bulb stays dark.
    verification = await _verify_state_changes(turned_on, desired_state="on")
    if verification:
        confirmed: list[dict[str, str]] = []
        for entry in turned_on:
            check = verification.get(entry["entity_id"])
            if check is None or check.get("ok"):
                confirmed.append(entry)
            else:
                failures.append({
                    "entity_id": entry["entity_id"],
                    "friendly_name": entry["friendly_name"],
                    "reason": "state_unchanged_after_call",
                    "actual_state": check.get("state"),
                })
                logger.warning(
                    "lights_on_state_unchanged",
                    entity_id=entry["entity_id"],
                    actual_state=check.get("state"),
                )
        turned_on = confirmed

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


def _format_failures(failures: list[dict[str, Any]]) -> str:
    """Render a short, accurate suffix that names devices that didn't
    actually transition. Distinguishes 'unchanged' (device offline /
    stuck) from generic call errors so the user gets actionable info."""
    if not failures:
        return ""
    unchanged = [
        f for f in failures if f.get("reason") == "state_unchanged_after_call"
    ]
    if unchanged:
        names = ", ".join(
            f.get("friendly_name", f["entity_id"]) for f in unchanged[:3]
        )
        more = (
            f" (+{len(unchanged) - 3} more)" if len(unchanged) > 3 else ""
        )
        return (
            f" {len(unchanged)} device(s) didn't respond — likely offline: "
            f"{names}{more}."
        )
    names = ", ".join(
        f.get("friendly_name", f["entity_id"]) for f in failures[:3]
    )
    more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
    return f" {len(failures)} call(s) failed: {names}{more}."


def _summarize_off(
    turned_off: list[dict[str, str]],
    failures: list[dict[str, str]],
    *,
    area: str | None,
) -> str:
    if not turned_off and failures:
        return f"Couldn't turn off any lights.{_format_failures(failures)}"
    n = len(turned_off)
    if area:
        scope = f" in {area}"
    else:
        scope = ""
    if n == 0:
        return f"No lights were on{scope}."
    if n == 1:
        base = f"Turned off {turned_off[0]['friendly_name']}."
    elif n <= 3:
        names = ", ".join(t["friendly_name"] for t in turned_off)
        base = f"Turned off {n} lights{scope}: {names}."
    else:
        sample = ", ".join(t["friendly_name"] for t in turned_off[:3])
        base = f"Turned off {n} lights{scope} (e.g. {sample})."
    return base + _format_failures(failures)


def _summarize_on(
    turned_on: list[dict[str, str]],
    failures: list[dict[str, str]],
    *,
    area: str | None,
    brightness: int | None,
) -> str:
    if not turned_on and failures:
        return f"Couldn't turn on any lights.{_format_failures(failures)}"
    n = len(turned_on)
    if area:
        scope = f" in {area}"
    else:
        scope = ""
    bright = f" at brightness {brightness}" if brightness is not None else ""
    if n == 0:
        return f"No lights matched{scope}."
    if n == 1:
        base = f"Turned on {turned_on[0]['friendly_name']}{bright}."
    elif n <= 3:
        names = ", ".join(t["friendly_name"] for t in turned_on)
        base = f"Turned on {n} lights{scope}{bright}: {names}."
    else:
        sample = ", ".join(t["friendly_name"] for t in turned_on[:3])
        base = f"Turned on {n} lights{scope}{bright} (e.g. {sample})."
    return base + _format_failures(failures)
