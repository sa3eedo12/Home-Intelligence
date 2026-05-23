from __future__ import annotations

from typing import Any

import pytest

from tools import auto_infer


class _FakeStore:
    def __init__(
        self,
        count: int = 0,
        corrections: dict[str, int] | None = None,
    ) -> None:
        self.count = count
        self._corrections = corrections or {"confirmed": 0, "rejected": 0, "skipped": 0}
        self.insert_calls: list[dict[str, Any]] = []
        self.confirm_calls: list[dict[str, Any]] = []
        self.reject_calls: list[dict[str, Any]] = []
        self.correction_lookups: list[dict[str, Any]] = []
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

    async def recent_for_inference(
        self, *, source_kind: str, inference: str, hours: int = 6
    ) -> int:
        # Default fixture returns 0 (no dedup hit); individual tests can
        # subclass / monkeypatch to simulate a dedup hit.
        return getattr(self, "_dedup_count", 0)

    async def correction_counts(
        self, *, source_kind: str, days: int = 7
    ) -> dict[str, int]:
        self.correction_lookups.append({"source_kind": source_kind, "days": days})
        return dict(self._corrections)

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
async def test_auto_infer_times_out_long_running_llm_calls(monkeypatch) -> None:
    """Regression for proposal #75 — production logs showed observer
    inferences exceeding 17 seconds and queueing every other reactive
    trigger behind them. The wait_for cap should turn these into a
    clean 'skipped: llm_timeout' so the rest of the pipeline keeps
    flowing instead of blocking on a hung LLM call.
    """
    import asyncio as _asyncio

    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _slow_infer(_context: str) -> dict[str, Any]:
        await _asyncio.sleep(10)  # well past the 0.1s timeout below
        return {"inference": "should not reach here", "confidence": 0.9}

    # Aggressively short timeout so the test stays fast.
    monkeypatch.setenv("AUTO_INFER_LLM_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _slow_infer)

    result = await auto_infer.auto_infer_observer_event(
        kind="garage.opened_unexpectedly",
        summary="Garage door opened with nobody home",
        payload={"signals": {"door_state": "open"}},
    )

    assert result == {"ok": True, "skipped": True, "reason": "llm_timeout"}
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

    # Pin TZ so the test is deterministic regardless of dev machine.
    monkeypatch.setenv("TZ", "Asia/Dubai")
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
    # 23:35 UTC = 03:35 in Asia/Dubai — the inference must render LOCAL time
    # not UTC. Regression: user saw "lightbulb turned on at 06:53:14" when
    # local was 10:53; the rendering used to skip the TZ conversion.
    assert "03:35" in result["inference"]
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


# ── User-correction memory ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_back_off_after_repeated_user_rejections(monkeypatch) -> None:
    """If the user has rejected/skipped this kind 3+ times in 7 days,
    the system stops proposing — the start of the 'actually learns from
    feedback' loop."""
    store = _FakeStore(count=0, corrections={"confirmed": 0, "rejected": 3, "skipped": 0})

    async def _store() -> _FakeStore:
        return store

    async def _fail_infer(_context: str) -> dict[str, Any]:
        raise AssertionError("LLM must NOT be called when backed off")

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _fail_infer)

    result = await auto_infer.auto_infer_observer_event(
        kind="entertainment.left_on",
        ts="2026-05-14T22:00:00+00:00",
        payload={"on_hours": 6.0, "friendly_name": "TV"},
    )

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "user_corrections"
    assert result["corrections"] == {"confirmed": 0, "rejected": 3, "skipped": 0}
    assert store.insert_calls == []
    # The lookup happened with the right kind + default 7-day window
    assert store.correction_lookups[0]["source_kind"] == "entertainment.left_on"
    assert store.correction_lookups[0]["days"] == 7


