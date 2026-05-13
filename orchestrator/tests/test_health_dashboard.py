from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


def test_health_dashboard_renders_with_synthetic_data() -> None:
    app = FastAPI()
    app.include_router(router)

    async def aggregate(metric: str, days: int = 30) -> list[dict]:
        if metric == "steps":
            return [{"day": "2026-05-13", "value": 8450, "unit": "steps"}]
        if metric == "sleep_asleep":
            return [{"day": "2026-05-13", "value": 430, "unit": "min"}]
        if metric == "active_energy":
            return [{"day": "2026-05-13", "value": 560, "unit": "kcal"}]
        if metric == "weight":
            return [{"day": "2026-05-13", "value": 82.4, "unit": "kg"}]
        if metric == "resting_heart_rate":
            return [{"day": "2026-05-13", "value": 57, "unit": "bpm"}]
        return []

    async def latest(metric: str) -> dict | None:
        if metric == "weight":
            return {"metric": "weight", "value": 82.4, "unit": "kg"}
        if metric == "workout":
            return {
                "metric": "workout",
                "value": 45,
                "unit": "min",
                "metadata": {"workout_type": "running"},
            }
        if metric == "resting_heart_rate":
            return {"metric": "resting_heart_rate", "value": 57, "unit": "bpm"}
        return None

    app.state.health_store = SimpleNamespace(
        summary=AsyncMock(
            return_value={
                "total_metrics": 12453,
                "last_received_at": "2026-05-13T08:00:00+00:00",
            }
        ),
        list_recent=AsyncMock(
            return_value=[
                {"metric": "sleep_deep", "value": 90},
                {"metric": "sleep_rem", "value": 80},
                {"metric": "sleep_core", "value": 260},
                {"metric": "sleep_awake", "value": 15},
            ]
        ),
        aggregate_daily=AsyncMock(side_effect=aggregate),
        latest=AsyncMock(side_effect=latest),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard/health")

    assert resp.status_code == 200
    assert "Apple Health" in resp.text
    assert "12,453 metrics in store" in resp.text
    assert "running" in resp.text
    assert "How to set up Health Auto Export" in resp.text
    assert "/static/health.css" in resp.text
    assert "/static/health.js" in resp.text
