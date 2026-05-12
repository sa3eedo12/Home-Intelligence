from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


class _Store:
    def __init__(self, profile=None) -> None:
        self.profile = profile or []

    async def list_profile(self):
        return self.profile


class _Graph:
    def __init__(self, *, things=None, habits=None, members=None) -> None:
        self.things = things or []
        self.habits = habits or []
        self.members = members or []

    async def list_things(self):
        return self.things

    async def list_habits(self):
        return self.habits

    async def list_members(self, include_pets: bool = True):
        if include_pets:
            return self.members
        return [member for member in self.members if member.get("role") != "pet"]


def _appliances(count: int) -> list[dict]:
    return [
        {"id": idx, "type": "appliance.washer", "friendly_name": f"Thing {idx}"}
        for idx in range(count)
    ]


def _profile(*keys: str, completed: bool = False) -> list[dict]:
    rows = [{"key": key, "value": "07:00" if key.endswith("time") else "weekdays"} for key in keys]
    if completed:
        rows.append({"key": "onboarding_completed", "value": True})
    return rows


def _app(graph: _Graph, store: _Store) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.knowledge_graph = graph
    app.state.reflection_store = store
    app.state.registry = SimpleNamespace(
        dispatch=AsyncMock(return_value={"ok": True, "result": {"items": [{"entity_id": "a"}]}})
    )
    return app


def _page(graph: _Graph, store: _Store) -> str:
    with TestClient(_app(graph, store)) as client:
        resp = client.get("/dashboard/onboarding")
    assert resp.status_code == 200
    return resp.text


def _json_state(html: str) -> dict:
    match = re.search(
        r'<script id="onboarding-data" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_onboarding_renders_stage_one_discovery_stepper() -> None:
    html = _page(_Graph(things=[]), _Store())

    assert "Stage 1 — Discovery" in html
    assert 'data-stage="1" class="step current' in html
    assert "/dashboard/discovery" in html


def test_onboarding_renders_stage_two_missing_fields_for_js() -> None:
    html = _page(_Graph(things=_appliances(3)), _Store(profile=_profile("wake_time")))
    state = _json_state(html)

    assert "Stage 2 — Routines" in html
    assert 'data-stage="2" class="step current' in html
    assert state["summary"]["missing_profile_keys"] == ["sleep_time", "work_hours"]
    assert 'data-missing-keys="sleep_time,work_hours"' in html


def test_onboarding_renders_stage_three_household_members() -> None:
    html = _page(
        _Graph(things=_appliances(3), members=[]),
        _Store(profile=_profile("wake_time", "sleep_time", "work_hours")),
    )

    assert "Stage 3 — Household" in html
    assert 'data-stage="3" class="step current' in html
    assert "member-form" in html


def test_onboarding_renders_stage_four_habit_cards_and_complete() -> None:
    graph = _Graph(
        things=_appliances(3),
        members=[{"id": 1, "name": "Saeed", "role": "adult"}],
        habits=[{"id": 7, "subject": "coffee", "pattern": {}, "last_confirmed_at": None}],
    )
    html = _page(graph, _Store(profile=_profile("wake_time", "sleep_time", "work_hours")))

    assert "Stage 4 — Habits" in html
    assert 'data-stage="4" class="step current' in html
    assert "coffee" in html
    assert "/admin/knowledge/confirm" not in html

    complete_html = _page(
        _Graph(
            things=_appliances(3),
            members=[{"id": 1, "name": "Saeed", "role": "adult"}],
            habits=[{"id": 7, "subject": "coffee", "pattern": {}, "last_confirmed_at": "now"}],
        ),
        _Store(profile=_profile("wake_time", "sleep_time", "work_hours", completed=True)),
    )
    assert "You're onboarded" in complete_html
    assert 'data-stage="4" class="step  complete' in complete_html