@pytest.mark.asyncio
async def test_back_off_counts_skips_as_corrections(monkeypatch) -> None:
    """User clicking 'Skip' on the inline keyboard is also a 'no' signal —
    it shouldn't take three explicit rejections + three skips to mute."""
    store = _FakeStore(count=0, corrections={"confirmed": 0, "rejected": 1, "skipped": 2})

    async def _store() -> _FakeStore:
        return store

    monkeypatch.setattr(auto_infer, "_auto_store", _store)

    result = await auto_infer.auto_infer_observer_event(
        kind="entertainment.left_on",
        ts="2026-05-14T22:00:00+00:00",
        payload={"on_hours": 6.0, "friendly_name": "TV"},
    )

    assert result["skipped"] is True
    assert result["reason"] == "user_corrections"


@pytest.mark.asyncio
async def test_corrections_below_threshold_still_proposes(monkeypatch) -> None:
    """Two rejections in a week is normal noise — don't mute prematurely."""
    store = _FakeStore(count=0, corrections={"confirmed": 1, "rejected": 2, "skipped": 0})

    async def _store() -> _FakeStore:
        return store

    monkeypatch.setattr(auto_infer, "_auto_store", _store)

    result = await auto_infer.auto_infer_observer_event(
        kind="entertainment.left_on",
        ts="2026-05-14T22:00:00+00:00",
        payload={"on_hours": 6.0, "friendly_name": "TV"},
    )

    assert result["ok"] is True
    assert result.get("skipped") is not True
    assert result["auto_inference_id"] == 77


@pytest.mark.asyncio
async def test_repeated_confirms_lower_confidence_floor(monkeypatch) -> None:
    """If the user has confirmed this kind 3+ times recently, lower the
    floor so borderline-confidence inferences from the LLM still surface.
    This is the 'system learns what you DO want' half of the loop."""
    store = _FakeStore(count=0, corrections={"confirmed": 3, "rejected": 0, "skipped": 0})

    async def _store() -> _FakeStore:
        return store

    # An LLM result that would normally fail the 0.5 floor (returns 0.45)
    # but with reinforcement the floor drops to 0.4 so it passes.
    action = {
        "agent": "knowledge_notes",
        "capability": "record_event",
        "payload": {
            "agent": "personal_assistant",
            "capability": "inferred_event",
            "summary": "Reinforced inference",
            "payload": {"source": "infer"},
        },
    }

    async def _infer(_context: str) -> dict[str, Any]:
        return {
            "inference": "Reinforced inference",
            "confidence": 0.45,
            "proposed_action": action,
        }

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _infer)

    result = await auto_infer.auto_infer_observer_event(
        kind="garage.opened_unexpectedly",
        summary="Garage opened",
        payload={},
    )

    assert result["ok"] is True
    assert result.get("skipped") is not True
    assert result["confidence"] == pytest.approx(0.45)
    assert store.insert_calls[0]["source_kind"] == "garage.opened_unexpectedly"


@pytest.mark.asyncio
async def test_correction_lookup_failure_does_not_block_inference(monkeypatch) -> None:
    """A flaky DB on the correction-counts query must not break inference —
    we treat it as 'no corrections known' and let the inference proceed."""
    class _ExplodingStore(_FakeStore):
        async def correction_counts(self, *, source_kind: str, days: int = 7):
            raise Exception("connection lost")

    store = _ExplodingStore(count=0)

    async def _store() -> _ExplodingStore:
        return store

    monkeypatch.setattr(auto_infer, "_auto_store", _store)

    result = await auto_infer.auto_infer_observer_event(
        kind="entertainment.left_on",
        ts="2026-05-14T22:00:00+00:00",
        payload={"on_hours": 6.0, "friendly_name": "TV"},
    )

    assert result["ok"] is True
    assert result.get("skipped") is not True
    assert result["auto_inference_id"] == 77


