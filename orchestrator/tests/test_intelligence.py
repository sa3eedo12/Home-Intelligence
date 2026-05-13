from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request

from orchestrator.intelligence import gather_intelligence_summary


def _make_request(app: FastAPI) -> Request:
    """Build a Request that exposes app.state.* the way our gatherer reads it."""
    scope = {"type": "http", "app": app, "headers": [], "query_string": b"", "path": "/"}
    return Request(scope)


def _make_app(*, members=None, things=None, habits=None) -> FastAPI:
    app = FastAPI()
    app.state.pool = None  # store ctors check ._ready via pool, None = not ready
    app.state.knowledge_graph = SimpleNamespace(
        list_members=AsyncMock(return_value=members or []),
        list_things=AsyncMock(return_value=things or []),
        list_habits=AsyncMock(return_value=habits or []),
    )
    app.state.health_store = None
    return app


@pytest.mark.asyncio
async def test_summary_returns_skeleton_when_db_empty() -> None:
    app = _make_app()
    summary = await gather_intelligence_summary(_make_request(app))
    assert summary["ok"] is True
    assert summary["household"]["members"] == []
    assert summary["devices"]["total"] == 0
    assert summary["habits"]["total"] == 0
    # Inferences all return defaults; structurally each section dict is intact
    for sec in summary["inferences"].values():
        if isinstance(sec, dict) and "confirmed" in sec:
            assert sec["confirmed"] == 0


@pytest.mark.asyncio
async def test_summary_groups_devices_by_type() -> None:
    app = _make_app(things=[
        {
            "id": 1, "friendly_name": "Washer", "type": "appliance.washer",
            "attributes": {"entity_id": "sensor.washer_machine_state"},
        },
        {
            "id": 2, "friendly_name": "Dryer", "type": "appliance.dryer",
            "attributes": {"entity_id": "sensor.dryer_machine_state"},
        },
        {
            "id": 3, "friendly_name": "Living Room TV", "type": "device.tv",
            "attributes": {"entity_id": "media_player.tv"},
        },
    ])
    summary = await gather_intelligence_summary(_make_request(app))
    by_type = summary["devices"]["by_type"]
    assert "appliance.washer" in by_type
    assert "appliance.dryer" in by_type
    assert "device.tv" in by_type
    assert summary["devices"]["total"] == 3


@pytest.mark.asyncio
async def test_summary_splits_confirmed_vs_unconfirmed_habits() -> None:
    app = _make_app(habits=[
        {"id": 1, "subject": "morning_coffee", "pattern": "weekday 7:15", "confidence": 0.9,
         "attributes": {"confirmed_at": "2026-05-01T08:00:00+00:00"}},
        {"id": 2, "subject": "evening_tv", "pattern": "weekday 21:00", "confidence": 0.6,
         "attributes": {}},
    ])
    summary = await gather_intelligence_summary(_make_request(app))
    assert len(summary["habits"]["confirmed"]) == 1
    assert summary["habits"]["confirmed"][0]["subject"] == "morning_coffee"
    assert len(summary["habits"]["unconfirmed"]) == 1


@pytest.mark.asyncio
async def test_open_questions_listed_when_things_pending() -> None:
    app = _make_app(habits=[
        {"id": 1, "subject": "x", "pattern": "y", "confidence": 0.5, "attributes": {}},
    ])
    summary = await gather_intelligence_summary(_make_request(app))
    questions = summary["open_questions"]
    assert any(q["topic"] == "habit" for q in questions)


@pytest.mark.asyncio
async def test_summary_resilient_to_section_failures() -> None:
    """If knowledge_graph.list_things raises, the page should still load."""
    app = FastAPI()
    app.state.pool = None
    app.state.knowledge_graph = SimpleNamespace(
        list_members=AsyncMock(return_value=[{"id": 1, "name": "Saeed", "role": "adult"}]),
        list_things=AsyncMock(side_effect=RuntimeError("DB went away")),
        list_habits=AsyncMock(return_value=[]),
    )
    app.state.health_store = None
    summary = await gather_intelligence_summary(_make_request(app))
    # Did not crash; things section is empty fallback
    assert summary["ok"] is True
    assert summary["devices"]["total"] == 0
    assert summary["household"]["members"][0]["name"] == "Saeed"


@pytest.mark.asyncio
async def test_summary_handles_missing_knowledge_graph() -> None:
    """When the orchestrator hasn't constructed a knowledge graph yet, the
    page should still render an empty skeleton without crashing."""
    app = FastAPI()
    app.state.pool = None
    app.state.knowledge_graph = None
    app.state.health_store = None
    summary = await gather_intelligence_summary(_make_request(app))
    assert summary["ok"] is True
    assert summary["household"]["members"] == []


def test_open_questions_synthesizes_action_for_each_pending_kind() -> None:
    """Quick sanity that the synthesizer covers each pending kind."""
    from orchestrator.intelligence import _open_questions

    qs = _open_questions(
        unconfirmed_habits=[{"subject": "x", "pattern": "y"}],
        cycle_unconfirmed=2,
        cleaning_unconfirmed=1,
        sleep_unconfirmed=3,
        presence_unconfirmed=1,
        tv_unconfirmed=2,
        auto_status_counts=Counter({"proposed": 4}),
    )
    topics = {q["topic"] for q in qs}
    assert topics == {"habit", "appliance", "cleaning", "sleep", "presence", "tv", "auto_infer"}
