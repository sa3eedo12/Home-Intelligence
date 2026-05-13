from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.admin import router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _payload() -> dict:
    return {
        "data": {
            "metrics": [
                {
                    "type": "HKQuantityTypeIdentifierStepCount",
                    "unit": "count",
                    "data": [{"date": "2026-05-13T08:00:00Z", "qty": 1234}],
                }
            ]
        }
    }


def test_sync_refuses_when_token_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("HEALTHKIT_WEBHOOK_TOKEN", raising=False)
    app = _app()

    with TestClient(app) as client:
        resp = client.post("/admin/healthkit/sync", json=_payload())

    assert resp.status_code == 503
    assert "HEALTHKIT_WEBHOOK_TOKEN" in resp.json()["detail"]


def test_sync_rejects_bad_token(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHKIT_WEBHOOK_TOKEN", "secret")
    app = _app()

    with TestClient(app) as client:
        resp = client.post(
            "/admin/healthkit/sync",
            json=_payload(),
            headers={"X-Health-Token": "wrong"},
        )

    assert resp.status_code == 401


def test_sync_normalizes_upserts_records_event_and_resolves_adult(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHKIT_WEBHOOK_TOKEN", "secret")
    app = _app()

    async def latest(metric: str) -> dict:
        return {"metric": metric, "value": 1234, "unit": "steps"}

    store = SimpleNamespace(
        upsert_metrics=AsyncMock(return_value={"inserted": 1, "skipped": 0}),
        latest=AsyncMock(side_effect=latest),
    )
    event_log = SimpleNamespace(record_event=AsyncMock(return_value={"ok": True}))
    app.state.health_store = store
    app.state.event_log_store = event_log
    app.state.knowledge_graph = SimpleNamespace(
        list_members=AsyncMock(
            return_value=[{"id": 2, "role": "child"}, {"id": 9, "role": "adult"}]
        )
    )

    with TestClient(app) as client:
        resp = client.post(
            "/admin/healthkit/sync",
            json=_payload(),
            headers={"X-Health-Token": "secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["inserted"] == 1
    assert body["skipped"] == 0
    assert body["latest"]["steps"]["value"] == 1234
    rows = store.upsert_metrics.await_args.args[0]
    assert rows[0]["member_id"] == 9
    assert rows[0]["metric"] == "steps"
    event_log.record_event.assert_awaited_once()
    assert event_log.record_event.await_args.kwargs["agent"] == "health.sync"
    assert "1 new rows" in event_log.record_event.await_args.kwargs["summary"]


def test_sync_accepts_explicit_member_id(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHKIT_WEBHOOK_TOKEN", "secret")
    app = _app()
    app.state.health_store = SimpleNamespace(
        upsert_metrics=AsyncMock(return_value={"inserted": 1, "skipped": 0}),
        latest=AsyncMock(return_value=None),
    )
    app.state.event_log_store = SimpleNamespace(record_event=AsyncMock(return_value={"ok": True}))
    app.state.knowledge_graph = SimpleNamespace(list_members=AsyncMock(return_value=[]))

    with TestClient(app) as client:
        resp = client.post(
            "/admin/healthkit/sync?member_id=42",
            json=_payload(),
            headers={"X-Health-Token": "secret"},
        )

    assert resp.status_code == 200
    rows = app.state.health_store.upsert_metrics.await_args.args[0]
    assert rows[0]["member_id"] == 42
    app.state.knowledge_graph.list_members.assert_not_awaited()


def test_sync_bad_member_id_returns_400(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHKIT_WEBHOOK_TOKEN", "secret")
    app = _app()

    with TestClient(app) as client:
        resp = client.post(
            "/admin/healthkit/sync?member_id=abc",
            json=_payload(),
            headers={"X-Health-Token": "secret"},
        )

    assert resp.status_code == 400


def test_recent_and_aggregate_validate_inputs() -> None:
    app = _app()
    app.state.health_store = SimpleNamespace(
        list_recent=AsyncMock(return_value=[{"metric": "steps"}]),
        aggregate_daily=AsyncMock(return_value=[{"day": "2026-05-13", "value": 1234}]),
    )

    with TestClient(app) as client:
        recent = client.get("/admin/healthkit/recent?metric=steps&hours=12")
        aggregate = client.get("/admin/healthkit/aggregate?metric=steps&days=7")
        bad = client.get("/admin/healthkit/aggregate?metric=bad metric")

    assert recent.status_code == 200
    assert recent.json()["count"] == 1
    app.state.health_store.list_recent.assert_awaited_once_with(metric="steps", hours=12)
    assert aggregate.status_code == 200
    app.state.health_store.aggregate_daily.assert_awaited_once_with("steps", days=7)
    assert bad.status_code == 400
