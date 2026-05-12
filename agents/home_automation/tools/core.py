from __future__ import annotations

from home_agents_sdk.tools import tool

from .ha_client import get_ha_client


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


@tool("call_service", side_effects=True)
async def call_service(domain: str, service: str, data: dict) -> dict:
    client = get_ha_client()
    result = await client.call_service(domain, service, data)
    return {"ok": True, "result": result}
