from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.admin import router


class _FakeStore:
    def __init__(self, profile=None) -> None:
        self.profile = profile or []
        self.upsert_profile = AsyncMock()

    async def list_profile(self):
        return self.profile


class _FakeGraph:
    def __init__(self, *, things=None, habits=None, members=None) -> None:
        self.things = things or []
        self.habits = habits or []
        self.members = members or []
        self.list_members = AsyncMock(side_effect=self._list_members)
        self.put_member = AsyncMock(return_value={"id": 1, "name": "Saeed", "role": "adult"})
        self.forget_member = AsyncMock(return_value=None)

    async def list_things(self):
        return self.things

    async def list_habits(self):
        return self.habits

    async def _list_members(self, include_pets: bool = True):
        if include_pets:
            return self.members
        return [member for member in self.members if member.get("role") != "pet"]


def _app(*, graph=None, store=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if graph is not None:
        app.state.knowledge_graph = graph
    app.state.reflection_store = store or _FakeStore()
    app.state.registry = SimpleNamespace(
        dispatch=AsyncMock(
            return_value={
                "ok": True,
                "result": {
                    "items": [
                        {"entity_id": "sensor.washer"},
                        {"entity_id": "sensor.dryer"},
                    ]
                },
            }
        )
    )
    return app


def _appliances(count: int) -> list[dict]:
    return [
        {"id": idx, "type": f"appliance.test_{idx}", "friendly_name": f"Appliance {idx}"}
        for idx in range(count)
    ]


def _profile(*keys: str, completed: bool = False) -> list[dict]:
    rows = [{"key": key, "value": "07:00" if key.endswith("time") else "weekdays"} for key in keys]
    if completed:
        rows.append({"key": "onboarding_completed", "value": True})
    return rows


def test_stage_detection_transitions() -> None:
    cases = [
        (_FakeGraph(things=[]), _FakeStore(), 1),
        (_FakeGraph(things=_appliances(3)), _FakeStore(profile=_profile("wake_time")), 2),
        (
            _FakeGraph(things=_appliances(3), members=[]),
            _FakeStore(profile=_profile("wake_time", "sleep_time", "work_hours")),
            3,
        ),
        (
            _FakeGraph(things=_appliances(3), members=[{"id": 1, "name": "Saeed"}]),
            _FakeStore(profile=_profile("wake_time", "sleep_time", "work_hours")),
            4,
        ),
        (
            _FakeGraph(
                things=_appliances(3),
                members=[{"id": 1, "name": "Saeed"}],
                habits=[{"id": 1, "subject": "coffee", "last_confirmed_at": "now"}],
            ),
            _FakeStore(profile=_profile("wake_time", "sleep_time", "work_hours", completed=True)),
            "complete",
        ),
    ]
    for graph, store, expected_stage in cases:
        with TestClient(_app(graph=graph, store=store)) as client:
            resp = client.get("/admin/onboarding/stage")
        assert resp.status_code == 200
        assert resp.json()["stage"] == expected_stage


def test_stage_detection_reports_missing_profile_keys() -> None:
    graph = _FakeGraph(things=_appliances(3))
    store = _FakeStore(profile=_profile("wake_time"))

    with TestClient(_app(graph=graph, store=store)) as client:
        resp = client.get("/admin/onboarding/stage")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stage"] == 2
    assert body["summary"]["missing_profile_keys"] == ["sleep_time", "work_hours"]


def test_complete_persists_profile_flag() -> None:
    graph = _FakeGraph(things=_appliances(3))
    store = _FakeStore()

    with TestClient(_app(graph=graph, store=store)) as client:
        resp = client.post("/admin/onboarding/complete")

    assert resp.status_code == 200
    store.upsert_profile.assert_awaited_once_with(
        key="onboarding_completed",
        value=True,
        confidence=1.0,
        source="onboarding_wizard",
    )


def test_household_list_upsert_and_forget() -> None:
    graph = _FakeGraph(members=[{"id": 1, "name": "Saeed", "role": "adult"}])

    with TestClient(_app(graph=graph)) as client:
        listed = client.get("/admin/household/list")
        upserted = client.post(
            "/admin/household/upsert",
            json={
                "name": "Saeed",
                "role": "adult",
                "telegram_chat_id": "123",
                "allergies": ["peanuts"],
                "dietary_restrictions": "vegetarian",
                "sleep_time": "22:30",
                "wake_time": "07:00",
            },
        )
        forgotten = client.post("/admin/household/forget", json={"id": 1})

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert upserted.status_code == 200
    assert forgotten.status_code == 200
    kwargs = graph.put_member.await_args.kwargs
    assert kwargs["telegram_chat_id"] == 123
    assert kwargs["allergies"] == ["peanuts"]
    assert kwargs["dietary_restrictions"] == ["vegetarian"]
    assert kwargs["sleep_time"].hour == 22
    graph.forget_member.assert_awaited_once_with(1)


def test_household_endpoints_validate_bad_shapes_and_no_graph() -> None:
    with TestClient(_app(graph=_FakeGraph())) as client:
        bad_role = client.post(
            "/admin/household/upsert",
            json={"name": "A", "role": "alien"},
        )
        bad_time = client.post(
            "/admin/household/upsert",
            json={"name": "A", "sleep_time": "25:00"},
        )
        assert bad_role.status_code == 400
        assert bad_time.status_code == 400
        assert client.post("/admin/household/forget", json={}).status_code == 400

    with TestClient(_app(graph=None)) as client:
        assert client.get("/admin/onboarding/stage").status_code == 503
        assert client.get("/admin/household/list").status_code == 503
