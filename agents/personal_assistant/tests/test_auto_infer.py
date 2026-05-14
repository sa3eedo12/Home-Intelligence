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
            "inference": "Maybe a guest just arrived",
            "confidence": 0.42,
            "proposed_action": None,
        }

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _infer)

    # Use a kind WITHOUT a rule-based producer so the LLM path is exercised.
    result = await auto_infer.auto_infer_observer_event(
        kind="garage.opened_unexpectedly",
        summary="Garage door opened with nobody home",
        payload={"signals": {"door_state": "open"}},
    )

    assert result["skipped"] is True
    assert result["reason"] == "confidence_gate"
    assert result["confidence"] == pytest.approx(0.42)
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
            "summary": "Saeed seems to have started a new routine",
            "payload": {"source": "infer"},
        },
    }

    async def _infer(context: str) -> dict[str, Any]:
        assert "garage.opened_unexpectedly" in context
        return {
            "inference": "Saeed seems to have started a new routine",
            "confidence": 0.81,
            "proposed_action": action,
        }

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _infer)

    # Use a kind WITHOUT a rule-based producer so we exercise the LLM path.
    result = await auto_infer.auto_infer_observer_event(
        agent="observer.garage",
        kind="garage.opened_unexpectedly",
        summary="Garage door opened with nobody home",
        payload={"event_log_id": 12, "signals": {"door_state": "open"}},
    )

    assert result["ok"] is True
    assert result["auto_inference_id"] == 77
    assert result["keyboard"][0][0]["callback"] == "infer:77:confirmed"
    assert "started a new routine" in result["summary"]
    assert store.insert_calls[0]["source_event_log_id"] == 12
    assert store.insert_calls[0]["source_kind"] == "garage.opened_unexpectedly"
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


# ── Rule-based inference path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_based_entertainment_left_on_persists_without_llm(monkeypatch) -> None:
    """REGRESSION: 0 auto_inferences ever in production despite many
    observer events. Root cause: the LLM prompt biases the model toward
    sub-threshold confidence. Rule-based path bypasses the LLM entirely
    for known kinds and produces a high-confidence inference straight
    from the envelope. Without this, entertainment.left_on would be
    routed to the LLM and confidence-gated out.
    """
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _fail_infer(_context: str) -> dict[str, Any]:
        raise AssertionError("LLM must NOT be called when a rule matches")

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _fail_infer)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.tv",
        kind="entertainment.left_on",
        summary="Living Room TV has been on for 6.0h (past_bedtime)",
        ts="2026-05-14T22:00:00+00:00",
        payload={
            "entity_id": "media_player.living_room_tv",
            "friendly_name": "Living Room TV",
            "on_hours": 6.0,
            "reason": "past_bedtime",
        },
    )

    assert result["ok"] is True
    assert result.get("skipped") is not True
    assert result["auto_inference_id"] == 77
    assert "Living Room TV" in result["inference"]
    assert "6.0h" in result["inference"]
    assert "past your usual bedtime" in result["inference"]
    assert result["confidence"] >= 0.6
    # The persisted action is a knowledge_notes.record_event with the
    # inference text as its summary, tagged with the rule id for audit.
    persisted = store.insert_calls[0]
    assert persisted["source_kind"] == "entertainment.left_on"
    action = persisted["proposed_action"]
    assert action["agent"] == "knowledge_notes"
    assert action["capability"] == "record_event"
    assert action["payload"]["payload"]["source"] == "rule:entertainment.left_on"


@pytest.mark.asyncio
async def test_rule_based_sleep_likely_asleep_uses_event_time(monkeypatch) -> None:
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _fail_infer(_context: str) -> dict[str, Any]:
        raise AssertionError("LLM must NOT be called when a rule matches")

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _fail_infer)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.sleep",
        kind="sleep.likely_asleep",
        summary="Bedroom signals suggest sleeping",
        ts="2026-05-14T23:35:00+00:00",
        payload={"signals": {"lights_off": True}},
    )

    assert result["ok"] is True
    assert result["auto_inference_id"] == 77
    assert "23:35" in result["inference"]
    assert store.insert_calls[0]["source_kind"] == "sleep.likely_asleep"


@pytest.mark.asyncio
async def test_rule_based_anomaly_detected_carries_summary(monkeypatch) -> None:
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _fail_infer(_context: str) -> dict[str, Any]:
        raise AssertionError("LLM must NOT be called when a rule matches")

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _fail_infer)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.system",
        kind="anomaly.detected",
        summary="Front door opened at 03:14 — household profile says everyone asleep",
        payload={"anomaly_type": "after_hours_door"},
    )

    assert result["ok"] is True
    assert result["auto_inference_id"] == 77
    assert "Front door opened at 03:14" in result["inference"]


@pytest.mark.asyncio
async def test_rule_based_inference_respects_rate_limit(monkeypatch) -> None:
    """Rules don't bypass the hourly cap — same throttle protects the user."""
    store = _FakeStore(count=5)

    async def _store() -> _FakeStore:
        return store

    monkeypatch.setenv("AUTO_INFER_HOURLY_CAP", "5")
    monkeypatch.setattr(auto_infer, "_auto_store", _store)

    result = await auto_infer.auto_infer_observer_event(
        kind="entertainment.left_on",
        ts="2026-05-14T22:00:00+00:00",
        payload={"on_hours": 6.0, "friendly_name": "TV"},
    )

    assert result == {"ok": True, "skipped": True, "reason": "rate_limit"}
    assert store.insert_calls == []


@pytest.mark.asyncio
async def test_min_confidence_is_env_configurable(monkeypatch) -> None:
    """Operators can tune the floor without redeploying. Setting it above
    every rule's confidence makes everything skip."""
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    monkeypatch.setenv("AUTO_INFER_MIN_CONFIDENCE", "0.99")
    monkeypatch.setattr(auto_infer, "_auto_store", _store)

    result = await auto_infer.auto_infer_observer_event(
        kind="entertainment.left_on",
        ts="2026-05-14T22:00:00+00:00",
        payload={"on_hours": 6.0, "friendly_name": "TV"},
    )

    assert result["skipped"] is True
    assert result["reason"] == "confidence_gate"


def test_rule_based_inference_unknown_kind_returns_none() -> None:
    assert auto_infer._rule_based_inference({"kind": "garage.opened"}) is None
    assert auto_infer._rule_based_inference({}) is None
