"""GET /admin/setup/auto-discover and POST /admin/setup/auto-apply.

Closes the loop on "I have 600 HA entities, what should the system actually
adopt and link?". The auto-discover endpoint scans HA's /api/states *and*
its WebSocket device + entity registries so we can group multiple HA
entities (e.g. 12 entities for one Aqara thermostat) under the same
``thing`` instead of treating each as a standalone object.

    {
      "things_to_adopt": [{"entity_id":"...", "type":"...", "friendly_name":"...",
                           "device_id":"abc", "extra_entity_ids":["...","..."]}],
      "trackers_by_member": {"saeed": ["person.saeed", "device_tracker.saeeds_iphone"]},
      "duplicates": [["entity_id_a", "entity_id_b"]]   // suggested merges
    }

The auto-apply endpoint takes that same shape (or a user-edited version)
and applies it: adopts each thing into the registry (with ALL its child
entity_ids) and patches each household_member's attributes.tracker_entity_ids.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

import httpx
from home_agents_sdk.telemetry import get_logger

from .ha_event_bridge import _default_ws_connector, _ws_url_for

logger = get_logger("orchestrator.auto_setup")

# Patterns: entity_id substring → registry thing type. First match wins.
ADOPT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("washer_machine_state", "appliance.washer"),
    ("dryer_machine_state", "appliance.dryer"),
    ("dishwasher_machine_state", "appliance.dishwasher"),
    ("washer_job_state", "appliance.washer"),
    ("dryer_job_state", "appliance.dryer"),
)

# Per-domain rules where matching is by domain, not substring.
DOMAIN_TO_TYPE: tuple[tuple[str, str], ...] = (
    ("vacuum.", "device.vacuum"),
    ("lock.", "device.lock"),
    ("climate.", "device.climate"),
    ("camera.", "device.camera"),
)

# media_player.* with TV-ish names.
TV_KEYWORDS = ("tv", "samsung", "lg", "sony", "vizio")
MONITOR_KEYWORDS = ("monitor", "oled", "odyssey", "display")


def _classify_thing(entity_id: str, friendly_name: str) -> str | None:
    eid = entity_id.lower()
    fn = (friendly_name or "").lower()
    for substr, type_ in ADOPT_PATTERNS:
        if substr in eid:
            return type_
    for prefix, type_ in DOMAIN_TO_TYPE:
        if eid.startswith(prefix):
            return type_
    if eid.startswith("media_player."):
        if any(kw in eid + " " + fn for kw in MONITOR_KEYWORDS):
            return "device.monitor"
        if any(kw in eid + " " + fn for kw in TV_KEYWORDS):
            return "device.tv"
    if eid.startswith("light."):
        return "device.light"
    if eid.startswith("cover."):
        haystack = eid + " " + fn
        if "curtain" in haystack or "shade" in haystack or "blind" in haystack:
            return "device.cover"
    if eid.startswith("binary_sensor.") and ("motion" in eid or "occupancy" in eid):
        return "sensor.motion"
    if eid.startswith("event.") and "doorbell" in eid + " " + fn:
        return "device.doorbell"
    return None


def _normalize_name(name: str) -> str:
    """Lowercase, strip apostrophes/spaces — for fuzzy member↔tracker matching."""
    s = name.lower()
    for ch in "'’-_":
        s = s.replace(ch, "")
    return "".join(s.split())


def _person_entity_for(member_name: str, all_entities: Iterable[dict[str, Any]]) -> list[str]:
    """Find HA person.* / device_tracker.* entities that look like they belong
    to ``member_name``. Returns the list of matching entity_ids."""
    norm_member = _normalize_name(member_name)
    if not norm_member:
        return []
    matches: list[tuple[int, str]] = []
    for s in all_entities:
        eid = s.get("entity_id", "")
        if not (eid.startswith("person.") or eid.startswith("device_tracker.")):
            continue
        slug = eid.split(".", 1)[1]
        norm_slug = _normalize_name(slug)
        fn_norm = _normalize_name(str((s.get("attributes") or {}).get("friendly_name", "")))
        # Score: exact-match-of-name (3), starts-with (2), contains (1)
        score = 0
        for hay in (norm_slug, fn_norm):
            if hay == norm_member:
                score = max(score, 3)
            elif hay.startswith(norm_member):
                score = max(score, 2)
            elif norm_member in hay:
                score = max(score, 1)
        if score > 0:
            matches.append((score, eid))
    # Highest score first; prefer person.* over device_tracker.*
    matches.sort(key=lambda m: (-m[0], 0 if m[1].startswith("person.") else 1))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for _, eid in matches:
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


async def _ha_states() -> list[dict[str, Any]]:
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")
    if not ha_url or not ha_token:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{ha_url}/api/states",
            headers={"Authorization": f"Bearer {ha_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


async def _ha_registries() -> dict[str, list[dict[str, Any]]]:
    """Pull HA's device + entity + area registries via WebSocket.

    Returns ``{"devices": [...], "entities": [...], "areas": [...]}``. Each
    entity row has a ``device_id`` linking back to a device row, which is
    how we group the "12 entities for one Aqara thermostat" mess into a
    single thing. Devices and entities can also have ``area_id`` linking to
    an area row (e.g. "Living Room", "Bedroom").

    Failures (no HA, bad token, HA too old to expose registries) return
    empty lists so the caller can fall back to per-entity classification
    using ``/api/states``.
    """
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")
    if not ha_url or not ha_token:
        return {"devices": [], "entities": [], "areas": []}
    try:
        ws_url = _ws_url_for(ha_url)
        async with _default_ws_connector(ws_url) as ws:
            first = json.loads(await ws.recv())
            if first.get("type") != "auth_required":
                logger.warning("ha_registry_fetch_no_auth_required", got=first.get("type"))
                return {"devices": [], "entities": [], "areas": []}
            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            second = json.loads(await ws.recv())
            if second.get("type") != "auth_ok":
                logger.warning(
                    "ha_registry_fetch_auth_failed", message=second.get("message")
                )
                return {"devices": [], "entities": [], "areas": []}

            async def fetch(req_id: int, kind: str) -> list[dict[str, Any]]:
                await ws.send(json.dumps({"id": req_id, "type": f"config/{kind}/list"}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") != req_id:
                        continue
                    if msg.get("type") != "result":
                        return []
                    if not msg.get("success"):
                        logger.warning(
                            "ha_registry_fetch_request_failed",
                            kind=kind,
                            error=msg.get("error"),
                        )
                        return []
                    result = msg.get("result")
                    return result if isinstance(result, list) else []

            devices = await fetch(1, "device_registry")
            entities = await fetch(2, "entity_registry")
            areas = await fetch(3, "area_registry")
            return {"devices": devices, "entities": entities, "areas": areas}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ha_registry_fetch_failed", error=f"{type(exc).__name__}: {exc}")
        return {"devices": [], "entities": [], "areas": []}


# Heuristics for device scope. A device is "personal" if it belongs to a
# specific household_member (phone, watch, laptop, ear-buds), and "home" if
# it's a shared appliance / fixture (TV, washer, lights, locks, climate).
PERSONAL_TYPE_PREFIXES = ("device.phone", "device.tablet", "device.laptop", "device.watch")
HOME_TYPES = {
    "device.tv", "device.monitor", "device.climate", "device.light",
    "device.cover", "device.lock", "device.camera", "device.vacuum",
    "device.doorbell", "appliance.washer", "appliance.dryer",
    "appliance.dishwasher", "appliance.vacuum", "sensor.motion",
}
PERSONAL_KEYWORDS = (
    "iphone", "ipad", "macbook", "watch", "airpods", "phone", "tablet",
    "laptop", "_pc", "saeed_pc", "judes_laptop",
)


def _scope_for_thing(
    thing_type: str,
    entity_ids: list[str],
    friendly_name: str,
    manufacturer: str | None = None,
) -> str:
    """Returns 'personal' or 'home'. Heuristic — can be overridden via
    POST /admin/devices/{id}/owner which writes a definitive owner."""
    haystack = (friendly_name + " " + " ".join(entity_ids) + " " + (manufacturer or "")).lower()
    if any(kw in haystack for kw in PERSONAL_KEYWORDS):
        return "personal"
    if any(thing_type.startswith(p) for p in PERSONAL_TYPE_PREFIXES):
        return "personal"
    if thing_type in HOME_TYPES:
        return "home"
    if any(eid.startswith("device_tracker.") or eid.startswith("person.") for eid in entity_ids):
        return "personal"
    # Default: shared/home.
    return "home"


def _guess_owner(
    friendly_name: str,
    entity_ids: list[str],
    members: list[dict[str, Any]],
) -> int | None:
    """Match a device to a household_member by name overlap. Returns the
    member's id, or None if nothing confidently matches."""
    haystack = (friendly_name + " " + " ".join(entity_ids)).lower()
    best: tuple[int, int | None] = (0, None)  # (score, member_id)
    for m in members:
        name = str(m.get("name") or "").lower().strip()
        if not name:
            continue
        norm = _normalize_name(name)
        if norm in haystack or name in haystack:
            score = 2 if name in haystack else 1
            if score > best[0]:
                best = (score, m.get("id"))
    return best[1]


