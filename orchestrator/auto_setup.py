"""GET /admin/setup/auto-discover and POST /admin/setup/auto-apply.

Closes the loop on "I have 600 HA entities, what should the system actually
adopt and link?". The auto-discover endpoint scans HA's /api/states and
returns a structured proposal:

    {
      "things_to_adopt": [{"entity_id":"...", "type":"...", "friendly_name":"..."}],
      "trackers_by_member": {"saeed": ["person.saeed", "device_tracker.saeeds_iphone"]},
      "duplicates": [["entity_id_a", "entity_id_b"]]   // suggested merges
    }

The auto-apply endpoint takes that same shape (or a user-edited version)
and applies it: adopts each thing into the registry and patches each
household_member's attributes.tracker_entity_ids.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import httpx
from home_agents_sdk.telemetry import get_logger

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


async def discover_proposal(*, knowledge_graph: Any) -> dict[str, Any]:
    """Build the auto-setup proposal: what to adopt + member tracker links."""
    states = await _ha_states()
    things = await knowledge_graph.list_things() if knowledge_graph else []
    members = await knowledge_graph.list_members(include_pets=False) if knowledge_graph else []

    adopted_eids: set[str] = set()
    for t in things or []:
        for eid in (t.get("attributes") or {}).get("entity_id_list", []):
            adopted_eids.add(eid)
        # Also match the singular entity_id if present
        single = (t.get("attributes") or {}).get("entity_id")
        if single:
            adopted_eids.add(single)

    things_to_adopt: list[dict[str, Any]] = []
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
        # Strip trailing _<digit>
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
        "things_to_adopt": things_to_adopt,
        "trackers_by_member": trackers_by_member,
        "duplicates": duplicates,
        "members": [{"id": m.get("id"), "name": m.get("name")} for m in members or []],
    }


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
            try:
                await knowledge_graph.put_thing(
                    type=str(thing.get("type") or ""),
                    friendly_name=str(thing.get("friendly_name") or eid),
                    attributes={"entity_id": eid},
                    ha_entity_ids=[eid],
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
                    sleep_time=target.get("sleep_time"),
                    wake_time=target.get("wake_time"),
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
