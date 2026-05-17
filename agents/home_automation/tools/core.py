from __future__ import annotations

from typing import Any

from home_agents_sdk.tools import tool

from .ha_client import get_ha_client


def _coerce_entity_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list | tuple | set):
        entity_ids: list[str] = []
        for item in value:
            entity_ids.extend(_coerce_entity_ids(item))
        return entity_ids
    return []


def _extract_entity_ids(data: dict[str, Any]) -> list[str]:
    entity_ids = _coerce_entity_ids(data.get("entity_id"))
    target = data.get("target")
    if isinstance(target, dict):
        entity_ids.extend(_coerce_entity_ids(target.get("entity_id")))

    deduped: list[str] = []
    for entity_id in entity_ids:
        if entity_id not in deduped:
            deduped.append(entity_id)
    return deduped


def _friendly_name_from_state(state: dict[str, Any]) -> str:
    attrs = state.get("attributes", {}) or {}
    return attrs.get("friendly_name") or state.get("entity_id", "unknown")


def _affected_from_service_result(result: Any) -> list[dict[str, str]]:
    if not isinstance(result, list):
        return []
    affected: list[dict[str, str]] = []
    for item in result:
        if not isinstance(item, dict) or not item.get("entity_id"):
            continue
        entity_id = item["entity_id"]
        affected.append({"entity_id": entity_id, "name": _friendly_name_from_state(item)})
    return affected


async def _describe_affected_entities(client: Any, entity_ids: list[str]) -> list[dict[str, str]]:
    affected: list[dict[str, str]] = []
    for entity_id in entity_ids:
        try:
            state = await client.resolve_entity(entity_id)
        except Exception:
            state = None
        name = _friendly_name_from_state(state) if state else entity_id
        affected.append({"entity_id": entity_id, "name": name})
    return affected


@tool("list_entities")
async def list_entities(
    domain: str | None = None, include_unavailable: bool = False
) -> dict:
    """Return entities grouped by area, with friendly names. Defaults to
    hiding unavailable entities so the response stays signal-rich.
    """
    client = get_ha_client()
    items = await client.list_states_enriched(
        domain=domain, include_unavailable=include_unavailable
    )
    by_area: dict[str, list[dict]] = {}
    hidden_unavailable = 0
    for item in items:
        area = item.get("area") or "Unassigned"
        by_area.setdefault(area, []).append(
            {"name": item["name"], "state": item["state"], "entity_id": item["entity_id"]}
        )
    if not include_unavailable:
        # Re-fetch with unavailable to count what's hidden, so the humanizer
        # can mention them. Cheap because it hits the same /api/template.
        try:
            full = await client.list_states_enriched(
                domain=domain, include_unavailable=True
            )
            hidden_unavailable = sum(
                1 for it in full if it["state"] in ("unavailable", "unknown")
            )
        except Exception:
            hidden_unavailable = 0
    return {
        "domain": domain,
        "by_area": by_area,
        "total_visible": sum(len(v) for v in by_area.values()),
        "hidden_unavailable": hidden_unavailable,
    }


@tool("get_entity_state")
async def get_entity_state(entity_id: str) -> dict:
    client = get_ha_client()
    return await client.get_state(entity_id)


@tool("search_entities")
async def search_entities(
    query: str,
    domain: str | None = None,
    include_unavailable: bool = False,
    limit: int = 30,
) -> dict:
    """Substring search across HA entities by entity_id AND
    friendly_name. Use this when the user mentions a specific thing
    by colloquial name (e.g. "car", "lock", "vacuum") and you need
    to find the matching entity_id without scanning hundreds of
    entities. Much higher signal-per-token than list_entities for
    discovery use cases.

    Args:
        query: case-insensitive substring matched against both
            entity_id and friendly_name. Examples: "car", "battery",
            "lock", "han", "vacuum".
        domain: optional HA domain filter (sensor, switch, climate,
            etc.). When omitted, searches across all domains — useful
            when you don't yet know which domain the thing lives in.
        include_unavailable: by default unavailable entities are
            excluded so the response stays signal-rich.
        limit: cap on number of hits (default 30, max 200).

    Returns ``{query, total_matched, hits: [{entity_id, name, area,
    state, domain}]}``.
    """
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 30
    needle = query.strip().casefold()
    if not needle:
        return {"query": query, "total_matched": 0, "hits": []}

    client = get_ha_client()
    items = await client.list_states_enriched(
        domain=domain, include_unavailable=include_unavailable
    )

    hits = []
    for item in items:
        eid = item.get("entity_id") or ""
        name = item.get("name") or ""
        if needle in eid.casefold() or needle in name.casefold():
            hits.append({
                "entity_id": eid,
                "name": name,
                "area": item.get("area") or "Unassigned",
                "state": item.get("state"),
                "domain": eid.split(".", 1)[0] if "." in eid else "",
            })
    return {
        "query": query,
        "total_matched": len(hits),
        "hits": hits[:limit],
        "truncated": len(hits) > limit,
    }


@tool("call_service", side_effects=True)
async def call_service(domain: str, service: str, data: dict) -> dict:
    client = get_ha_client()
    result = await client.call_service(domain, service, data)
    entity_ids = _extract_entity_ids(data)
    affected = await _describe_affected_entities(client, entity_ids)
    if not affected:
        affected = _affected_from_service_result(result)
    return {
        "ok": True,
        "result": result,
        "affected_entities": affected,
        "affected_names": [entity["name"] for entity in affected],
    }