def _device_friendly_name(device: dict[str, Any], fallback_entity_id: str) -> str:
    """User-editable name wins, then auto-name, then the entity_id."""
    for key in ("name_by_user", "name"):
        v = device.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return fallback_entity_id


def _classify_device(
    entity_ids: list[str],
    state_by_eid: dict[str, dict[str, Any]],
    device: dict[str, Any] | None,
) -> tuple[str | None, str]:
    """Given a device + all its child entity_ids, return (thing_type, primary_eid).

    Strategy:
      1. Look for a "primary" entity by domain — climate.* / vacuum.* / lock.* /
         light.* / cover.* / camera.* control the device, so they're the
         natural primary.
      2. Otherwise look for the appliance-state sensors used by observers.
      3. Otherwise look for motion / occupancy / doorbell signals.
      4. Fall back to per-entity classification with the first matching one.
    """
    by_domain: dict[str, list[str]] = {}
    for eid in entity_ids:
        domain = eid.split(".", 1)[0] if "." in eid else ""
        by_domain.setdefault(domain, []).append(eid)

    # Tier 1: domains that represent the device itself (controllable surfaces).
    DOMAIN_PRIORITY = (
        ("climate", "device.climate"),
        ("vacuum", "device.vacuum"),
        ("lock", "device.lock"),
        ("camera", "device.camera"),
        ("light", "device.light"),
    )
    for domain, type_ in DOMAIN_PRIORITY:
        if by_domain.get(domain):
            return type_, sorted(by_domain[domain])[0]

    # Tier 2: media_player needs name-based disambiguation (TV vs monitor vs speaker).
    for eid in by_domain.get("media_player", []):
        fn = (state_by_eid.get(eid, {}).get("attributes") or {}).get("friendly_name", "")
        haystack = (eid + " " + fn).lower()
        if any(kw in haystack for kw in MONITOR_KEYWORDS):
            return "device.monitor", eid
        if any(kw in haystack for kw in TV_KEYWORDS):
            return "device.tv", eid

    # Tier 3: observer-relevant appliance state sensors.
    for eid in entity_ids:
        for substr, type_ in ADOPT_PATTERNS:
            if substr in eid:
                return type_, eid

    # Tier 4: presence / doorbell signals.
    for eid in by_domain.get("binary_sensor", []):
        if "motion" in eid or "occupancy" in eid:
            return "sensor.motion", eid
    for eid in by_domain.get("event", []):
        fn = (state_by_eid.get(eid, {}).get("attributes") or {}).get("friendly_name", "")
        if "doorbell" in (eid + " " + fn).lower():
            return "device.doorbell", eid

    # Tier 5: covers (curtains/shades/blinds).
    for eid in by_domain.get("cover", []):
        fn = (state_by_eid.get(eid, {}).get("attributes") or {}).get("friendly_name", "")
        haystack = (eid + " " + fn).lower()
        if any(kw in haystack for kw in ("curtain", "shade", "blind")):
            return "device.cover", eid

    # Nothing matched — caller skips this device.
    return None, ""


