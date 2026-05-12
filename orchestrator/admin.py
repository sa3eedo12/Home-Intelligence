from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger

from .safety import SafetyPolicy

router = APIRouter(tags=["admin"])
logger = get_logger("orchestrator.admin")


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _reflection_store(request: Request):
    store = getattr(request.app.state, "reflection_store", None)
    if store is not None:
        return store
    return ReflectionStore(getattr(request.app.state, "pool", None))


def _format_proposal_markdown(proposal: dict[str, Any]) -> str:
    evidence = proposal.get("evidence_event_ids") or []
    lines = [
        f"# {proposal.get('title', 'Reflection proposal')}",
        "",
        f"- Kind: `{proposal.get('kind', 'unknown')}`",
        f"- Status: `{proposal.get('status', 'pending')}`",
        f"- Confidence: {float(proposal.get('confidence') or 0.0):.2f}",
        f"- Evidence event ids: {', '.join(str(item) for item in evidence) or 'n/a'}",
        "",
        "## Rationale",
        str(proposal.get("rationale") or "No rationale provided."),
        "",
        "## Implementation prompt",
        "Use the Home-Intelligence repository context. Implement the proposal above as a "
        "small, well-tested change. Cite the evidence event ids before changing code, keep "
        "the change local-first, and do not touch unrelated agent areas.",
    ]
    if proposal.get("cost_estimate"):
        lines.insert(4, f"- Cost: {proposal['cost_estimate']}")
    if proposal.get("impact_estimate"):
        lines.insert(5, f"- Impact: {proposal['impact_estimate']}")
    return "\n".join(lines)


_KNOWLEDGE_CONFIRM_METHODS = {
    "things": "confirm_thing",
    "habits": "confirm_habit",
    "preferences": "confirm_preference",
    "routines": "confirm_routine",
}
_KNOWLEDGE_FORGET_METHODS = {
    "things": "forget_thing",
    "habits": "forget_habit",
    "preferences": "forget_preference",
    "routines": "forget_routine",
}
_KNOWLEDGE_PATCH_FIELDS = {
    "things": {
        "type",
        "friendly_name",
        "attributes",
        "ha_entity_ids",
        "photo_path",
        "confidence",
        "source",
    },
    "habits": {"subject", "pattern", "frequency", "confidence", "last_observed_at", "source"},
    "preferences": {"value", "confidence", "source"},
    "routines": {"name", "steps", "schedule", "last_run_at", "source"},
}


