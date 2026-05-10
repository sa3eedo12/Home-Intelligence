from __future__ import annotations

from home_agents_sdk.tools import tool

from .ha_client import get_ha_client


@tool("list_entities")
async def list_entities(domain: str | None = None) -> list[dict]:
    client = get_ha_client()
    states = await client.list_states(domain=domain)
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "domain": s["entity_id"].split(".")[0],
        }
        for s in states
    ]


@tool("get_entity_state")
async def get_entity_state(entity_id: str) -> dict:
    client = get_ha_client()
    return await client.get_state(entity_id)


@tool("call_service", side_effects=True)
async def call_service(domain: str, service: str, data: dict) -> dict:
    client = get_ha_client()
    result = await client.call_service(domain, service, data)
    return {"ok": True, "result": result}
