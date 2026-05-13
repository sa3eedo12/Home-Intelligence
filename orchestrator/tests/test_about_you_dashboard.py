from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


class _FakeKnowledgeGraph:
    async def list_things(self):
        return [
            {
                "id": 1,
                "type": "appliance.washer",
                "friendly_name": "Washer",
                "attributes": {"brand": "LG"},
                "ha_entity_ids": ["sensor.washer"],
                "photo_path": None,
                "confidence": 0.8,
                "learned_at": "2026-01-01T00:00:00+00:00",
                "last_confirmed_at": None,
                "source": "event_log",
            }
        ]

    async def list_habits(self):
        return [
            {
                "id": 2,
                "subject": "user.coffee_brew",
                "pattern": {"days_of_week": ["mon", "tue"], "time_window_local": "07:00-07:30"},
                "frequency": "weekdays",
                "confidence": 0.7,
                "last_observed_at": "2026-01-02T07:10:00+00:00",
                "source": "event_log",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def list_preferences(self):
        return [
            {
                "key": "lights.after_sunset",
                "value": {"color_temperature": "warm"},
                "confidence": 0.9,
                "source": "user",
                "updated_at": "2026-01-03T00:00:00+00:00",
            }
        ]

    async def list_routines(self):
        return [
            {
                "id": 3,
                "name": "Laundry day",
                "steps": [{"thing": "Washer", "action": "run bedding cycle"}],
                "schedule": {"day": "sun"},
                "last_run_at": None,
                "source": "user",
                "created_at": "2026-01-04T00:00:00+00:00",
            }
        ]


def test_about_you_renders_learned_knowledge() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.knowledge_graph = _FakeKnowledgeGraph()

    with TestClient(app) as client:
        resp = client.get("/dashboard/about-you")

    assert resp.status_code == 200
    assert "About You" in resp.text
    assert "Washer" in resp.text
    assert "user.coffee_brew" in resp.text
    assert "lights.after_sunset" in resp.text
    assert "Laundry day" in resp.text
    assert "/static/_design.css" in resp.text
    assert "/static/_app.js" in resp.text
    assert "/static/about_you.css" in resp.text
    assert "/static/about_you.js" in resp.text
    assert 'id="edit-modal"' in resp.text
    assert 'id="evidence-modal"' in resp.text
    assert "toast-stack" in resp.text
    assert 'data-table="things"' in resp.text
    assert "Confirm" in resp.text
    assert "Why?" in resp.text