async def discover_proposal(*, knowledge_graph: Any) -> dict[str, Any]:
    """Build the auto-setup proposal: what to adopt + member tracker links.

    Now device-aware: if HA's device registry is reachable, group entities
    by their ``device_id`` and propose one ``thing`` per *device* with all
    its child entity_ids. Falls back to per-entity classification (the old
    behavior) if the registry can't be fetched.
    """
    states = await _ha_states()
    registries = await _ha_registries()
    things = await knowledge_graph.list_things() if knowledge_graph else []
    members = await knowledge_graph.list_members(include_pets=False) if knowledge_graph else []

    adopted_eids: set[str] = set()
    for t in things or []:
        for eid in (t.get("attributes") or {}).get("entity_id_list", []):
            adopted_eids.add(eid)
        for eid in (t.get("ha_entity_ids") or []):
            adopted_eids.add(eid)
        single = (t.get("attributes") or {}).get("entity_id")
        if single:
            adopted_eids.add(single)

    state_by_eid: dict[str, dict[str, Any]] = {
        s.get("entity_id", ""): s for s in states if s.get("entity_id")
    }
    things_to_adopt: list[dict[str, Any]] = []

    devices_by_id: dict[str, dict[str, Any]] = {
        d.get("id", ""): d for d in registries["devices"] if d.get("id")
    }
    entities_by_device: dict[str, list[str]] = {}
    deviceless_entities: list[str] = []
    for entity in registries["entities"]:
        eid = str(entity.get("entity_id") or "").strip()
        if not eid:
            continue
        # Skip disabled / hidden entities — HA already says they shouldn't show
        # up in user-facing surfaces.
        if entity.get("disabled_by") or entity.get("hidden_by"):
            continue
        device_id = entity.get("device_id")
        if device_id and device_id in devices_by_id:
            entities_by_device.setdefault(device_id, []).append(eid)
        else:
            deviceless_entities.append(eid)

    # ── Path 1: HA device registry available → group by device_id ──────────
    areas_by_id = {a.get("area_id", ""): a for a in registries["areas"] if a.get("area_id")}
    if entities_by_device:
        for device_id, child_eids in entities_by_device.items():
            if any(c in adopted_eids for c in child_eids):
                continue
            device = devices_by_id[device_id]
            thing_type, primary_eid = _classify_device(child_eids, state_by_eid, device)
            if thing_type is None or not primary_eid:
                continue
            extras = sorted(c for c in child_eids if c != primary_eid)
            area_id = device.get("area_id") or next(
                (
                    e.get("area_id")
                    for e in registries["entities"]
                    if e.get("device_id") == device_id and e.get("area_id")
                ),
                None,
            )
            area_name = areas_by_id.get(area_id or "", {}).get("name") if area_id else None
            things_to_adopt.append({
                "entity_id": primary_eid,
                "type": thing_type,
                "friendly_name": _device_friendly_name(device, primary_eid),
                "device_id": device_id,
                "manufacturer": device.get("manufacturer"),
                "model": device.get("model"),
                "area": area_name,
                "area_id": area_id,
                "extra_entity_ids": extras,
                "child_count": len(child_eids),
            })

        # Path 1.5: deviceless entities (template helpers, scripts, etc) get
        # the per-entity classifier so we still surface things like a
        # template washer state sensor that isn't tied to an HA device.
        for eid in deviceless_entities:
            if eid in adopted_eids:
                continue
            state = state_by_eid.get(eid, {})
            fn = (state.get("attributes") or {}).get("friendly_name", "")
            thing_type = _classify_thing(eid, fn)
            if thing_type is None:
                continue
            things_to_adopt.append({
                "entity_id": eid,
                "type": thing_type,
                "friendly_name": fn or eid,
                "device_id": None,
                "extra_entity_ids": [],
                "child_count": 1,
            })
    else:
        # ── Path 2: No registry → per-entity fallback (the old behavior) ───
        for s in states:
            eid = s.get("entity_id", "")
            if eid in adopted_eids:
                continue
            fn = (s.get("attributes") or {}).get("friendly_name", "")
            thing_type = _classify_thing(eid, fn)
            if thing_type is None:
                continue
            things_to_adopt.append({
                "entity_id": eid,
                "type": thing_type,
                "friendly_name": fn or eid,
                "device_id": None,
                "extra_entity_ids": [],
                "child_count": 1,
            })

    trackers_by_member: dict[str, list[str]] = {}
    for m in members or []:
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        suggestions = _person_entity_for(name, states)
        if suggestions:
            trackers_by_member[name] = suggestions

    # Group ambiguous duplicates (entities whose slug differs only by trailing _2 / _3).
    duplicates: list[list[str]] = []
    seen_dup: set[str] = set()
    by_root: dict[str, list[str]] = {}
    for s in states:
        eid = s.get("entity_id", "")
        domain, _, local = eid.partition(".")
        if not local:
            continue
        root = local
        while root and root.split("_")[-1].isdigit():
            root = root.rsplit("_", 1)[0]
        by_root.setdefault(f"{domain}.{root}", []).append(eid)
    for root, eids in by_root.items():
        if len(eids) > 1 and root not in seen_dup:
            duplicates.append(sorted(eids))
            seen_dup.add(root)

    return {
        "ok": True,
        "ha_total": len(states),
        "ha_devices_total": len(devices_by_id),
        "registry_available": bool(entities_by_device),
        "things_to_adopt": things_to_adopt,
        "trackers_by_member": trackers_by_member,
        "duplicates": duplicates,
        "members": [{"id": m.get("id"), "name": m.get("name")} for m in members or []],
    }


