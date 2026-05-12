from __future__ import annotations

import pytest

from tools import registry


class _FakeKnowledgeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _remember(self, method: str, *args, **kwargs) -> None:
        self.calls.append((method, args, kwargs))

    async def list_things(self, type=None):
        self._remember("list_things", type=type)
        return [{"id": 1, "friendly_name": "Washer"}]

    async def put_thing(self, **kwargs):
        self._remember("put_thing", **kwargs)
        return {"id": 2, **kwargs}

    async def forget_thing(self, thing_id):
        self._remember("forget_thing", thing_id)
        return True

    async def confirm_thing(self, thing_id):
        self._remember("confirm_thing", thing_id)
        return {"id": thing_id, "last_confirmed_at": "now"}

    async def list_members(self, include_pets=True):
        self._remember("list_members", include_pets=include_pets)
        return [{"id": 1, "name": "Saeed", "role": "adult"}]

    async def put_member(self, **kwargs):
        self._remember("put_member", **kwargs)
        return {"id": kwargs.get("member_id") or 2, **kwargs}

    async def forget_member(self, member_id):
        self._remember("forget_member", member_id)
        return None

    async def list_habits(self, subject=None):
        self._remember("list_habits", subject=subject)
        return [{"id": 3, "subject": "user.coffee_brew"}]

    async def put_habit(self, **kwargs):
        self._remember("put_habit", **kwargs)
        return {"id": 4, **kwargs}

    async def forget_habit(self, habit_id):
        self._remember("forget_habit", habit_id)
        return True

    async def confirm_habit(self, habit_id):
        self._remember("confirm_habit", habit_id)
        return {"id": habit_id, "last_observed_at": "now"}

    async def list_preferences(self):
        self._remember("list_preferences")
        return [{"key": "lights", "value": {"color": "warm"}}]

    async def put_preference(self, **kwargs):
        self._remember("put_preference", **kwargs)
        return kwargs

    async def forget_preference(self, key):
        self._remember("forget_preference", key)
        return True

    async def list_routines(self):
        self._remember("list_routines")
        return [{"id": 5, "name": "Laundry day"}]

    async def put_routine(self, **kwargs):
        self._remember("put_routine", **kwargs)
        return {"id": 6, **kwargs}

    async def forget_routine(self, routine_id):
        self._remember("forget_routine", routine_id)
        return True


@pytest.fixture
def fake_graph(monkeypatch) -> _FakeKnowledgeGraph:
    fake = _FakeKnowledgeGraph()

    async def _fake_graph() -> _FakeKnowledgeGraph:
        return fake

    monkeypatch.setattr(registry, "_knowledge_graph", _fake_graph)
    return fake


@pytest.mark.asyncio
async def test_things_capabilities_contract(fake_graph: _FakeKnowledgeGraph) -> None:
    listed = await registry.list_things(type="appliance.washer")
    put = await registry.put_thing(
        type="appliance.washer",
        friendly_name="Washer",
        attributes={"brand": "LG"},
        ha_entity_ids=["sensor.washer"],
        confidence=0.8,
        source="event_log",
    )
    forgotten = await registry.forget_thing(2)
    confirmed = await registry.confirm_thing(2)

    assert listed == {"items": [{"id": 1, "friendly_name": "Washer"}], "count": 1}
    assert put["ok"] is True
    assert put["thing"]["attributes"] == {"brand": "LG"}
    assert forgotten == {"ok": True, "deleted": True, "thing_id": 2}
    assert confirmed["thing"]["last_confirmed_at"] == "now"
    assert fake_graph.calls[0][0] == "list_things"


@pytest.mark.asyncio
async def test_members_capabilities_contract(fake_graph: _FakeKnowledgeGraph) -> None:
    listed = await registry.list_members(include_pets=False)
    put = await registry.put_member(
        name="Saeed",
        role="adult",
        telegram_chat_id=123,
        allergies=["peanuts"],
        dietary_restrictions=["vegetarian"],
        sleep_time="22:30",
        wake_time="07:00",
        attributes={"room": "primary"},
    )
    forgotten = await registry.forget_member(2)

    assert listed == {"items": [{"id": 1, "name": "Saeed", "role": "adult"}], "count": 1}
    assert put["ok"] is True
    assert put["member"]["telegram_chat_id"] == 123
    assert forgotten == {"ok": True, "member_id": 2}
    assert fake_graph.calls[-1][0] == "forget_member"


@pytest.mark.asyncio
async def test_habits_capabilities_contract(monkeypatch) -> None:
    fake = _FakeKnowledgeGraph()

    async def _fake_graph() -> _FakeKnowledgeGraph:
        return fake

    monkeypatch.setattr(registry, "_knowledge_graph", _fake_graph)

    listed = await registry.list_habits(subject="user.coffee_brew")
    put = await registry.put_habit(
        subject="user.coffee_brew",
        pattern={"days_of_week": ["mon"], "time_window_local": "07:00-07:30"},
        frequency="weekdays",
        confidence=0.7,
        source="event_log",
    )
    forgotten = await registry.forget_habit(4)
    confirmed = await registry.confirm_habit(4)

    assert listed["count"] == 1
    assert put["ok"] is True
    assert put["habit"]["frequency"] == "weekdays"
    assert forgotten["deleted"] is True
    assert confirmed["habit"]["last_observed_at"] == "now"


@pytest.mark.asyncio
async def test_preferences_capabilities_contract(fake_graph: _FakeKnowledgeGraph) -> None:
    listed = await registry.list_preferences()
    put = await registry.put_preference(
        key="lights.after_sunset",
        value={"color": "warm"},
        confidence=0.9,
        source="user",
    )
    forgotten = await registry.forget_preference("lights.after_sunset")

    assert listed["items"][0]["key"] == "lights"
    assert put == {
        "ok": True,
        "preference": {
            "key": "lights.after_sunset",
            "value": {"color": "warm"},
            "confidence": 0.9,
            "source": "user",
        },
    }
    assert forgotten == {"ok": True, "deleted": True, "key": "lights.after_sunset"}
    assert fake_graph.calls[-1][0] == "forget_preference"


@pytest.mark.asyncio
async def test_routines_capabilities_contract(fake_graph: _FakeKnowledgeGraph) -> None:
    listed = await registry.list_routines()
    put = await registry.put_routine(
        name="Laundry day",
        steps=[{"thing": "Washer", "action": "run bedding cycle"}],
        schedule={"day": "sun"},
        source="user",
    )
    forgotten = await registry.forget_routine(6)

    assert listed["count"] == 1
    assert put["ok"] is True
    assert put["routine"]["schedule"] == {"day": "sun"}
    assert forgotten == {"ok": True, "deleted": True, "routine_id": 6}


@pytest.mark.asyncio
async def test_registry_tools_noop_when_store_unavailable(monkeypatch) -> None:
    async def _broken_graph():
        raise RuntimeError("database offline")

    monkeypatch.setattr(registry, "_knowledge_graph", _broken_graph)

    assert await registry.list_things() == {
        "items": [],
        "count": 0,
        "error": "knowledge_graph_unavailable",
    }
    assert await registry.put_preference("x", {}) == {
        "ok": False,
        "error": "knowledge_graph_unavailable",
    }
