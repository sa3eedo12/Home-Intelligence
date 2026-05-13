from __future__ import annotations

from typing import Any

import pytest

from tools import auto_infer


class _FakeStore:
    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.insert_calls: list[dict[str, Any]] = []
        self.confirm_calls: list[dict[str, Any]] = []
        self.reject_calls: list[dict[str, Any]] = []
        self.record: dict[str, Any] | None = {
            "id": 7,
            "status": "proposed",
            "proposed_action": {
                "agent": "knowledge_notes",
                "capability": "record_event",
                "payload": {
                    "agent": "personal_assistant",
                    "capability": "inferred_event",
                    "summary": "Logged an inferred event",
                    "payload": {"source": "infer"},
                },
            },
        }

    async def recent_count_in_window(self, *, hours: int = 1) -> int:
        assert hours == 1
        return self.count

    async def insert(self, **kwargs: Any) -> int:
        self.insert_calls.append(kwargs)
        return 77

    async def get(self, auto_inference_id: int) -> dict[str, Any] | None:
        assert auto_inference_id == 7
        return self.record

    async def confirm(
        self,
        auto_inference_id: int,
        *,
        chat_id: int | None = None,
        action_result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        self.confirm_calls.append(
            {"id": auto_inference_id, "chat_id": chat_id, "action_result": action_result}
        )
        return {"id": auto_inference_id, "status": "confirmed", "chat_id": chat_id}

    async def reject(
        self,
        auto_inference_id: int,
        *,
        status: str = "rejected",
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        self.reject_calls.append({"id": auto_inference_id, "status": status, "chat_id": chat_id})
        return {"id": auto_inference_id, "status": status, "chat_id": chat_id}


@pytest.mark.asyncio
async def test_auto_infer_skips_known_kinds(monkeypatch) -> None:
    async def _fail_store() -> _FakeStore:
        raise AssertionError("store should not be used for skipped kinds")

    monkeypatch.setattr(auto_infer, "_auto_store", _fail_store)

    result = await auto_infer.auto_infer_observer_event(
        kind="coffee.brewed",
        summary="Coffee brewed by Espresso",
        payload={"entity_id": "sensor.espresso"},
    )

    assert result == {"ok": True, "skipped": True, "reason": "skip_kind:coffee.brewed"}


@pytest.mark.asyncio
async def test_auto_infer_rate_limits_before_llm(monkeypatch) -> None:
    store = _FakeStore(count=5)

    async def _store() -> _FakeStore:
        return store

    async def _fail_infer(_context: str) -> dict[str, Any]:
        raise AssertionError("infer should not be called while rate-limited")

    monkeypatch.setenv("AUTO_INFER_HOURLY_CAP", "5")
    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _fail_infer)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.sleep",
        kind="sleep.likely_asleep",
        summary="Bedroom signals suggest everyone is likely asleep",
        payload={"signals": {"bedroom_lights_off": True, "tv_off": True}},
    )

    assert result == {"ok": True, "skipped": True, "reason": "rate_limit"}
    assert store.insert_calls == []


@pytest.mark.asyncio
async def test_auto_infer_confidence_gate_skips_notification(monkeypatch) -> None:
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _infer(_context: str) -> dict[str, Any]:
        return {
            "inference": "Maybe someone went to bed",
            "confidence": 0.42,
            "proposed_action": None,
        }

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _infer)

    result = await auto_infer.auto_infer_observer_event(
        kind="sleep.likely_asleep",
        summary="Bedroom signals suggest everyone is likely asleep",
        payload={"signals": {"bedroom_lights_off": True, "tv_off": True}},
    )

    assert result["skipped"] is True
    assert result["reason"] == "confidence_gate"
    assert store.insert_calls == []


@pytest.mark.asyncio
async def test_auto_infer_persists_high_confidence_proposal(monkeypatch) -> None:
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    action = {
        "agent": "knowledge_notes",
        "capability": "record_event",
        "payload": {
            "agent": "personal_assistant",
            "capability": "inferred_event",
            "summary": "Went to bed around 11 PM",
            "payload": {"source": "infer"},
        },
    }

    async def _infer(context: str) -> dict[str, Any]:
        assert "sleep.likely_asleep" in context
        return {
            "inference": "you went to bed around 11 PM",
            "confidence": 0.81,
            "proposed_action": action,
        }

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _infer)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.sleep",
        kind="sleep.likely_asleep",
        summary="Bedroom signals suggest everyone is likely asleep",
        payload={"event_log_id": 12, "signals": {"bedroom_lights_off": True, "tv_off": True}},
    )

    assert result["ok"] is True
    assert result["auto_inference_id"] == 77
    assert result["keyboard"][0][0]["callback"] == "infer:77:confirmed"
    assert "Did you just went to bed around 11 PM?" in result["summary"]
    assert store.insert_calls[0]["source_event_log_id"] == 12
    assert store.insert_calls[0]["source_kind"] == "sleep.likely_asleep"
    assert store.insert_calls[0]["proposed_action"] == action


@pytest.mark.asyncio
async def test_confirm_auto_inference_dispatches_and_confirms(monkeypatch) -> None:
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _dispatch(action: dict[str, Any]) -> dict[str, Any]:
        assert action["agent"] == "knowledge_notes"
        assert action["capability"] == "record_event"
        return {"ok": True, "result": {"ok": True, "event": {"id": 99}}}

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer, "_dispatch_proposed_action", _dispatch)

    result = await auto_infer.confirm_auto_inference(
        auto_inference_id=7,
        status="confirmed",
        chat_id=123,
    )

    assert result["ok"] is True
    assert store.confirm_calls == [
        {
            "id": 7,
            "chat_id": 123,
            "action_result": {"ok": True, "result": {"ok": True, "event": {"id": 99}}},
        }
    ]


@pytest.mark.asyncio
async def test_confirm_auto_inference_rejects_without_dispatch(monkeypatch) -> None:
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _dispatch(_action: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("dispatch should not run for rejected proposals")

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer, "_dispatch_proposed_action", _dispatch)

    result = await auto_infer.confirm_auto_inference(
        auto_inference_id=7,
        status="rejected",
        chat_id=123,
    )

    assert result["ok"] is True
    assert store.reject_calls == [{"id": 7, "status": "rejected", "chat_id": 123}]


@pytest.mark.asyncio
async def test_confirm_auto_inference_does_not_confirm_failed_dispatch(monkeypatch) -> None:
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _dispatch(_action: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "offline"}

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer, "_dispatch_proposed_action", _dispatch)

    result = await auto_infer.confirm_auto_inference(
        auto_inference_id=7,
        status="confirmed",
        chat_id=123,
    )

    assert result["ok"] is False
    assert result["error"] == "proposed_action_failed"
    assert store.confirm_calls == []
