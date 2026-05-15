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
                "attributes": {"brand": "LG", "scope": "home"},
                "ha_entity_ids": ["sensor.washer"],
                "photo_path": None,
                "confidence": 0.8,
                "learned_at": "2026-01-01T00:00:00+00:00",
                "last_confirmed_at": None,
                "source": "event_log",
            },
            {
                "id": 2,
                "type": "device.phone",
                "friendly_name": "Saeed's iPhone",
                "attributes": {
                    "manufacturer": "Apple",
                    "model": "iPhone17,2",
                    "scope": "personal",
                    "owner_member_id": 7,
                    "ha_device_id": "abc",
                },
                "ha_entity_ids": ["device_tracker.saeeds_iphone"],
                "photo_path": None,
                "confidence": 0.9,
                "learned_at": "2026-01-01T00:00:00+00:00",
                "last_confirmed_at": None,
                "source": "auto_setup",
            },
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

    async def list_members(self, *, include_pets=False):
        return [
            {
                "id": 7,
                "name": "Saeed",
                "role": "adult",
            }
        ]


def test_about_you_renders_learned_knowledge() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.knowledge_graph = _FakeKnowledgeGraph()

    with TestClient(app) as client:
        resp = client.get("/dashboard/about-you")

    assert resp.status_code == 200
    # Header switched from "About You" to "About <member>" when a member exists.
    assert "About Saeed" in resp.text
    # Personal device shows up; home device (Washer) does NOT show as a card.
    assert "Saeed&#39;s iPhone" in resp.text or "Saeed's iPhone" in resp.text
    assert 'data-thing-id="1"' not in resp.text  # Washer thing not in personal devices
    assert 'data-thing-id="2"' in resp.text       # iPhone is
    # Other knowledge sections still rendered (they're not member-filtered yet).
    assert "user.coffee_brew" in resp.text
    assert "lights.after_sunset" in resp.text
    assert "Laundry day" in resp.text
    # Page chrome
    assert "/static/_design.css" in resp.text
    assert "/static/_app.js" in resp.text
    assert "/static/about_you.css" in resp.text
    assert "/static/about_you.js" in resp.text
    assert 'id="edit-modal"' in resp.text
    assert 'id="evidence-modal"' in resp.text
    assert "toast-stack" in resp.text
    # Tabs + drill-down DOM contract
    assert 'class="ay-tab' in resp.text
    assert 'data-tab="devices"' in resp.text
    assert 'data-thing-id="2"' in resp.text
    assert 'id="dev-detail"' in resp.text


def test_about_you_filters_to_specified_member() -> None:
    """?member=99 selects a member that doesn't exist → falls back to first."""
    app = FastAPI()
    app.include_router(router)
    app.state.knowledge_graph = _FakeKnowledgeGraph()

    with TestClient(app) as client:
        resp = client.get("/dashboard/about-you?member=99")

    assert resp.status_code == 200
    # Falls back to the first member (Saeed) since 99 doesn't exist.
    assert "About Saeed" in resp.text


# ── Profile (user_profile rows) surfacing ────────────────────────────────


def test_about_you_renders_profile_answers_from_user_profile() -> None:
    """REGRESSION: user repeatedly asked 'are my answered questions used?'.
    Audit found dashboard.py only loaded knowledge graph rows; the 10
    user_profile entries were silently ignored. This test pins the new
    'About me' tab + per-entry rendering."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)

    class _Graph(_FakeKnowledgeGraph):
        async def list_members(self, *, include_pets=False):
            return [{"id": 1, "name": "Saeed", "role": "adult"}]

    app.state.knowledge_graph = _Graph()
    app.state.reflection_store = SimpleNamespace(
        list_profile=AsyncMock(
            return_value=[
                {
                    "key": "dietary_restrictions",
                    "value": '"I only eat Halal food, not a big fan of seafood"',
                    "source": "morning_brief",
                    "confidence": 1.0,
                    "updated_at": "2026-05-12T22:31:00+00:00",
                },
                {
                    "key": "wake_time",
                    "value": '"I wake up no later than 9:00 AM on weekdays"',
                    "source": "morning_brief",
                    "confidence": 1.0,
                    "updated_at": "2026-05-12T22:30:00+00:00",
                },
                {
                    "key": "allergies",
                    "value": '"(skipped)"',
                    "source": "user_skipped",
                    "confidence": 0.0,
                    "updated_at": "2026-05-12T22:31:00+00:00",
                },
            ]
        )
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard/about-you")

    assert resp.status_code == 200
    text = resp.text
    # 'About me' tab present and active
    assert 'data-tab="profile"' in text
    assert 'data-pane="profile"' in text
    # Surfaced answers (humanised, JSON-quotes stripped)
    assert "Halal food" in text
    assert "wake up no later than 9:00 AM" in text
    # The label is humanised (sleep_time -> 'Sleep time' style)
    assert "Dietary restrictions" in text
    assert "Wake time" in text
    # Skipped entries are filtered out — the user shouldn't see them on
    # the visible 'About me' surface
    assert "Allergies" not in text


def test_about_you_renders_empty_profile_state_when_no_answers() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)
    app.state.knowledge_graph = _FakeKnowledgeGraph()
    app.state.reflection_store = SimpleNamespace(
        list_profile=AsyncMock(return_value=[])
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard/about-you")
    assert resp.status_code == 200
    assert "No profile answers yet" in resp.text


def test_about_you_survives_profile_load_failure() -> None:
    """A flaky reflection_store.list_profile must not break the page."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)
    app.state.knowledge_graph = _FakeKnowledgeGraph()
    app.state.reflection_store = SimpleNamespace(
        list_profile=AsyncMock(side_effect=Exception("connection lost"))
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard/about-you")
    assert resp.status_code == 200
    # Empty-state copy still renders
    assert "No profile answers yet" in resp.text
