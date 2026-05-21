"""Seed the knowledge graph from the structured baseline tables.

Idempotent — uses ``external_ref`` upserts so re-running just refreshes
existing nodes/edges in place rather than duplicating them. Safe to call
at every orchestrator startup.

What gets seeded:

- ``household_members`` → ``person`` nodes (external_ref ``household_members:<id>``)
- ``things``            → ``device`` nodes (external_ref ``things:<id>``)
- ``preferences``       → ``preference`` nodes (external_ref ``preferences:<key>``)
- ``user_profile``      → ``profile_fact`` nodes (external_ref ``user_profile:<key>``)
- ``things.owner_member_id`` → ``OWNS`` edge from person to device
- ``things.attributes['area']`` (if present) → ``LOCATED_IN`` edge from device
  to an auto-created ``area`` node (one per distinct area label)
"""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from .knowledge_graph_store import KnowledgeGraphStore

logger = logging.getLogger(__name__)


def _attrs_to_dict(value: Any) -> dict[str, Any]:
    """Normalise jsonb columns (str or dict) into a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def seed_from_baseline(
    pool: asyncpg.Pool | None,
    *,
    store: KnowledgeGraphStore | None = None,
) -> dict[str, int]:
    """Seed nodes + edges from the structured tables.

    Returns counts ``{persons, devices, preferences, profile_facts,
    owns_edges, located_edges}`` for logging / dashboards.
    """
    counts = {
        "persons": 0,
        "devices": 0,
        "preferences": 0,
        "profile_facts": 0,
        "owns_edges": 0,
        "located_edges": 0,
    }
    if pool is None:
        return counts
    store = store or KnowledgeGraphStore(pool)

    # ── Persons ────────────────────────────────────────────────────
    member_to_node: dict[int, int] = {}
    async with pool.acquire() as conn:
        members = await conn.fetch(
            "SELECT id, name, role, telegram_chat_id, sleep_time, wake_time, "
            "attributes FROM household_members"
        )
    for m in members:
        attrs = _attrs_to_dict(m["attributes"])
        attrs.update({
            "role": m["role"],
            "telegram_chat_id": m["telegram_chat_id"],
            "sleep_time": str(m["sleep_time"]) if m["sleep_time"] else None,
            "wake_time": str(m["wake_time"]) if m["wake_time"] else None,
        })
        node_id = await store.upsert_node(
            type="person",
            label=m["name"],
            attributes=attrs,
            external_ref=f"household_members:{m['id']}",
            source="seeder:household_members",
        )
        if node_id is not None:
            member_to_node[int(m["id"])] = node_id
            counts["persons"] += 1

    # ── Devices + OWNS edges + LOCATED_IN edges ────────────────────
    async with pool.acquire() as conn:
        things = await conn.fetch(
            "SELECT id, type, friendly_name, attributes, ha_entity_ids, "
            "owner_member_id, source FROM things"
        )
    area_to_node: dict[str, int] = {}
    for t in things:
        thing_attrs = _attrs_to_dict(t["attributes"])
        device_attrs = dict(thing_attrs)
        device_attrs["device_type"] = t["type"]
        device_attrs["ha_entity_ids"] = list(t["ha_entity_ids"] or [])
        device_node_id = await store.upsert_node(
            type="device",
            label=t["friendly_name"],
            attributes=device_attrs,
            external_ref=f"things:{t['id']}",
            source=f"seeder:things({t['source']})" if t["source"] else "seeder:things",
        )
        if device_node_id is None:
            continue
        counts["devices"] += 1

        owner_id = t["owner_member_id"]
        if owner_id is not None and owner_id in member_to_node:
            edge_id = await store.upsert_edge(
                source_node_id=member_to_node[owner_id],
                target_node_id=device_node_id,
                rel_type="OWNS",
                source="seeder:things.owner_member_id",
            )
            if edge_id is not None:
                counts["owns_edges"] += 1

        # Area is stored as attributes->>'area' when known.
        area_label = thing_attrs.get("area")
        if isinstance(area_label, str) and area_label.strip():
            area_key = area_label.strip().casefold()
            area_node_id = area_to_node.get(area_key)
            if area_node_id is None:
                area_node_id = await store.upsert_node(
                    type="area",
                    label=area_label.strip(),
                    external_ref=f"area:{area_key}",
                    source="seeder:things.attributes.area",
                )
                if area_node_id is None:
                    continue
                area_to_node[area_key] = area_node_id
            edge_id = await store.upsert_edge(
                source_node_id=device_node_id,
                target_node_id=area_node_id,
                rel_type="LOCATED_IN",
                source="seeder:things.attributes.area",
            )
            if edge_id is not None:
                counts["located_edges"] += 1

    # ── Preferences ───────────────────────────────────────────────
    async with pool.acquire() as conn:
        prefs = await conn.fetch(
            "SELECT key, value, confidence, source FROM preferences"
        )
    for p in prefs:
        attrs = {
            "value": _attrs_to_dict(p["value"]) or p["value"],
            "source": p["source"],
        }
        node_id = await store.upsert_node(
            type="preference",
            label=p["key"],
            attributes=attrs,
            external_ref=f"preferences:{p['key']}",
            source="seeder:preferences",
            confidence=float(p["confidence"] or 0.0),
        )
        if node_id is not None:
            counts["preferences"] += 1

    # ── User profile facts ────────────────────────────────────────
    async with pool.acquire() as conn:
        profile_rows = await conn.fetch(
            "SELECT key, value, confidence, source FROM user_profile"
        )
    for p in profile_rows:
        attrs = {
            "value": _attrs_to_dict(p["value"]) or p["value"],
            "source": p["source"],
        }
        node_id = await store.upsert_node(
            type="profile_fact",
            label=p["key"],
            attributes=attrs,
            external_ref=f"user_profile:{p['key']}",
            source="seeder:user_profile",
            confidence=float(p["confidence"] or 0.0),
        )
        if node_id is not None:
            counts["profile_facts"] += 1

    logger.info("knowledge_graph_seeder.completed counts=%s", counts)
    return counts
