from __future__ import annotations

import ast
import json
import re
from typing import Any

from home_agents_sdk.tools import tool

from .ha_client import get_ha_client


def _parse_template_list(rendered: str) -> list[Any]:
    text = rendered.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(text)
    return parsed if isinstance(parsed, list) else []


def _area_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _normalize_area_item(area: Any) -> dict[str, Any] | None:
    if isinstance(area, dict):
        name = area.get("name") or area.get("area_id") or area.get("id")
        if name:
            return {**area, "name": str(name)}
    elif area not in (None, ""):
        return {"name": str(area)}
    return None


def _area_entities_template(area_name: str) -> str:
    escaped = area_name.replace("\\", "\\\\").replace("'", "\\'")
    return "{{ area_entities('" + escaped + "') | list }}"


async def _resolve_area_name(client: Any, requested_area: str) -> str:
    requested = requested_area.strip()
    requested_key = _area_key(requested)

    try:
        states = await client.list_states_enriched(include_unavailable=True)
        seen: dict[str, str] = {}
        for state in states:
            area = str(state.get("area") or "").strip()
            if area and area != "Unassigned":
                seen.setdefault(_area_key(area), area)
        if requested_key in seen:
            return seen[requested_key]
    except Exception:
        pass

    try:
        for area in await client.get_areas():
            name = str(area.get("name") or "").strip()
            if name and _area_key(name) == requested_key:
                return name
    except Exception:
        pass

    return requested.title()


@tool("list_areas")
async def list_areas() -> dict:
    client = get_ha_client()
    try:
        rendered = await client.render_template("{{ areas() | list }}")
        raw_areas = _parse_template_list(rendered)
    except Exception as exc:
        return {"areas": [], "count": 0, "error": str(exc)}

    areas = [area for area in (_normalize_area_item(item) for item in raw_areas) if area]
    return {"areas": areas, "count": len(areas)}


@tool("call_service_in_area", side_effects=True)
async def call_service_in_area(
    area: str, domain: str, service: str, data: dict | None = None
) -> dict:
    client = get_ha_client()
    area_name = await _resolve_area_name(client, area)
    payload_base = dict(data or {})
    errors: list[dict[str, str]] = []

    try:
        rendered = await client.render_template(_area_entities_template(area_name))
        entity_ids = _parse_template_list(rendered)
    except Exception as exc:
        return {
            "area": area_name,
            "domain": domain,
            "service": service,
            "target_count": 0,
            "errors": [{"area": area_name, "error": str(exc)}],
        }

    targets = [
        entity_id
        for entity_id in entity_ids
        if isinstance(entity_id, str) and entity_id.startswith(f"{domain}.")
    ]
    for entity_id in targets:
        payload = {**payload_base, "entity_id": entity_id}
        try:
            await client.call_service(domain, service, payload)
        except Exception as exc:
            errors.append({"entity_id": entity_id, "error": str(exc)})

    return {
        "area": area_name,
        "domain": domain,
        "service": service,
        "target_count": len(targets),
        "errors": errors,
    }