# ── Cross-entity device-level dedup ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_skips_when_same_inference_already_recent(monkeypatch) -> None:
    """REGRESSION: one physical TV exposes 4 HA entities; without
    dedup the auto_infer persisted 4 separate "left X on for 6h past
    bedtime" rows. The dedup_key from the rule producer + the store's
    recent_for_inference lookup collapses these to a single row."""
    store = _FakeStore(count=0)
    store._dedup_count = 1  # simulate "we already inserted one in last 6h"

    async def _store() -> _FakeStore:
        return store

    async def _fail_infer(_context: str) -> dict[str, Any]:
        raise AssertionError("LLM must NOT be called when dedup hits")

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _fail_infer)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.tv",
        kind="entertainment.left_on",
        summary="some other TV entity also left on",
        ts="2026-05-14T22:00:00+00:00",
        payload={
            "entity_id": "media_player.34_odyssey_oled_g8_2",
            "friendly_name": "OLED G8",
            "on_hours": 6.0,
            "reason": "past_bedtime",
        },
    )

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "dedup_within_window"
    assert result["dedup_key"] == "entertainment.left_on:past_bedtime"
    assert store.insert_calls == []


@pytest.mark.asyncio
async def test_dedup_does_not_block_first_emission(monkeypatch) -> None:
    """First inference for a kind+key passes through normally."""
    store = _FakeStore(count=0)
    # _dedup_count defaults to 0 → no hit
    async def _store() -> _FakeStore:
        return store

    monkeypatch.setattr(auto_infer, "_auto_store", _store)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.tv",
        kind="entertainment.left_on",
        summary="first TV alert today",
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
    assert store.insert_calls


# ── Timezone conversion in LLM context (REGRESSION) ─────────────────────


def test_context_for_infer_converts_envelope_ts_to_local(monkeypatch) -> None:
    """REGRESSION: user saw 'lightbulb turned on at 06:53:14' when the
    actual local time was 10:53. The envelope's ts was UTC and the LLM
    repeated it verbatim. _context_for_infer must now hand the LLM the
    LOCAL ISO string + a hint that the timezone is local."""
    monkeypatch.setenv("TZ", "Asia/Dubai")
    envelope = {
        "agent": "observer.device_activity",
        "kind": "device.state_changed",
        "summary": "💡 The lightbulb was turned on",
        "ts": "2026-05-16T06:53:14+00:00",  # UTC
        "payload": {
            "entity_id": "light.lightbulb",
            "on_since": "2026-05-16T06:53:14+00:00",
        },
    }
    ctx = auto_infer._context_for_infer(envelope, "unhandled_observer_event")
    # The LOCAL time string must appear; the bare UTC '06:53' must NOT.
    assert "10:53" in ctx
    assert "Asia/Dubai" in ctx
    assert "all timestamps below are LOCAL time" in ctx


# ── Regression: _hh_mm must render in the configured local tz ───────


def test_hh_mm_converts_utc_to_local_tz(monkeypatch: Any) -> None:
    """User saw 'lightbulb turned on at 06:53:14' when actual local was
    10:53 (UTC+4). _hh_mm must convert to the configured TZ before
    formatting — never render raw UTC as if it were local time."""
    monkeypatch.setenv("TZ", "Asia/Dubai")  # UTC+4 year-round
    # 06:53 UTC = 10:53 in Dubai
    assert auto_infer._hh_mm("2026-05-21T06:53:14+00:00") == "10:53"
    # Trailing-Z form must also work
    assert auto_infer._hh_mm("2026-05-21T06:53:14Z") == "10:53"