def _knowledge_graph(request: Request) -> Any:
    graph = getattr(request.app.state, "knowledge_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
    return graph


def _required_discovery_str(body: dict[str, Any], key: str) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value


def _optional_discovery_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _knowledge_id(table: str, raw_id: Any) -> int | str:
    if table == "preferences":
        key = str(raw_id or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="id is required")
        return key
    try:
        return int(raw_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="id must be an integer") from exc


def _knowledge_table(table: Any) -> str:
    parsed = str(table or "").strip()
    if parsed not in _KNOWLEDGE_PATCH_FIELDS:
        raise HTTPException(status_code=400, detail="unknown knowledge table")
    return parsed


@router.post("/admin/reload-policies")
async def reload_policies(request: Request) -> dict:
    app_state = request.app.state
    policies = _load_yaml("orchestrator/policies.yaml")
    await app_state.policy_engine.reload(policies)
    safety = getattr(app_state, "safety", None)
    safety_reload = getattr(safety, "reload", None)
    if callable(safety_reload):
        safety_reload()
    schedules_result = await app_state.scheduler.reload()
    reactive_result = await app_state.reactive.reload()
    return {
        "ok": True,
        "policies": len(policies),
        "schedules": schedules_result.get("jobs", 0),
        "triggers": reactive_result.get("triggers", 0),
    }


@router.post("/admin/run-job/{job_id}")
async def run_job(job_id: str, request: Request) -> dict:
    try:
        result = await request.app.state.scheduler.run_job_now(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}") from exc
    return {"ok": True, "job_id": job_id, "result": result}


@router.post("/admin/reflection/run")
async def run_reflection(request: Request) -> dict:
    reflector = getattr(request.app.state, "reflector", None)
    if reflector is None:
        raise HTTPException(status_code=503, detail="reflection is not configured")

    # If a run is already in progress, just report its status — don't queue another.
    if reflector.status.get("running"):
        return {"ok": True, "started": False, "status": reflector.status}

    # Kick the reflection off in the background and return immediately so the
    # browser doesn't wait minutes for the LLM. The Morning Brief page polls
    # /admin/reflection/status to know when it's done.
    asyncio.create_task(_safe_run_reflection(reflector), name="reflection-manual")
    return {"ok": True, "started": True, "status": reflector.status}


async def _safe_run_reflection(reflector: Any) -> None:
    try:
        await reflector.run_once()
    except Exception as exc:  # noqa: BLE001
        # NightlyReflector already logs and stores last_error; just swallow here
        # so the background task doesn't fire a noisy "Task exception" warning.
        try:
            reflector._status["last_error"] = str(exc)  # noqa: SLF001
        except Exception:
            pass


@router.get("/admin/reflection/status")
async def reflection_status(request: Request) -> dict:
    reflector = getattr(request.app.state, "reflector", None)
    if reflector is None:
        return {
            "configured": False,
            "running": False,
            "started_at": None,
            "phase": None,
            "elapsed_seconds": None,
            "last_finished_at": None,
            "last_brief_id": None,
            "last_error": None,
            "last_duration_seconds": None,
        }
    return {"configured": True, **reflector.status}


@router.post("/admin/profile/upsert")
async def upsert_profile(request: Request) -> dict[str, Any]:
    body = await request.json()
    key = str(body.get("key") or "").strip()
    value = body.get("value")
    source = str(body.get("source") or "user").strip() or "user"
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise HTTPException(status_code=400, detail="value is required")
    store = _reflection_store(request)
    await store.upsert_profile(key=key, value=value, confidence=1.0, source=source)
    return {"ok": True, "key": key}


@router.post("/admin/profile/skip")
async def skip_profile(request: Request) -> dict[str, Any]:
    """Mark a knowledge gap as skipped so the reflector deprioritises it.

    Implementation: write a sentinel value with low confidence and a special
    source. The reflector's _knowledge_gaps method considers any present key
    as 'covered', so this stops the question from re-appearing.
    """
    body = await request.json()
    key = str(body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    store = _reflection_store(request)
    await store.upsert_profile(
        key=key, value="(skipped)", confidence=0.0, source="user_skipped"
    )
    return {"ok": True, "key": key}


@router.post("/admin/safety/explain")
async def explain_safety(request: Request) -> dict[str, Any]:
    body = await request.json()
    agent = str(body.get("agent") or "").strip()
    capability = str(body.get("capability") or "").strip()
    inputs = body.get("inputs") or {}
    if not agent or not capability:
        raise HTTPException(status_code=400, detail="agent and capability are required")
    if not isinstance(inputs, dict):
        raise HTTPException(status_code=400, detail="inputs must be an object")
    safety = getattr(request.app.state, "safety", None) or SafetyPolicy(
        os.environ.get("SAFETY_POLICY_PATH", "policies/safety.yaml")
    )
    return {"ok": True, **safety.explain(agent, capability, inputs)}


@router.post("/admin/proposals/{proposal_id}/format")
async def format_proposal(proposal_id: int, request: Request) -> dict:
    proposals = await _reflection_store(request).list_proposals(limit=500)
    proposal = next((item for item in proposals if int(item.get("id") or 0) == proposal_id), None)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal id: {proposal_id}")
    return {"ok": True, "proposal_id": proposal_id, "markdown": _format_proposal_markdown(proposal)}


@router.get("/admin/policies")
async def get_policies(request: Request) -> dict:
    return request.app.state.policy_engine.policies


@router.post("/admin/knowledge/confirm")
async def knowledge_confirm(request: Request) -> dict:
    body = await request.json()
    table = _knowledge_table(body.get("table"))
    row_id = _knowledge_id(table, body.get("id"))
    graph = _knowledge_graph(request)
    method = getattr(graph, _KNOWLEDGE_CONFIRM_METHODS[table], None)
    if method is None:
        raise HTTPException(status_code=400, detail="confirm is not supported for table")
    item = await method(row_id)
    if item is None:
        raise HTTPException(status_code=404, detail="knowledge row not found")
    return {"ok": True, "table": table, "id": row_id, "item": item}


@router.post("/admin/discovery/adopt")
async def discovery_adopt(request: Request) -> dict[str, Any]:
    body = await request.json()
    entity_id = _required_discovery_str(body, "entity_id")
    thing_type = _required_discovery_str(body, "type")
    friendly_name = _required_discovery_str(body, "friendly_name")
    photo_path = _optional_discovery_str(body, "photo_path")
    graph = _knowledge_graph(request)
    try:
        thing = await graph.put_thing(
            type=thing_type,
            friendly_name=friendly_name,
            attributes={},
            ha_entity_ids=[entity_id],
            photo_path=photo_path,
            confidence=1.0,
            source="discovery_user",
        )
    except Exception as exc:
        logger.warning("discovery_adopt_failed", entity_id=entity_id, error=str(exc))
        raise HTTPException(status_code=500, detail="discovery adopt failed") from exc
    if thing is None:
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
    return {"ok": True, "thing": thing}


@router.post("/admin/discovery/ignore")
async def discovery_ignore(request: Request) -> dict[str, Any]:
    body = await request.json()
    entity_id = _required_discovery_str(body, "entity_id")
    graph = _knowledge_graph(request)
    try:
        thing = await graph.put_thing(
            type="ignored.entity",
            friendly_name=entity_id,
            attributes={},
            ha_entity_ids=[entity_id],
            confidence=1.0,
            source="discovery_user",
        )
    except Exception as exc:
        logger.warning("discovery_ignore_failed", entity_id=entity_id, error=str(exc))
        raise HTTPException(status_code=500, detail="discovery ignore failed") from exc
    if thing is None:
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
    return {"ok": True, "thing": thing}


@router.get("/admin/knowledge/evidence")
async def knowledge_evidence(table: str, id: str, request: Request) -> dict:  # noqa: A002
    parsed_table = _knowledge_table(table)
    row_id = _knowledge_id(parsed_table, id)
    graph = _knowledge_graph(request)
    return {"items": await graph.evidence_for(parsed_table, row_id)}


@router.post("/admin/knowledge/forget")
async def knowledge_forget(request: Request) -> dict:
    body = await request.json()
    table = _knowledge_table(body.get("table"))
    row_id = _knowledge_id(table, body.get("id"))
    graph = _knowledge_graph(request)
    method = getattr(graph, _KNOWLEDGE_FORGET_METHODS[table], None)
    if method is None:
        raise HTTPException(status_code=400, detail="forget is not supported for table")
    deleted = await method(row_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="knowledge row not found")
    return {"ok": True, "table": table, "id": row_id, "deleted": True}


@router.patch("/admin/knowledge/{table}/{row_id}")
async def knowledge_patch(table: str, row_id: str, request: Request) -> dict:
    parsed_table = _knowledge_table(table)
    parsed_id = _knowledge_id(parsed_table, row_id)
    body = await request.json()
    updates = {
        key: value for key, value in body.items() if key in _KNOWLEDGE_PATCH_FIELDS[parsed_table]
    }
    if not updates:
        raise HTTPException(status_code=400, detail="no editable fields supplied")
    graph = _knowledge_graph(request)
    item = await graph.patch_row(parsed_table, parsed_id, updates)
    if item is None:
        raise HTTPException(status_code=404, detail="knowledge row not found")
    return {"ok": True, "table": parsed_table, "id": parsed_id, "item": item}


# === Dashboard button endpoints ============================================
# These power the read-write actions exposed by the live dashboard. The
# dashboard JS POSTs to them via fetch(); the resulting state changes are
# observed by the user through the SSE stream within a second or two.


@router.post("/admin/quiet/{state}")
async def set_quiet(state: str, request: Request) -> dict:
    if state not in {"on", "off", "clear"}:
        raise HTTPException(status_code=400, detail="state must be on|off|clear")
    policy_engine = request.app.state.policy_engine
    if state == "clear":
        await policy_engine.clear_quiet_override()
        return {"ok": True, "quiet": None}
    # Override TTL: 8h for `on`, 12h for `off` so the user can sleep through it.
    ttl_seconds = 8 * 3600 if state == "on" else 12 * 3600
    await policy_engine.set_quiet_override(state, ttl_seconds)
    return {"ok": True, "quiet": state, "ttl_seconds": ttl_seconds}


@router.post("/admin/mute")
async def mute(request: Request) -> dict:
    body = await request.json()
    key = str(body.get("key", "")).strip()
    minutes_raw = body.get("minutes")
    minutes = int(minutes_raw) if minutes_raw is not None else 30
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    if minutes <= 0 or minutes > 24 * 60:
        raise HTTPException(status_code=400, detail="minutes must be 1..1440")
    redis = request.app.state.redis
    await redis.set(f"policy:mute:{key}", "1", ex=minutes * 60)
    return {"ok": True, "key": key, "minutes": minutes}


@router.post("/admin/unmute")
async def unmute(request: Request) -> dict:
    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    redis = request.app.state.redis
    deleted = await redis.delete(f"policy:mute:{key}")
    return {"ok": True, "key": key, "deleted": int(deleted)}


@router.post("/admin/invoke")
async def invoke_capability(request: Request) -> dict:
    body = await request.json()
    agent = str(body.get("agent", "")).strip()
    capability = str(body.get("capability", "")).strip()
    payload = body.get("payload") or {}
    if not agent or not capability:
        raise HTTPException(status_code=400, detail="agent and capability are required")
    registry = request.app.state.registry
    try:
        result = await registry.dispatch(agent, capability, payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"agent error: {exc}") from exc
    return {"ok": True, "agent": agent, "capability": capability, "result": result}


@router.post("/admin/replay")
async def replay_event(request: Request) -> dict:
    body = await request.json()
    stream = str(body.get("stream", "")).strip()
    payload = body.get("payload")
    if not stream or payload is None:
        raise HTTPException(status_code=400, detail="stream and payload are required")
    redis = request.app.state.redis
    msg_id = await redis.xadd(stream, {"payload": json.dumps(payload, default=str)})
    return {"ok": True, "stream": stream, "id": str(msg_id)}


@router.get("/admin/activity/snapshot")
async def activity_snapshot(request: Request) -> dict[str, Any]:
    aggregator = request.app.state.activity_aggregator
    snapshot = aggregator.snapshot()
    snapshot["recent"] = aggregator.recent_events(limit=50)
    return snapshot
