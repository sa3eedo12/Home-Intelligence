from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from home_agents_sdk.reflection_store import ReflectionStore

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


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


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html.j2",
        context={"status": await _status(request)},
    )


@router.get("/dashboard/morning-brief", response_class=HTMLResponse)
async def morning_brief(request: Request) -> HTMLResponse:
    store = _reflection_store(request)
    briefs = await store.list_briefs(limit=1)
    brief = briefs[0] if briefs else None
    body = (brief or {}).get("body_json") or {}
    proposals = body.get("proposals") or await store.list_proposals(limit=50)
    return templates.TemplateResponse(
        request=request,
        name="morning_brief.html.j2",
        context={"status": await _status(request), "brief": brief, "proposals": proposals},
    )
