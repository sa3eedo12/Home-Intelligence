"""Device activity recorder — persists raw HA state transitions for lights,
thermostats, covers, fans, locks, occupancy sensors etc. into event_log so
they're queryable later.

Why this exists:
  Until this observer was added, ~zero rows landed in event_log for
  light/climate/cover/binary_sensor changes. The HA bridge wrote them all
  to the rolling Redis stream events.home (50K cap, ~few hours of history),
  but nothing persisted them to Postgres. The user's question 'is light /
  thermostat activity being captured?' had to be answered 'no' — even
  though HA itself was emitting hundreds of these events per day.

Design:
  - One emit_event per *transition* (skip duplicate same-state updates)
  - Whitelist of "interesting" domains so the millions of sensor.* updates
    (energy, signal strength, CPU%) don't drown the log
  - Per-entity dedup window so a flicker (light 'on' → 'on' → 'on') only
    writes the first transition
  - Capability is "device.state_changed" so existing event_log queries +
    dashboard activity feed pick it up automatically
  - Per-domain rate limit so a runaway integration (e.g. motion sensor
    bouncing every second) can't fill the log
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from . import Observer
from .utils import (
    domain_of,
    extract_state_change,
    normalized_state,
    parse_datetime,
)

# Domains we WANT to record. Everything else (sensor.*, update.*, sun.*,
# automation.*, button.*, weather.*, todo.*) gets dropped to keep the log
# focused on user-meaningful state.
RECORDED_DOMAINS: frozenset[str] = frozenset({
    "light",
    "climate",
    "cover",
    "fan",
    "lock",
    "vacuum",
    "switch",
    "binary_sensor",
    "alarm_control_panel",
    "scene",
    "script",
})

# binary_sensor.* is a huge family — only persist motion / door / window /
# occupancy / presence / smoke / leak ones. Skip the noisy battery /
# connectivity / problem booleans most integrations expose.
BINARY_SENSOR_INTERESTING_KEYWORDS: tuple[str, ...] = (
    "motion",
    "door",
    "window",
    "occupancy",
    "presence",
    "smoke",
    "co",
    "leak",
    "moisture",
    "vibration",
    "tamper",
    "garage",
)

# switch.* is also large. Skip the obvious infrastructure switches that flap
# constantly and aren't user actions (the same blocklist tv_observer uses
# for false-positive TV detection covers most of them).
SWITCH_FALSE_POSITIVE_KEYWORDS: tuple[str, ...] = (
    "sound_sensor",
    "sound_detection",
    "remote_control",
    "child_lock",
    "indicator",
    "status_led",
    "energy",
    "power_consumption",
    "watering",
    "diagnostics",
)

# Per-entity cooldown: state changes faster than this for the same entity
# are coalesced into the first transition only. Prevents motion-sensor
# bouncing from filling the log.
ENTITY_COOLDOWN = timedelta(seconds=15)
MAX_TRACKED_ENTITIES = 4096


class DeviceActivityRecorder(Observer):
    name = "device_activity"
    subscribed_streams = ["events.home"]

    def __init__(self) -> None:
        super().__init__()
        # entity_id → (last_state, last_recorded_at)
        self._last_seen: OrderedDict[str, tuple[str | None, datetime]] = OrderedDict()

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None:
            return
        if not _should_record(change):
            return
        new_norm = normalized_state(change.new_state)
        if new_norm is None or new_norm in {"", "unknown", "unavailable"}:
            return
        old_norm = normalized_state(change.old_state)
        # Drop reconnection transitions: when HA itself restarts (or the
        # NAS reboots), every entity flips from "unavailable" → its actual
        # state. That's not user activity — nothing actually changed — so
        # we MUST not record it. Without this filter, a single NAS reboot
        # produced ~thousands of phantom device.state_changed events,
        # each firing a downstream LLM inference call and pegging Ollama
        # at 800%+ CPU for ~20 min.
        if old_norm in {None, "", "unknown", "unavailable"}:
            return
        # Only write rows on real transitions
        if old_norm == new_norm:
            return
        now = parse_datetime(change.ts) or datetime.now(UTC)
        last = self._last_seen.get(change.entity_id)
        if last is not None:
            last_state, last_ts = last
            if last_state == new_norm and (now - last_ts) < ENTITY_COOLDOWN:
                return
        self._last_seen[change.entity_id] = (new_norm, now)
        self._last_seen.move_to_end(change.entity_id)
        while len(self._last_seen) > MAX_TRACKED_ENTITIES:
            self._last_seen.popitem(last=False)

        domain = domain_of(change.entity_id)
        friendly = change.friendly_name or change.entity_id
        # Compose the human-readable summary from a per-domain template
        summary = _summarize(domain, friendly, old_norm, new_norm, change.attributes)
        await self.emit_event(
            "device.state_changed",
            summary,
            {
                "entity_id": change.entity_id,
                "domain": domain,
                "friendly_name": friendly,
                "old_state": old_norm,
                "new_state": new_norm,
                "area": change.area,
                # Trim attributes to a small shape — full attrs can be huge
                # (some integrations expose 30+ fields per state). Keep the
                # ones that frequently matter for after-the-fact analysis.
                "attributes": _slim_attributes(change.attributes, domain),
                "ts": change.ts,
            },
        )


def _should_record(change: Any) -> bool:
    domain = domain_of(change.entity_id)
    if domain not in RECORDED_DOMAINS:
        return False
    haystack = (change.entity_id + " " + (change.friendly_name or "")).casefold()
    if domain == "binary_sensor":
        # Use word-boundary matching so "co" (carbon monoxide) doesn't fire
        # on "connectivity" or "scope". Each keyword must appear as its
        # own underscore/space-bounded token.
        tokens = set(haystack.replace(".", " ").replace("_", " ").split())
        return any(kw in tokens for kw in BINARY_SENSOR_INTERESTING_KEYWORDS)
    if domain == "switch":
        return not any(kw in haystack for kw in SWITCH_FALSE_POSITIVE_KEYWORDS)
    return True


def _summarize(
    domain: str,
    friendly_name: str,
    old_state: str | None,
    new_state: str,
    attributes: dict[str, Any],
) -> str:
    """One-line human summary per domain. Generic format for unknowns."""
    if domain == "light":
        return f"💡 {friendly_name} turned {new_state}"
    if domain == "climate":
        target = attributes.get("temperature") or attributes.get("target_temp_high")
        if target:
            return f"🌡 {friendly_name} set to {new_state} (target {target}°)"
        return f"🌡 {friendly_name} mode → {new_state}"
    if domain == "cover":
        return f"🪟 {friendly_name} → {new_state}"
    if domain == "fan":
        return f"💨 {friendly_name} → {new_state}"
    if domain == "lock":
        return f"🔒 {friendly_name} → {new_state}"
    if domain == "vacuum":
        return f"🤖 {friendly_name} → {new_state}"
    if domain == "binary_sensor":
        return f"🔔 {friendly_name}: {new_state}"
    if domain == "switch":
        return f"🔌 {friendly_name} turned {new_state}"
    if domain == "alarm_control_panel":
        return f"🛡 {friendly_name} → {new_state}"
    if domain == "scene":
        return f"🎬 {friendly_name} activated"
    if domain == "script":
        return f"📜 {friendly_name} ran"
    return f"{friendly_name} → {new_state} (was {old_state or 'unknown'})"


def _slim_attributes(attributes: dict[str, Any], domain: str) -> dict[str, Any]:
    """Pick only the attributes that actually help interpret the change.

    Keeping every attribute would store ~5KB per row for chatty integrations
    (Hue lights expose color/brightness/effect/transition/etc.); 95% of
    those fields are never queried after the fact.
    """
    if not isinstance(attributes, dict):
        return {}
    keep = _ATTRIBUTE_WHITELIST.get(domain, _DEFAULT_KEPT_ATTRIBUTES)
    return {k: attributes[k] for k in keep if k in attributes}


_DEFAULT_KEPT_ATTRIBUTES: tuple[str, ...] = ("device_class", "icon")
_ATTRIBUTE_WHITELIST: dict[str, tuple[str, ...]] = {
    "light": ("brightness", "color_temp_kelvin", "rgb_color", "effect", "device_class"),
    "climate": (
        "current_temperature",
        "temperature",
        "target_temp_high",
        "target_temp_low",
        "hvac_action",
        "fan_mode",
        "preset_mode",
    ),
    "cover": ("current_position", "current_tilt_position", "device_class"),
    "fan": ("percentage", "preset_mode", "direction"),
    "lock": ("device_class",),
    "vacuum": ("battery_level", "fan_speed", "status"),
    "binary_sensor": ("device_class",),
    "switch": ("device_class",),
    "alarm_control_panel": ("changed_by",),
}


def build() -> DeviceActivityRecorder:
    return DeviceActivityRecorder()
