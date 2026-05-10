from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException

from .schemas import InvokeRequest, InvokeResponse, Manifest
from .telemetry import get_logger
from .tools import get_tool, list_tools


async def _invoke_tool(fn: Any, payload: dict[str, Any], ctx: dict[str, Any]) -> Any:
    kwargs = dict(payload)
    if "ctx" in inspect.signature(fn).parameters:
        kwargs["ctx"] = ctx
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _load_manifest(manifest_path: str) -> Manifest:
    data = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8"))
    return Manifest.model_validate(data)


def _validate_manifest_tools(manifest: Manifest) -> None:
    available = list_tools()
    missing = [cap.id for cap in manifest.capabilities if cap.id not in available]
    if missing:
        raise ValueError(f"Missing tool registrations for capabilities: {', '.join(missing)}")


def build_app(agent_name: str, manifest_path: str) -> FastAPI:
    manifest = _load_manifest(manifest_path)
    if manifest.agent != agent_name:
        raise ValueError(f"Manifest agent '{manifest.agent}' does not match '{agent_name}'")
    _validate_manifest_tools(manifest)

    app = FastAPI(title=f"{agent_name}-agent")
    logger = get_logger(agent_name)
    ctx = {"bus": None, "memory": None, "llm": None, "npu": None}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": agent_name}

    @app.get("/manifest")
    async def manifest_endpoint() -> dict[str, Any]:
        return manifest.model_dump()

    @app.post("/invoke", response_model=InvokeResponse)
    async def invoke(req: InvokeRequest) -> InvokeResponse:
        spec = get_tool(req.capability)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown capability: {req.capability}")
        try:
            result = await _invoke_tool(spec.fn, req.payload, ctx)
            return InvokeResponse(ok=True, result=result)
        except (TypeError, ValueError, KeyError) as exc:  # pragma: no cover - surfaced to caller
            logger.warning("invoke_failed", capability=req.capability, error=str(exc))
            return InvokeResponse(ok=False, error=str(exc))

    return app


__all__ = ["build_app"]
