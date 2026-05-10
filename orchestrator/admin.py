from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["admin"])


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


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


@router.get("/admin/policies")
async def get_policies(request: Request) -> dict:
    return request.app.state.policy_engine.policies
