from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import orchestrator.admin as admin_module
from orchestrator.admin import router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.policy_engine = SimpleNamespace(
        reload=AsyncMock(),
        policies={"quiet_hours": {}},
        set_quiet_override=AsyncMock(),
        clear_quiet_override=AsyncMock(),
    )
    app.state.scheduler = SimpleNamespace(
        reload=AsyncMock(return_value={"jobs": 3}), run_job_now=AsyncMock()
    )
    app.state.reactive = SimpleNamespace(reload=AsyncMock(return_value={"triggers": 2}))
    app.state.redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        delete=AsyncMock(return_value=1),
        xadd=AsyncMock(return_value="1-0"),
    )
    app.state.registry = SimpleNamespace(dispatch=AsyncMock(return_value={"ok": True}))
    app.state.activity_aggregator = SimpleNamespace(
        snapshot=lambda: {"agents": [], "window_minutes": 5, "total_events": 0},
        recent_events=lambda limit=50: [],
    )
    return app


def test_reload_policies_returns_counts(monkeypatch) -> None:
    app = _build_app()
    monkeypatch.setattr(
        admin_module, "_load_yaml", lambda _path: {"quiet_hours": {}, "rate_limits": []}
    )

    with TestClient(app) as client:
        resp = client.post("/admin/reload-policies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["schedules"] == 3
    assert body["triggers"] == 2


def test_run_job_invokes_stub_once() -> None:
    app = _build_app()
    app.state.scheduler.run_job_now = AsyncMock(return_value={"ok": True, "result": "done"})

    with TestClient(app) as client:
        resp = client.post("/admin/run-job/morning_brief")
    assert resp.status_code == 200
    assert resp.json()["result"]["result"] == "done"
    app.state.scheduler.run_job_now.assert_awaited_once_with("morning_brief")


def test_quiet_on_sets_override_with_ttl() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/quiet/on")
    assert resp.status_code == 200
    body = resp.json()
    assert body["quiet"] == "on"
    assert body["ttl_seconds"] == 8 * 3600
    app.state.policy_engine.set_quiet_override.assert_awaited_once_with("on", 8 * 3600)


def test_quiet_clear_calls_clear_override() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/quiet/clear")
    assert resp.status_code == 200
    assert resp.json()["quiet"] is None
    app.state.policy_engine.clear_quiet_override.assert_awaited_once()


def test_quiet_invalid_state_returns_400() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/quiet/maybe")
    assert resp.status_code == 400


def test_mute_sets_redis_key_with_ttl() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/mute", json={"key": "home_automation", "minutes": 15})
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "home_automation"
    assert body["minutes"] == 15
    app.state.redis.set.assert_awaited_once_with("policy:mute:home_automation", "1", ex=15 * 60)


def test_mute_rejects_invalid_minutes() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/mute", json={"key": "x", "minutes": 0})
    assert resp.status_code == 400


def test_mute_rejects_missing_key() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/mute", json={"minutes": 15})
    assert resp.status_code == 400


def test_unmute_deletes_redis_key() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/unmute", json={"key": "home_automation"})
    assert resp.status_code == 200
    app.state.redis.delete.assert_awaited_once_with("policy:mute:home_automation")


def test_invoke_capability_proxies_to_registry() -> None:
    app = _build_app()
    app.state.registry.dispatch = AsyncMock(return_value={"ok": True, "result": "hello"})

    with TestClient(app) as client:
        resp = client.post(
            "/admin/invoke",
            json={"agent": "home_automation", "capability": "list_entities", "payload": {}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "home_automation"
    assert body["result"] == {"ok": True, "result": "hello"}
    app.state.registry.dispatch.assert_awaited_once_with("home_automation", "list_entities", {})


def test_invoke_capability_requires_agent_and_capability() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/invoke", json={"agent": "", "capability": "x"})
    assert resp.status_code == 400


def test_replay_publishes_to_stream() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/admin/replay",
            json={"stream": "events.home", "payload": {"type": "doorbell_ring"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stream"] == "events.home"
    assert body["id"] == "1-0"


def test_activity_snapshot_returns_aggregator_data() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/admin/activity/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    assert "agents" in body
    assert "recent" in body
