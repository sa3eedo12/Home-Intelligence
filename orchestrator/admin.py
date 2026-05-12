from __future__ import annotations

import json
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from home_agents_sdk.reflection_store import ReflectionStore

router = APIRouter(tags=["admin"])


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


@router.post("/admin/reload-policies")
async def reload_policies(request: Request) -> dict:
    app_state = request.app.state
    policies = _load_yaml("orchestrator/policies.yaml")
    await app_state.policy_engine.reload(policies)
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
    result = await reflector.run_once()
    return {"ok": True, "result": result}


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
