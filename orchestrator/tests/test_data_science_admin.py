from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import orchestrator.admin as admin_module
from orchestrator.admin import router
from tests.data_science_fakes import FakePool


class AdminConn:
    async def fetch(self, query: str, *args):
        if "DISTINCT ON" in query:
            return [
                {
                    "capability": "maintenance",
                    "ts": datetime(2026, 5, 12, 3, 30, tzinfo=UTC),
                    "summary": "data_science.maintenance status=ok",
                    "payload": {"status": "ok"},
                }
            ]
        return []

    async def fetchval(self, query: str, *args):
        return 5

    async def fetchrow(self, query: str, *args):
        return {"body_markdown": "# Weekly report", "kind": args[0], "period_label": args[1]}


class FakeLock:
    def locked(self) -> bool:
        return False


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.maintenance = SimpleNamespace(
        _lock=FakeLock(), run=AsyncMock(return_value={"status": "ok"})
    )
    app.state.pattern_miner = SimpleNamespace(
        _lock=FakeLock(), run=AsyncMock(return_value={"stored": 1})
    )
    app.state.reembed = SimpleNamespace(
        _lock=FakeLock(), current_model="bge-m3-int8", run=AsyncMock(return_value={"processed": 1})
    )
    app.state.reports = SimpleNamespace(
        _lock=FakeLock(),
        weekly_report=AsyncMock(return_value={"path": "weekly.md"}),
        monthly_report=AsyncMock(return_value={"path": "monthly.md"}),
        list_recent_reports=AsyncMock(
            return_value=[{"kind": "weekly", "period_label": "2026W19", "summary": "10 events"}]
        ),
        get_report=AsyncMock(return_value={"body_markdown": "# Stored report"}),
    )
    app.state.lora_training = SimpleNamespace(
        _lock=FakeLock(), run=AsyncMock(return_value={"status": "disabled"})
    )
    app.state.pool = FakePool(AdminConn())
    return app


def test_run_data_science_job_endpoints_return_immediately(monkeypatch) -> None:
    created = []

    def fake_create_task(coro, name=None):
        created.append(name)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(admin_module.asyncio, "create_task", fake_create_task)
    app = _build_app()

    with TestClient(app) as client:
        for job in (
            "maintenance",
            "pattern_mining",
            "reembed",
            "weekly_report",
            "monthly_report",
            "lora_training",
        ):
            resp = client.post(f"/admin/data-science/run/{job}")
            assert resp.status_code == 200
            assert resp.json()["started"] is True

    assert created == [
        "data-science-maintenance-manual",
        "data-science-pattern_mining-manual",
        "data-science-reembed-manual",
        "data-science-weekly_report-manual",
        "data-science-monthly_report-manual",
        "data-science-lora_training-manual",
    ]


def test_data_science_status_returns_jobs_reports_and_embedding() -> None:
    app = _build_app()

    with TestClient(app) as client:
        resp = client.get("/admin/data-science/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"][0]["name"] == "maintenance"
    assert body["jobs"][0]["last_status"] == "ok"
    assert body["reports"][0]["period_label"] == "2026W19"
    assert body["embedding"] == {"current_model": "bge-m3-int8", "stale_event_count": 5}


def test_report_fetch_returns_markdown() -> None:
    app = _build_app()

    with TestClient(app) as client:
        resp = client.get("/admin/reports/weekly/2026W19")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text == "# Stored report"