def test_hh_mm_respects_user_tz_fallback(monkeypatch: Any) -> None:
    """When TZ env is unset USER_TZ is the fallback."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("USER_TZ", "America/New_York")
    # 14:00 UTC = 10:00 EDT in May
    assert auto_infer._hh_mm("2026-05-21T14:00:00Z") == "10:00"


def test_hh_mm_returns_empty_on_bad_input() -> None:
    assert auto_infer._hh_mm(None) == ""
    assert auto_infer._hh_mm(12345) == ""
    assert auto_infer._hh_mm("not an iso timestamp") == ""


# ── Auto-confirm for passive observations ───────────────────────


@pytest.mark.asyncio
async def test_passive_observation_auto_confirms_and_dispatches(monkeypatch) -> None:
    """anomaly.detected is a passive observation — there's no user choice
    to be made, just journal it. The auto-infer pipeline must auto-
    confirm + dispatch so the proactive scanner (which keys on
    'confirmed' status) actually has something to draw from. Without
    this fix, 34 auto_inferences sat in 'proposed' forever on TrueNAS."""
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    dispatch_calls: list[dict] = []

    async def _dispatch(action: dict) -> dict:
        dispatch_calls.append(action)
        return {"ok": True, "result": {"ok": True, "stored_id": 42}}

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer, "_dispatch_proposed_action", _dispatch)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.system",
        kind="anomaly.detected",
        summary="🌙 You're 60 min past your usual bedtime",
        payload={"anomaly_type": "late_bedtime"},
    )
    assert result["ok"] is True
    assert result["auto_confirmed"] is True
    # No keyboard once auto-confirmed (no decision needed)
    assert "keyboard" not in result
    # The action was dispatched + confirm() was called on the store
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["agent"] == "knowledge_notes"
    assert dispatch_calls[0]["capability"] == "record_event"
    assert store.confirm_calls and store.confirm_calls[0]["id"] == 77


@pytest.mark.asyncio
async def test_non_observational_kind_still_requires_user_confirm(monkeypatch) -> None:
    """User-choice inferences (e.g. 'did you go to bed at 02:30?') stay
    in 'proposed' state — only Telegram callback can confirm them."""
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _dispatch(_action: dict) -> dict:
        raise AssertionError("dispatch must NOT be called for non-observational kinds")

    async def _infer(_context: str) -> dict:
        return {
            "inference": "Saeed went to bed at 02:30",
            "confidence": 0.9,
            "proposed_action": {
                "agent": "knowledge_notes",
                "capability": "record_event",
                "payload": {
                    "agent": "personal_assistant",
                    "capability": "inferred_event",
                    "summary": "Saeed went to bed at 02:30",
                    "payload": {"source": "llm"},
                },
            },
        }

    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer, "_dispatch_proposed_action", _dispatch)
    monkeypatch.setattr(auto_infer.infer_tool, "infer", _infer)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.sleep",
        kind="sleep.bedtime_question",   # not in _AUTO_CONFIRM_SOURCE_KINDS
        summary="Looks like bedtime",
        payload={},
    )
    assert result["ok"] is True
    assert result["auto_confirmed"] is False
    # Keyboard is present so the user can confirm/reject
    assert "keyboard" in result
    assert store.confirm_calls == []


@pytest.mark.asyncio
async def test_auto_confirm_below_threshold_stays_proposed(monkeypatch) -> None:
    """Even a passive observational kind must clear the confidence
    floor before we auto-confirm — a low-confidence guess shouldn't
    sneak straight into the knowledge graph."""
    store = _FakeStore(count=0)

    async def _store() -> _FakeStore:
        return store

    async def _dispatch(_action: dict) -> dict:
        raise AssertionError("dispatch must NOT be called below threshold")

    # The rule-based path for anomaly.detected hard-codes its own
    # confidence. To exercise the threshold, lower the cap.
    monkeypatch.setattr(auto_infer, "_AUTO_CONFIRM_MIN_CONFIDENCE", 0.99)
    monkeypatch.setattr(auto_infer, "_auto_store", _store)
    monkeypatch.setattr(auto_infer, "_dispatch_proposed_action", _dispatch)

    result = await auto_infer.auto_infer_observer_event(
        agent="observer.system",
        kind="anomaly.detected",
        summary="Front door opened at 03:14",
        payload={"anomaly_type": "after_hours_door"},
    )
    assert result["ok"] is True
    assert result["auto_confirmed"] is False
    assert "keyboard" in result
    assert store.confirm_calls == []
