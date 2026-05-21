"""Tests for the /admin/routines endpoints (Phase 5/6 wiring)."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.admin import router


def _build_app(*, store) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.routine_lifecycle_store = store
    return app


def _stub_routine(**overrides) -> dict:
    base = {
        "id": 7,
        "name": "washer.cycle_complete -> dryer.start",
        "status": "suggested",
        "source": "routine_sequence_miner",
        "confirmed_count": 1,
        "steps": '{"steps":[{"trigger":"washer.cycle_complete"},'
                 '{"action":"dryer.start"}],"attributes":{"confidence":0.85}}',
        "schedule": None,
        "created_at": datetime(2026, 5, 21, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 21, tzinfo=UTC),
        "promoted_at": None,
        "dismissed_at": None,
    }
    base.update(overrides)
    return base


def test_get_routines_returns_grouped_payload() -> None:
    store = SimpleNamespace(
        list_suggested=AsyncMock(return_value=[_stub_routine()]),
        list_active=AsyncMock(return_value=[]),
        list_dismissed=AsyncMock(return_value=[]),
        stats=AsyncMock(return_value={
            "suggested": 1, "active": 0, "dismissed": 0,
        }),
    )
    with TestClient(_build_app(store=store)) as client:
        res = client.get("/admin/routines")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["stats"]["suggested"] == 1
    assert len(body["suggested"]) == 1
    item = body["suggested"][0]
    assert item["id"] == 7
    assert item["name"] == "washer.cycle_complete -> dryer.start"
    # Steps jsonb was decoded for the response
    assert "steps" in item and isinstance(item["steps"], dict)


def test_post_action_confirm_calls_store() -> None:
    store = SimpleNamespace(
        record_action=AsyncMock(return_value=_stub_routine(
            status="suggested", confirmed_count=2,
        )),
    )
    with TestClient(_build_app(store=store)) as client:
        res = client.post(
            "/admin/routines/7/confirm",
            json={"note": "looks right"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["routine"]["confirmed_count"] == 2
    store.record_action.assert_awaited_once()
    call = store.record_action.await_args
    assert call.args[0] == 7
    assert call.args[1] == "confirm"
    assert call.kwargs.get("note") == "looks right"


def test_post_action_dismiss_returns_dismissed_state() -> None:
    store = SimpleNamespace(
        record_action=AsyncMock(return_value=_stub_routine(
            status="dismissed", dismissed_at=datetime(2026, 5, 21, tzinfo=UTC),
        )),
    )
    with TestClient(_build_app(store=store)) as client:
        res = client.post("/admin/routines/7/dismiss")
    assert res.status_code == 200
    assert res.json()["routine"]["status"] == "dismissed"


def test_post_action_unknown_action_returns_400() -> None:
    store = SimpleNamespace(record_action=AsyncMock(return_value=None))
    with TestClient(_build_app(store=store)) as client:
        res = client.post("/admin/routines/7/burn")
    assert res.status_code == 400


def test_post_action_unknown_routine_returns_404() -> None:
    store = SimpleNamespace(record_action=AsyncMock(return_value=None))
    with TestClient(_build_app(store=store)) as client:
        res = client.post("/admin/routines/999/confirm")
    assert res.status_code == 404


def test_post_action_validation_error_returns_400() -> None:
    store = SimpleNamespace(
        record_action=AsyncMock(side_effect=ValueError("bad")),
    )
    with TestClient(_build_app(store=store)) as client:
        res = client.post("/admin/routines/7/confirm")
    assert res.status_code == 400


def test_get_history_returns_audit_rows() -> None:
    store = SimpleNamespace(
        history=AsyncMock(return_value=[
            {
                "id": 11, "action": "confirm", "source": "dashboard",
                "note": None, "created_at": datetime(2026, 5, 21, tzinfo=UTC),
            }
        ]),
    )
    with TestClient(_build_app(store=store)) as client:
        res = client.get("/admin/routines/7/history")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert len(body["history"]) == 1
    assert body["history"][0]["action"] == "confirm"
