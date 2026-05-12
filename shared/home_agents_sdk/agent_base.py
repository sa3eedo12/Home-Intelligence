from __future__ import annotations

import inspect
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from redis.asyncio import Redis

from .schemas import InvokeRequest, InvokeResponse, Manifest
from .telemetry import get_logger
from .tools import get_tool, list_tools

ACTIVITY_STREAM = "events.activity"
ACTIVITY_MAXLEN = 10000


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


async def _publish_activity(
    redis: Redis | None,
    *,
    agent: str,
    capability: str,
    status: str,
    duration_ms: float,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Publish a single activity event. Silently no-ops if Redis is unavailable.

    Schema: {agent, capability, status, duration_ms, ts, error?, extra?}
    """
    if redis is None:
        return
    payload: dict[str, Any] = {
        "agent": agent,
        "capability": capability,
        "status": status,
        "duration_ms": round(duration_ms, 2),
        "ts": datetime.now(UTC).isoformat(),
    }
    if error is not None:
        payload["error"] = error
    if extra:
        payload["extra"] = extra
    try:
        await redis.xadd(
            ACTIVITY_STREAM,
            {"payload": json.dumps(payload, default=str)},
            maxlen=ACTIVITY_MAXLEN,
            approximate=True,
        )
    except Exception:
        # The dashboard is best-effort; never break /invoke because of telemetry.
        pass


def build_app(agent_name: str, manifest_path: str) -> FastAPI:
    manifest = _load_manifest(manifest_path)
    if manifest.agent != agent_name:
        raise ValueError(f"Manifest agent '{manifest.agent}' does not match '{agent_name}'")
    _validate_manifest_tools(manifest)

    app = FastAPI(title=f"{agent_name}-agent")
    logger = get_logger(agent_name)
    ctx: dict[str, Any] = {
        "agent": agent_name,
        "bus": None,
        "memory": None,
        "llm": None,
        "npu": None,
        "redis": None,
        "publish_activity": None,
    }
    app.state.activity_redis = None

    @app.on_event("startup")
    async def _connect_activity_bus() -> None:
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            return
        try:
            client = Redis.from_url(redis_url, decode_responses=True)
            await client.ping()
        except Exception as exc:
            logger.warning("activity_bus_unavailable", error=str(exc))
            return
        app.state.activity_redis = client
        ctx["redis"] = client

        async def _publish_progress(capability: str, message: str, **extra: Any) -> None:
            await _publish_activity(
                client,
                agent=agent_name,
                capability=capability,
                status="in_progress",
                duration_ms=0.0,
                extra={"message": message, **extra},
            )

        ctx["publish_activity"] = _publish_progress

    @app.on_event("shutdown")
    async def _close_activity_bus() -> None:
        client = app.state.activity_redis
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

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

        redis = app.state.activity_redis
        await _publish_activity(
            redis,
            agent=agent_name,
            capability=req.capability,
            status="started",
            duration_ms=0.0,
        )

        start = time.perf_counter()
        try:
            result = await _invoke_tool(spec.fn, req.payload, ctx)
        except (TypeError, ValueError, KeyError) as exc:  # surfaced to caller
            duration_ms = (time.perf_counter() - start) * 1000
            logger.warning("invoke_failed", capability=req.capability, error=str(exc))
            await _publish_activity(
                redis,
                agent=agent_name,
                capability=req.capability,
                status="error",
                duration_ms=duration_ms,
                error=str(exc),
            )
            return InvokeResponse(ok=False, error=str(exc))

        duration_ms = (time.perf_counter() - start) * 1000
        await _publish_activity(
            redis,
            agent=agent_name,
            capability=req.capability,
            status="ok",
            duration_ms=duration_ms,
        )
        return InvokeResponse(ok=True, result=result)

    return app


__all__ = ["ACTIVITY_STREAM", "ACTIVITY_MAXLEN", "build_app"]
