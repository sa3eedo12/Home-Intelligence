from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tools import tv_inference


class _FakeStore:
    def __init__(self) -> None:
        self.insert_kwargs: dict[str, Any] | None = None
        self.confirm_calls: list[dict[str, Any]] = []
        self.auto_off_args: dict[str, Any] | None = None

    async def insert(self, **kwargs: Any) -> int:
        self.insert_kwargs = kwargs
        return 42

    async def confirm(
        self,
        tv_left_on_id: int,
        *,
        action: str,
        chat_id: int | None = None,
    ) -> dict[str, Any]:
        self.confirm_calls.append(
            {"tv_left_on_id": tv_left_on_id, "action": action, "chat_id": chat_id}
        )
        return {
            "id": tv_left_on_id,
            "entity_id": "media_player.living_room_tv",
            "friendly_name": "Living Room TV",
            "confirmed_action": action,
        }

    async def enable_auto_off_at_bedtime(
        self,
        *,
        entity_id: str,
        friendly_name: str | None = None,
    ) -> dict[str, Any]:
        self.auto_off_args = {"entity_id": entity_id, "friendly_name": friendly_name}
        return {"id": 9, "attributes": {"auto_off_at_bedtime": True}}

    async def recent(
        self,
        *,
        limit: int = 20,
        only_unconfirmed: bool = False,
    ) -> list[dict[str, Any]]:
        return [{"id": 1, "entity_id": "media_player.living_room_tv"}]


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore()

    async def _fake_pool() -> object:
        return object()

    monkeypatch.setattr(tv_inference, "_pool", _fake_pool)
    monkeypatch.setattr(tv_inference, "TvLeftOnStore", lambda _pool: store)
    return store


@pytest.mark.asyncio
async def test_suggest_tv_action_persists_and_returns_keyboard(fake_store: _FakeStore) -> None:
    result = await tv_inference.suggest_tv_action(
        entity_id="media_player.living_room_tv",
        friendly_name="Living Room TV",
        on_since="2026-01-01T10:00:00Z",
        on_hours="6.5",
        reason="past_bedtime",
        event_log_id="123",
    )

    assert result["ok"] is True
    assert result["tv_left_on_id"] == 42
    assert result["suggested_action"] == "always_off_at_bedtime"
    callbacks = [button["callback"] for row in result["keyboard"] for button in row]
    assert callbacks == [
        "tv:42:turn_off",
        "tv:42:snooze",
        "tv:42:always_off_at_bedtime",
        "tv:42:skip",
    ]
    assert fake_store.insert_kwargs is not None
    assert fake_store.insert_kwargs["entity_id"] == "media_player.living_room_tv"
    assert fake_store.insert_kwargs["event_log_id"] == 123
    assert fake_store.insert_kwargs["on_since"] == datetime(2026, 1, 1, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_confirm_tv_action_turns_off_entity(
    fake_store: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_turn_off(entity_id: str) -> dict[str, Any]:
        return {"ok": True, "entity_id": entity_id}

    monkeypatch.setattr(tv_inference, "_turn_off_entity", _fake_turn_off)

    result = await tv_inference.confirm_tv_action(42, "turn_off", chat_id=12345)

    assert result["ok"] is True
    assert result["turn_off"] == {"ok": True, "entity_id": "media_player.living_room_tv"}
    assert fake_store.confirm_calls == [
        {"tv_left_on_id": 42, "action": "turn_off", "chat_id": 12345}
    ]


@pytest.mark.asyncio
async def test_confirm_tv_action_sets_auto_off_attribute(fake_store: _FakeStore) -> None:
    result = await tv_inference.confirm_tv_action(42, "always_off_at_bedtime", chat_id=123)

    assert result["ok"] is True
    assert result["auto_off_at_bedtime"]["attributes"]["auto_off_at_bedtime"] is True
    assert fake_store.auto_off_args == {
        "entity_id": "media_player.living_room_tv",
        "friendly_name": "Living Room TV",
    }


@pytest.mark.asyncio
async def test_recent_tv_left_on_returns_items(fake_store: _FakeStore) -> None:
    result = await tv_inference.recent_tv_left_on(limit=5, only_unconfirmed=True)

    assert result == {
        "ok": True,
        "items": [{"id": 1, "entity_id": "media_player.living_room_tv"}],
        "count": 1,
    }
