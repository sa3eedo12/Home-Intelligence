from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger

from .admin import build_onboarding_state
from .data_science.common import current_embedding_model, decode_json
from .observers.utils import APPLIANCE_SYNONYMS

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
logger = get_logger("orchestrator.dashboard")

DISCOVERY_TYPES = [
    "appliance.washer",
    "appliance.dryer",
    "appliance.vacuum",
    "appliance.dishwasher",
    "appliance.oven",
    "appliance.coffee_maker",
    "vehicle.car",
    "person.member",
    "room",
    "pet.dog",
    "pet.cat",
    "light",
    "sensor",
    "media_player",
    "other",
]


async def _status(request: Request) -> dict[str, Any]:
    provider = getattr(request.app.state, "status_provider", None)
    if provider is None:
        return {"reflection": {"last_run_at": None, "age_hours": None, "healthy": False}}
    status = await provider()
    status.setdefault("reflection", {"last_run_at": None, "age_hours": None, "healthy": False})
    return status


def _reflection_store(request: Request) -> Any:
    store = getattr(request.app.state, "reflection_store", None)
    if store is not None:
        return store
    return ReflectionStore(getattr(request.app.state, "pool", None))


async def _data_science_snapshot(request: Request) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "reports": [],
        "maintenance_runs": [],
        "last_lora_run": None,
        "embedding": {"current_model": _current_embedding_model(request), "stale_event_count": 0},
    }
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return snapshot
    try:
        async with pool.acquire() as conn:
            report_rows = await conn.fetch(
                """
                SELECT kind, period_label, file_path, summary, generated_at
                FROM reports
                ORDER BY generated_at DESC
                LIMIT 10
                """
            )
            maintenance_rows = await conn.fetch(
                """
                SELECT ts, summary, payload
                FROM event_log
                WHERE agent = 'data_science'
                  AND capability = 'maintenance'
                ORDER BY ts DESC
                LIMIT 7
                """
            )
            lora_row = await conn.fetchrow(
                """
                SELECT id, started_at, finished_at, status, model_base,
                       training_file, quality_score, error
                FROM lora_training_runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            stale_count = await conn.fetchval(
                """
                SELECT count(*)
                FROM event_log
                WHERE embedding_model IS DISTINCT FROM $1
                """,
                snapshot["embedding"]["current_model"],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_science_dashboard_snapshot_failed", error=str(exc))
        return snapshot

    snapshot["reports"] = [_dashboard_row(row) for row in report_rows]
    snapshot["maintenance_runs"] = [_maintenance_row(row) for row in maintenance_rows]
    snapshot["last_lora_run"] = _dashboard_row(lora_row) if lora_row else None
    snapshot["embedding"]["stale_event_count"] = int(stale_count or 0)
    return snapshot


def _current_embedding_model(request: Request) -> str:
    reembed = getattr(request.app.state, "reembed", None)
    value = getattr(reembed, "current_model", None)
    if value:
        return str(value)
    return current_embedding_model(getattr(request.app.state, "embedder", None))


def _dashboard_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


def _maintenance_row(row: Any) -> dict[str, Any]:
    data = _dashboard_row(row)
    payload = decode_json(data.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    return {
        "ts": data.get("ts"),
        "summary": data.get("summary"),
        "status": payload.get("status") or "ok",
        "archived_rows": payload.get("archived_rows", 0),
        "errors": payload.get("errors") or [],
    }


async def _about_you_snapshot(request: Request) -> dict[str, list[dict[str, Any]]]:
    empty: dict[str, list[dict[str, Any]]] = {
        "things": [],
        "habits": [],
        "preferences": [],
        "routines": [],
    }
    knowledge_graph = getattr(request.app.state, "knowledge_graph", None)
    if knowledge_graph is None:
        return empty
    try:
        things, habits, preferences, routines = await asyncio.gather(
            knowledge_graph.list_things(),
            knowledge_graph.list_habits(),
            knowledge_graph.list_preferences(),
            knowledge_graph.list_routines(),
        )
    except Exception:
        return empty
    return {
        "things": things,
        "habits": habits,
        "preferences": preferences,
        "routines": routines,
    }


async def _discovery_snapshot(request: Request) -> dict[str, Any]:
    entities, things = await asyncio.gather(
        _list_ha_entities(request),
        _list_discovery_things(request),
    )
    known_entity_ids = {
        str(entity_id)
        for thing in things
        for entity_id in (thing.get("ha_entity_ids") or [])
        if entity_id
    }
    unidentified = [
        {**entity, "suggested_type": _suggest_entity_type(entity)}
        for entity in entities
        if entity.get("entity_id") not in known_entity_ids
    ]
    return {
        "entities": unidentified,
        "types": DISCOVERY_TYPES,
        "identified_count": len(known_entity_ids),
        "total_count": len(entities),
    }


async def _list_ha_entities(request: Request) -> list[dict[str, Any]]:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return []
    try:
        result = await registry.dispatch(
            "home_automation",
            "list_entities",
            {"include_unavailable": True},
        )
    except Exception as exc:
        logger.warning("discovery_list_entities_failed", error=str(exc))
        return []
    if isinstance(result, dict) and result.get("ok") is False:
        logger.warning("discovery_list_entities_failed", error=result.get("error"))
        return []
    payload = result.get("result") if isinstance(result, dict) and "result" in result else result
    return _flatten_entities(payload)


async def _list_discovery_things(request: Request) -> list[dict[str, Any]]:
    knowledge_graph = getattr(request.app.state, "knowledge_graph", None)
    if knowledge_graph is None:
        return []
    try:
        return await knowledge_graph.list_things()
    except Exception as exc:
        logger.warning("discovery_list_things_failed", error=str(exc))
        return []


def _flatten_entities(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entity for item in payload if (entity := _normalize_entity(item, None))]
    if not isinstance(payload, dict):
        return []
    by_area = payload.get("by_area")
    if isinstance(by_area, dict):
        entities: list[dict[str, Any]] = []
        for area, items in by_area.items():
            if not isinstance(items, list):
                continue
            entities.extend(
                entity for item in items if (entity := _normalize_entity(item, str(area)))
            )
        return entities
    items = payload.get("items") or payload.get("entities") or []
    return [entity for item in items if (entity := _normalize_entity(item, None))]


def _normalize_entity(item: Any, area: str | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    entity_id = str(item.get("entity_id") or "").strip()
    if not entity_id:
        return None
    friendly_name = str(
        item.get("friendly_name") or item.get("name") or attrs.get("friendly_name") or entity_id
    )
    entity_area = item.get("area") or attrs.get("area") or attrs.get("area_name") or area
    return {
        "entity_id": entity_id,
        "friendly_name": friendly_name,
        "area": entity_area or "Unassigned",
        "state": item.get("state", "unknown"),
        "attributes": attrs,
    }


def _suggest_entity_type(entity: dict[str, Any]) -> str:
    entity_id = str(entity.get("entity_id") or "")
    friendly_name = str(entity.get("friendly_name") or "")
    haystack = f"{entity_id} {friendly_name}".casefold()
    appliance_types = {
        "washer": "appliance.washer",
        "dryer": "appliance.dryer",
        "vacuum": "appliance.vacuum",
        "dishwasher": "appliance.dishwasher",
        "oven": "appliance.oven",
        "coffee": "appliance.coffee_maker",
    }
    for appliance, thing_type in appliance_types.items():
        if any(needle.casefold() in haystack for needle in APPLIANCE_SYNONYMS.get(appliance, [])):
            return thing_type
    domain = entity_id.split(".", 1)[0]
    if domain == "light":
        return "light"
    if domain in {"sensor", "binary_sensor"}:
        return "sensor"
    if domain == "media_player":
        return "media_player"
    if domain == "person":
        return "person.member"
    if domain == "device_tracker":
        if any(token in haystack for token in ("car", "vehicle", "tesla", "bmw", "audi")):
            return "vehicle.car"
        return "person.member"
    return "other"


@router.get("/dashboard/about-you", response_class=HTMLResponse)
async def about_you(request: Request) -> HTMLResponse:
    knowledge = await _about_you_snapshot(request)
    return templates.TemplateResponse(
        request=request,
        name="about_you.html.j2",
        context={"knowledge": knowledge},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html.j2",
        context={"status": await _status(request)},
    )


@router.get("/dashboard/data-science", response_class=HTMLResponse)
async def data_science(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="data_science.html.j2",
        context={"data_science": await _data_science_snapshot(request)},
    )


@router.get("/dashboard/discovery", response_class=HTMLResponse)
async def discovery(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="discovery.html.j2",
        context={"discovery": await _discovery_snapshot(request)},
    )


@router.get("/dashboard/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request, stage: int | None = None) -> HTMLResponse:
    state = await build_onboarding_state(request, override_stage=stage)
    return templates.TemplateResponse(
        request=request,
        name="onboarding.html.j2",
        context={"onboarding": state},
    )


@router.get("/dashboard/morning-brief", response_class=HTMLResponse)
async def morning_brief(request: Request) -> HTMLResponse:
    store = _reflection_store(request)
    briefs = await store.list_briefs(limit=1)
    brief = briefs[0] if briefs else None
    body = (brief or {}).get("body_json") or {}
    proposals = await store.list_proposals(limit=50)
    if not proposals:
        proposals = body.get("proposals") or []
    reflector = getattr(request.app.state, "reflector", None)
    reflection_state = reflector.status if reflector is not None else {"running": False}
    return templates.TemplateResponse(
        request=request,
        name="morning_brief.html.j2",
        context={
            "status": await _status(request),
            "brief": brief,
            "proposals": proposals,
            "reflection_state": reflection_state,
        },
    )