def _parse_hhmm(value: Any) -> Any:
    """``put_member`` wants a datetime.time for sleep_time/wake_time; list_members
    returns them as 'HH:MM' strings or already-time objects. Tolerate both."""
    if value is None:
        return None
    if hasattr(value, "hour"):
        return value
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        from datetime import time as _time

        if len(parts) >= 2:
            return _time(int(parts[0]), int(parts[1]))
    except (ValueError, TypeError):
        return None
    return None


async def apply_proposal(
    *,
    proposal: dict[str, Any],
    knowledge_graph: Any,
) -> dict[str, Any]:
    """Apply a proposal — idempotent (skips already-adopted, no-ops empty links)."""
    adopted: list[str] = []
    skipped: list[str] = []
    member_updates: list[dict[str, Any]] = []

    things_to_adopt = proposal.get("things_to_adopt") or []
    trackers_by_member = proposal.get("trackers_by_member") or {}

    if knowledge_graph is not None:
        existing_things = await knowledge_graph.list_things()
        members_for_scope = await knowledge_graph.list_members(include_pets=False) or []
        existing_eids: set[str] = set()
        for t in existing_things or []:
            attrs = t.get("attributes") or {}
            for e in attrs.get("entity_id_list", []):
                existing_eids.add(e)
            single = attrs.get("entity_id")
            if single:
                existing_eids.add(single)

        for thing in things_to_adopt:
            eid = str(thing.get("entity_id") or "").strip()
            if not eid or eid in existing_eids:
                skipped.append(eid)
                continue
            extras_raw = thing.get("extra_entity_ids") or []
            extras = [str(e).strip() for e in extras_raw if isinstance(e, str) and e.strip()]
            all_entity_ids = [eid] + [e for e in extras if e != eid]
            attrs: dict[str, Any] = {
                "entity_id": eid,
                "entity_id_list": all_entity_ids,
            }
            if thing.get("device_id"):
                attrs["ha_device_id"] = thing["device_id"]
            if thing.get("manufacturer"):
                attrs["manufacturer"] = thing["manufacturer"]
            if thing.get("model"):
                attrs["model"] = thing["model"]
            if thing.get("area"):
                attrs["area"] = thing["area"]
            if thing.get("area_id"):
                attrs["area_id"] = thing["area_id"]
            scope = _scope_for_thing(
                thing_type=str(thing.get("type") or ""),
                entity_ids=all_entity_ids,
                friendly_name=str(thing.get("friendly_name") or ""),
                manufacturer=thing.get("manufacturer"),
            )
            attrs["scope"] = scope
            if scope == "personal":
                guess = _guess_owner(
                    friendly_name=str(thing.get("friendly_name") or ""),
                    entity_ids=all_entity_ids,
                    members=members_for_scope,
                )
                if guess is not None:
                    attrs["owner_member_id"] = int(guess)
            try:
                await knowledge_graph.put_thing(
                    type=str(thing.get("type") or ""),
                    friendly_name=str(thing.get("friendly_name") or eid),
                    attributes=attrs,
                    ha_entity_ids=all_entity_ids,
                    photo_path=None,
                    confidence=0.9,
                    source="auto_setup",
                )
                adopted.append(eid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto_setup_adopt_failed", entity_id=eid, error=str(exc))

        members = await knowledge_graph.list_members(include_pets=True)
        by_name = {str(m.get("name") or "").lower(): m for m in members or []}
        for member_name, eids in trackers_by_member.items():
            target = by_name.get(member_name.lower())
            if target is None:
                continue
            attrs = dict(target.get("attributes") or {})
            attrs["tracker_entity_ids"] = list(eids)
            try:
                await knowledge_graph.put_member(
                    member_id=int(target.get("id") or 0),
                    name=str(target.get("name") or ""),
                    role=str(target.get("role") or "adult"),
                    telegram_chat_id=target.get("telegram_chat_id"),
                    allergies=list(target.get("allergies") or []),
                    dietary_restrictions=list(target.get("dietary_restrictions") or []),
                    sleep_time=_parse_hhmm(target.get("sleep_time")),
                    wake_time=_parse_hhmm(target.get("wake_time")),
                    attributes=attrs,
                )
                member_updates.append({"name": member_name, "tracker_entity_ids": list(eids)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto_setup_link_failed", member=member_name, error=str(exc))

    return {
        "ok": True,
        "adopted": adopted,
        "adopted_count": len(adopted),
        "skipped_already_adopted": len(skipped),
        "member_updates": member_updates,
    }


async def consolidate_by_device(*, knowledge_graph: Any) -> dict[str, Any]:
    """Walk the existing things and merge sibling HA entities into the same
    thing using HA's device registry as the source of truth.

    Use case: the registry-aware discovery added in 2026-05 needs to fix up
    the things that were created BEFORE registry-grouping existed (one row
    per HA entity, even though many of them are siblings of the same
    physical device).

    Idempotent: rows that are already device-grouped (have
    attributes.ha_device_id) are left alone. Rows whose entity_id no longer
    exists in HA are also left alone (could be a renamed entity).

    Returns ``{"ok": True, "updated": [...], "merged_into": [...],
    "untouched": N}``.
    """
    if knowledge_graph is None:
        return {"ok": False, "error": "knowledge_graph unavailable"}
    registries = await _ha_registries()
    if not registries["entities"]:
        return {"ok": False, "error": "HA registry not reachable"}

    # entity_id → device_id (skip disabled/hidden)
    eid_to_device: dict[str, str] = {}
    for entity in registries["entities"]:
        eid = str(entity.get("entity_id") or "").strip()
        if not eid:
            continue
        if entity.get("disabled_by") or entity.get("hidden_by"):
            continue
        device_id = entity.get("device_id")
        if device_id:
            eid_to_device[eid] = device_id

    devices_by_id = {d.get("id", ""): d for d in registries["devices"] if d.get("id")}
    areas_by_id = {a.get("area_id", ""): a for a in registries["areas"] if a.get("area_id")}

    # device_id → all of its entity_ids (HA's view, not ours)
    device_to_eids: dict[str, list[str]] = {}
    for eid, device_id in eid_to_device.items():
        device_to_eids.setdefault(device_id, []).append(eid)

    things = await knowledge_graph.list_things() or []
    members = await knowledge_graph.list_members(include_pets=False) or []

    # First pass: bucket existing things by their device_id (if discoverable).
    # We re-process even already-grouped rows so the migration can backfill
    # scope/area/owner fields added in later versions.
    things_by_device: dict[str, list[dict[str, Any]]] = {}
    untouched: list[str] = []
    for t in things:
        attrs = t.get("attributes") or {}
        eids = list(t.get("ha_entity_ids") or [])
        single = attrs.get("entity_id")
        if single and single not in eids:
            eids.append(single)
        # Pick the device id either from existing attributes (already grouped)
        # or by looking up any of its entity_ids in HA's registry.
        device_id = attrs.get("ha_device_id")
        if not device_id:
            device_ids = {eid_to_device.get(eid) for eid in eids} - {None}
            if len(device_ids) != 1:
                # Multi-device thing (user crafted by hand) or unknown to HA.
                untouched.append(single or (eids[0] if eids else ""))
                continue
            device_id = next(iter(device_ids))
        if device_id is None or device_id not in devices_by_id:
            untouched.append(single or (eids[0] if eids else ""))
            continue
        things_by_device.setdefault(device_id, []).append(t)

    updated: list[dict[str, Any]] = []
    merged_into: list[dict[str, Any]] = []

    for device_id, group in things_by_device.items():
        all_ha_eids = sorted(set(device_to_eids.get(device_id, [])))
        device = devices_by_id.get(device_id, {})
        if not all_ha_eids:
            continue

        # Pick the canonical thing to keep: prefer the one whose primary
        # entity is "controllable" (climate / vacuum / lock / camera / light),
        # otherwise the first one alphabetically. Other things in the same
        # device-group get deleted (their entity_ids fold into the keeper).
        def _rank(thing: dict[str, Any]) -> tuple[int, str]:
            primary = str((thing.get("attributes") or {}).get("entity_id") or "")
            domain = primary.split(".", 1)[0] if "." in primary else ""
            priority = {
                "climate": 0, "vacuum": 1, "lock": 2, "camera": 3,
                "light": 4, "cover": 5, "media_player": 6,
            }.get(domain, 99)
            return (priority, primary)
        keeper = sorted(group, key=_rank)[0]
        keeper_attrs = dict(keeper.get("attributes") or {})
        keeper_attrs["entity_id_list"] = all_ha_eids
        keeper_attrs["ha_device_id"] = device_id
        if device.get("manufacturer"):
            keeper_attrs["manufacturer"] = device["manufacturer"]
        if device.get("model"):
            keeper_attrs["model"] = device["model"]
        # Area: prefer the device's area, fall back to any entity's area.
        area_id = device.get("area_id") or next(
            (
                e.get("area_id")
                for e in registries["entities"]
                if e.get("device_id") == device_id and e.get("area_id")
            ),
            None,
        )
        if area_id and area_id in areas_by_id:
            keeper_attrs["area_id"] = area_id
            keeper_attrs["area"] = areas_by_id[area_id].get("name", "")
        # Personal vs home + best-guess owner. Existing owner_member_id wins
        # so a user who manually re-assigned via the dashboard isn't undone.
        keeper_attrs["scope"] = _scope_for_thing(
            thing_type=str(keeper.get("type") or ""),
            entity_ids=all_ha_eids,
            friendly_name=str(keeper.get("friendly_name") or ""),
            manufacturer=device.get("manufacturer"),
        )
        keeper_name = _device_friendly_name(
            device, str(keeper.get("friendly_name") or "")
        ) or keeper.get("friendly_name") or "device"
        owner_member_id = keeper.get("owner_member_id")
        if owner_member_id is None and keeper_attrs["scope"] == "personal":
            owner_member_id = _guess_owner(
                friendly_name=str(keeper_name),
                entity_ids=all_ha_eids,
                members=members,
            )
        if owner_member_id is not None:
            keeper_attrs["owner_member_id"] = int(owner_member_id)

        try:
            await knowledge_graph.put_thing(
                thing_id=int(keeper.get("id")) if keeper.get("id") is not None else None,
                type=str(keeper.get("type") or ""),
                friendly_name=str(keeper_name),
                attributes=keeper_attrs,
                ha_entity_ids=all_ha_eids,
                photo_path=keeper.get("photo_path"),
                confidence=float(keeper.get("confidence") or 0.9),
                source=str(keeper.get("source") or "auto_setup"),
            )
            updated.append({
                "device_id": device_id,
                "name": keeper_name,
                "primary": keeper_attrs.get("entity_id"),
                "entity_count": len(all_ha_eids),
                "scope": keeper_attrs.get("scope"),
                "area": keeper_attrs.get("area"),
                "owner_member_id": owner_member_id,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "consolidate_keep_failed",
                device_id=device_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue

        # Delete the non-keeper rows (they're now redundant).
        deleter = getattr(knowledge_graph, "forget_thing", None)
        for losing in group:
            if losing.get("id") == keeper.get("id"):
                continue
            losing_id = losing.get("id")
            losing_eid = (losing.get("attributes") or {}).get("entity_id")
            if deleter is None or losing_id is None:
                merged_into.append({
                    "kept_id": keeper.get("id"),
                    "would_delete_id": losing_id,
                    "would_delete_entity": losing_eid,
                    "note": "knowledge_graph has no forget_thing — left in place",
                })
                continue
            try:
                await deleter(int(losing_id))
                merged_into.append({
                    "kept_id": keeper.get("id"),
                    "deleted_id": losing_id,
                    "deleted_entity": losing_eid,
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "consolidate_delete_failed",
                    losing_id=losing_id, error=f"{type(exc).__name__}: {exc}",
                )

    return {
        "ok": True,
        "updated_count": len(updated),
        "merged_count": len(merged_into),
        "untouched_count": len(untouched),
        "updated": updated,
        "merged_into": merged_into,
    }
