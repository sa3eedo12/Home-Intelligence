from __future__ import annotations

from typing import Any

from home_agents_sdk import tool
from home_agents_sdk.knowledge_graph import KnowledgeGraph
from home_agents_sdk.telemetry import get_logger

from tools.core import _pool

logger = get_logger("knowledge_notes.registry")


async def _knowledge_graph() -> KnowledgeGraph:
    return KnowledgeGraph(pool=await _pool())


async def _store_or_error() -> KnowledgeGraph | None:
    try:
        return await _knowledge_graph()
    except Exception as exc:
        logger.warning("knowledge_graph_unavailable", error=str(exc))
        return None


@tool("things.list")
async def list_things(type: str | None = None) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"items": [], "count": 0, "error": "knowledge_graph_unavailable"}
    items = await store.list_things(type=type)
    return {"items": items, "count": len(items)}


@tool("things.put", side_effects=True)
async def put_thing(
    type: str,
    friendly_name: str,
    attributes: dict[str, Any] | None = None,
    ha_entity_ids: list[str] | None = None,
    photo_path: str | None = None,
    confidence: float = 0.5,
    source: str = "user",
) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    thing = await store.put_thing(
        type=type,
        friendly_name=friendly_name,
        attributes=attributes,
        ha_entity_ids=ha_entity_ids,
        photo_path=photo_path,
        confidence=confidence,
        source=source,
    )
    return {"ok": thing is not None, "thing": thing}


@tool("things.forget", side_effects=True)
async def forget_thing(thing_id: int) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    deleted = await store.forget_thing(thing_id)
    return {"ok": True, "deleted": deleted, "thing_id": thing_id}


@tool("things.confirm", side_effects=True)
async def confirm_thing(thing_id: int) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    thing = await store.confirm_thing(thing_id)
    return {"ok": thing is not None, "thing": thing}


@tool("habits.list")
async def list_habits(subject: str | None = None) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"items": [], "count": 0, "error": "knowledge_graph_unavailable"}
    items = await store.list_habits(subject=subject)
    return {"items": items, "count": len(items)}


@tool("habits.put", side_effects=True)
async def put_habit(
    subject: str,
    pattern: dict[str, Any],
    frequency: str | None = None,
    confidence: float = 0.5,
    source: str = "user",
    last_observed_at: str | None = None,
) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    habit = await store.put_habit(
        subject=subject,
        pattern=pattern,
        frequency=frequency,
        confidence=confidence,
        last_observed_at=last_observed_at,
        source=source,
    )
    return {"ok": habit is not None, "habit": habit}


@tool("habits.forget", side_effects=True)
async def forget_habit(habit_id: int) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    deleted = await store.forget_habit(habit_id)
    return {"ok": True, "deleted": deleted, "habit_id": habit_id}


@tool("habits.confirm", side_effects=True)
async def confirm_habit(habit_id: int) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    habit = await store.confirm_habit(habit_id)
    return {"ok": habit is not None, "habit": habit}


@tool("preferences.list")
async def list_preferences() -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"items": [], "count": 0, "error": "knowledge_graph_unavailable"}
    items = await store.list_preferences()
    return {"items": items, "count": len(items)}


@tool("preferences.put", side_effects=True)
async def put_preference(
    key: str,
    value: Any,
    confidence: float = 0.5,
    source: str = "user",
) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    preference = await store.put_preference(
        key=key,
        value=value,
        confidence=confidence,
        source=source,
    )
    return {"ok": preference is not None, "preference": preference}


@tool("preferences.forget", side_effects=True)
async def forget_preference(key: str) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    deleted = await store.forget_preference(key)
    return {"ok": True, "deleted": deleted, "key": key}


@tool("routines.list")
async def list_routines() -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"items": [], "count": 0, "error": "knowledge_graph_unavailable"}
    items = await store.list_routines()
    return {"items": items, "count": len(items)}


@tool("routines.put", side_effects=True)
async def put_routine(
    name: str,
    steps: list[Any] | dict[str, Any],
    schedule: dict[str, Any] | None = None,
    source: str = "user",
    last_run_at: str | None = None,
) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    routine = await store.put_routine(
        name=name,
        steps=steps,
        schedule=schedule,
        last_run_at=last_run_at,
        source=source,
    )
    return {"ok": routine is not None, "routine": routine}


@tool("routines.forget", side_effects=True)
async def forget_routine(routine_id: int) -> dict[str, Any]:
    store = await _store_or_error()
    if store is None:
        return {"ok": False, "error": "knowledge_graph_unavailable"}
    deleted = await store.forget_routine(routine_id)
    return {"ok": True, "deleted": deleted, "routine_id": routine_id}
