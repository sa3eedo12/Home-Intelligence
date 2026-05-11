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
    app.state.policy_engine = SimpleNamespace(reload=AsyncMock(), policies={"quiet_hours": {}})
    app.state.scheduler = SimpleNamespace(
        reload=AsyncMock(return_value={"jobs": 3}), run_job_now=AsyncMock()
    )
    app.state.reactive = SimpleNamespace(reload=AsyncMock(return_value={"triggers": 2}))
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
