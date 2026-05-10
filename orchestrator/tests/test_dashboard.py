from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


def test_dashboard_renders_with_empty_state() -> None:
    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return {
            "stack": {
                "orchestrator": {"ok": True},
                "ollama": {"ok": False},
                "lemonade": {"ok": False},
                "redis": {"ok": False},
                "postgres": {"ok": False},
                "qdrant": {"ok": False},
            },
            "agents": [],
            "jobs": [],
            "recent_notifications": [],
            "active_mutes": [],
            "quiet_override": None,
            "models": {"npu": [], "igpu": []},
        }

    app.state.status_provider = _status

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Home Intelligence Dashboard" in resp.text
