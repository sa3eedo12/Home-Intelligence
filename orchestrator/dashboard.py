from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


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
    status = await request.app.state.status_provider()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html.j2",
        context={"status": status},
    )
