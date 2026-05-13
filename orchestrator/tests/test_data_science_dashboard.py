from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router
from tests.data_science_fakes import FakePool


class DashboardConn:
    async def fetch(self, query: str, *args):
        if "FROM reports" in query:
            return [
                {
                    "kind": "weekly",
                    "period_label": "2026W19",
                    "file_path": "data/reports/weekly-2026W19.md",
                    "summary": "10 events, 1 errors",
                    "generated_at": datetime(2026, 5, 12, 5, tzinfo=UTC),
                }
            ]
        if "capability = 'maintenance'" in query:
            return [
                {
                    "ts": datetime(2026, 5, 12, 3, 30, tzinfo=UTC),
                    "summary": "data_science.maintenance status=ok",
                    "payload": {"status": "ok", "archived_rows": 4, "errors": []},
                }
            ]
        return []

    async def fetchrow(self, query: str, *args):
        return {
            "id": 1,
            "started_at": datetime(2026, 5, 12, 6, tzinfo=UTC),
            "finished_at": datetime(2026, 5, 12, 6, 1, tzinfo=UTC),
            "status": "disabled",
            "model_base": "qwen3:8b",
            "training_file": None,
            "quality_score": None,
            "error": None,
        }

    async def fetchval(self, query: str, *args):
        return 7


def test_data_science_dashboard_renders_synthetic_data() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.pool = FakePool(DashboardConn())
    app.state.reembed = SimpleNamespace(current_model="bge-m3-int8")

    with TestClient(app) as client:
        resp = client.get("/dashboard/data-science")

    assert resp.status_code == 200
    assert "Data science" in resp.text
    assert "/static/_design.css" in resp.text
    assert "/static/_app.js" in resp.text
    assert "/static/data_science.css" in resp.text
    assert "/static/data_science.js" in resp.text
    assert 'id="ds-status"' in resp.text
    assert 'id="job-status"' in resp.text
    assert "2026W19" in resp.text
    assert "Run maintenance now" in resp.text
    assert 'data-job="reembed"' in resp.text
    assert "7 event_log rows" in resp.text


def test_suggest_entity_type_classifies_tvs_and_monitors() -> None:
    from orchestrator.dashboard import _suggest_entity_type

    cases = [
        (
            {"entity_id": "media_player.living_room_tv", "friendly_name": "Living Room TV"},
            "device.tv",
        ),
        (
            {"entity_id": "media_player.appletv_bedroom", "friendly_name": "Apple TV Bedroom"},
            "device.tv",
        ),
        (
            {"entity_id": "switch.dell_monitor", "friendly_name": "Dell Monitor"},
            "device.monitor",
        ),
        (
            {"entity_id": "media_player.sonos_kitchen", "friendly_name": "Kitchen Sonos"},
            "device.speaker",
        ),
        (
            {"entity_id": "switch.playstation_5", "friendly_name": "PS5"},
            "device.game_console",
        ),
        (
            {"entity_id": "sensor.fridge_temp", "friendly_name": "Fridge Temperature"},
            "appliance.fridge",
        ),
        (
            {"entity_id": "media_player.unknown", "friendly_name": "Bookshelf"},
            "media_player",
        ),
    ]
    for entity, expected in cases:
        assert _suggest_entity_type(entity) == expected, entity
