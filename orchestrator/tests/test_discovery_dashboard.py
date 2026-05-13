from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


class _FakeKnowledgeGraph:
    async def list_things(self):
        return [
            {
                "id": 1,
                "type": "appliance.dryer",
                "friendly_name": "Dryer",
                "ha_entity_ids": ["sensor.dryer"],
            },
            {
                "id": 2,
                "type": "ignored.entity",
                "friendly_name": "sensor.ignore_me",
                "ha_entity_ids": ["sensor.ignore_me"],
            },
        ]


def test_discovery_renders_unidentified_entities_only() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.registry = SimpleNamespace(
        dispatch=AsyncMock(
            return_value={
                "ok": True,
                "result": {
                    "by_area": {
                        "Laundry": [
                            {"name": "Washer", "entity_id": "sensor.washer", "state": "idle"},
                            {"name": "Dryer", "entity_id": "sensor.dryer", "state": "off"},
                        ],
                        "Misc": [
                            {
                                "name": "Ignore Me",
                                "entity_id": "sensor.ignore_me",
                                "state": "on",
                            }
                        ],
                    }
                },
            }
        )
    )
    app.state.knowledge_graph = _FakeKnowledgeGraph()

    with TestClient(app) as client:
        resp = client.get("/dashboard/discovery")

    assert resp.status_code == 200
    assert "Discovery" in resp.text
    assert "sensor.washer" in resp.text
    assert "appliance.washer" in resp.text
    assert "sensor.dryer" not in resp.text
    assert "sensor.ignore_me" not in resp.text
    assert "/static/_design.css" in resp.text
    assert "/static/_app.js" in resp.text
    assert "/static/discovery.js" in resp.text
    assert 'id="search"' in resp.text
    assert 'id="group-by"' in resp.text
    assert "toast-stack" in resp.text
    app.state.registry.dispatch.assert_awaited_once_with(
        "home_automation",
        "list_entities",
        {"include_unavailable": True},
    )
