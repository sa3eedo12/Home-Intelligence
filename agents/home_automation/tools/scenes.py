from __future__ import annotations

from home_agents_sdk.tools import tool

from .ha_client import get_ha_client


@tool("list_scenes")
async def list_scenes() -> list[dict]:
    client = get_ha_client()
    states = await client.list_states(domain="scene")
    return [
        {
            "entity_id": s["entity_id"],
            "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
        }
        for s in states
    ]


@tool("set_scene", side_effects=True)
async def set_scene(scene_name: str) -> dict:
    client = get_ha_client()
    scenes = await client.list_states(domain="scene")
    match = next(
        (
            s
            for s in scenes
            if s.get("attributes", {}).get("friendly_name", "").lower() == scene_name.lower()
        ),
        None,
    )
    if match is None:
        return {"ok": False, "error": f"Scene '{scene_name}' not found"}
    entity_id = match["entity_id"]
    name = match.get("attributes", {}).get("friendly_name") or entity_id
    result = await client.call_service("scene", "turn_on", {"entity_id": entity_id})
    affected = [{"entity_id": entity_id, "name": name}]
    return {
        "ok": True,
        "scene": entity_id,
        "result": result,
        "affected_entities": affected,
        "affected_names": [name],
    }
