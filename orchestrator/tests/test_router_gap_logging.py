"""Regression tests for capability_gap logging in router.handle.

Covers the four failure pathways that should produce a gap row:
1. invalid_capability: classifier picked something not registered
2. chat_fallback_for_action_verb: classifier fell to chat for action text
3. dispatch_failed: registry.dispatch raised
4. no fallback available (escalator_no_tool_proposed)

Also pins NEGATIVE cases: greetings and successful tool routing must
NOT create gap rows (we don't want to flood the table with noise).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.gap_store import GapStore
from orchestrator.router import Router, _is_action_verb_request


def _make_npu_response(agent, capability, inputs=None, reason="ok"):
    content = json.dumps({
        "agent": agent,
        "capability": capability,
        "inputs": inputs or {},
        "needs_confirmation": False,
        "reason": reason,
    })
    return {"choices": [{"message": {"content": content}}]}


def _make_router_with_gap_store(
    *,
    npu_response,
    capability_lookup=None,
    dispatch_side_effect=None,
    dispatch_response=None,
    semantic_results=None,
):
    npu = MagicMock()
    npu.chat = AsyncMock(return_value=npu_response)

    registry = MagicMock()
    registry.agents = MagicMock(return_value=["home_automation", "personal_assistant"])
    if capability_lookup is None:
        registry.get_capability = MagicMock(return_value={"description": "ok"})
    else:
        registry.get_capability = MagicMock(side_effect=capability_lookup)
    registry.list_capabilities = MagicMock(return_value=[
        {"agent": "home_automation", "id": "lights_off", "description": "off lights"},
    ])
    if dispatch_side_effect is not None:
        registry.dispatch = AsyncMock(side_effect=dispatch_side_effect)
    else:
        registry.dispatch = AsyncMock(return_value=dispatch_response or {"ok": True})
    registry.semantic_search = AsyncMock(return_value=semantic_results or [])

    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("no humanizer in test"))

    gap_store = MagicMock(spec=GapStore)
    gap_store.record_gap = AsyncMock(return_value=99)

    router = Router(
        npu=npu,
        registry=registry,
        router_model="test-model",
        llm=llm,
        llm_fallback_model="qwen3:8b",
        humanizer_model="qwen3:8b",
        gap_store=gap_store,
    )
    return router, gap_store


def test_action_verb_detection_positive_cases() -> None:
    """The patterns must catch every realistic action-verb phrasing."""
    positives = [
        "reduce the temperature in the bedroom",
        "turn off the lights",
        "turn on the bedroom fan",
        "set the AC to 22",
        "raise the heat in the office",
        "open the bedroom blinds",
        "close the kitchen blinds",
        "lock the front door",
        "play music in the living room",
        "pause the music",
        "dim the lights to 30",
        "lower the bedroom thermostat",
        "switch on the kitchen lights",
        "toggle the office lights",
        "start the dishwasher",
        "schedule a reminder for 7am",
    ]
    for text in positives:
        assert _is_action_verb_request(text), f"missed: {text!r}"


def test_action_verb_detection_negative_cases() -> None:
    """Greetings, questions, and chit-chat must NOT trigger."""
    negatives = [
        "hi",
        "thanks",
        "good morning",
        "how are you",
        "what's the temperature in the bedroom",
        "is the dishwasher running",
        "are the lights on",
        "tell me a joke",
        "who is at the door",
        "any messages from John",
    ]
    for text in negatives:
        assert not _is_action_verb_request(text), f"false positive: {text!r}"


@pytest.mark.asyncio
async def test_invalid_capability_records_gap() -> None:
    """When classifier picks a capability that doesn't exist, gap fires."""
    npu_resp = _make_npu_response("home_automation", "nonexistent_tool", {})

    def cap_lookup(agent, capability):
        if (agent, capability) == ("home_automation", "nonexistent_tool"):
            return None
        return {"description": "ok"}  # chat catch-all exists

    router, gap_store = _make_router_with_gap_store(
        npu_response=npu_resp, capability_lookup=cap_lookup,
    )
    await router.handle("non-action question text", "user1")

    gap_store.record_gap.assert_awaited_once()
    call = gap_store.record_gap.await_args.kwargs
    assert call["failure_reason"] == "invalid_capability"
    assert call["router_pick"]["capability"] == "nonexistent_tool"


@pytest.mark.asyncio
async def test_action_verb_routed_to_chat_records_gap() -> None:
    """The headline regression: 'reduce the bedroom temperature' that
    falls through to chat MUST record a gap so the reflector can mine
    the pattern, even though chat returned something to the user."""
    # Classifier returns nulls -> falls through to semantic (empty) ->
    # then to chat catch-all.
    npu_resp = _make_npu_response(None, None, {})

    def cap_lookup(agent, capability):
        if (agent, capability) == ("personal_assistant", "chat"):
            return {"description": "chat"}
        return None  # nothing else registered

    router, gap_store = _make_router_with_gap_store(
        npu_response=npu_resp,
        capability_lookup=cap_lookup,
        dispatch_response={"reply": "ok", "already_natural": True},
    )
    await router.handle("reduce the bedroom temperature", "user1")

    gap_store.record_gap.assert_awaited_once()
    call = gap_store.record_gap.await_args.kwargs
    assert call["failure_reason"] == "chat_fallback_for_action_verb"
    assert "bedroom temperature" in call["user_text"]


@pytest.mark.asyncio
async def test_greeting_routed_to_chat_does_NOT_record_gap() -> None:
    """The conversational fast-path is fine — no gap, no noise."""
    npu_resp = _make_npu_response(None, None, {})
    router, gap_store = _make_router_with_gap_store(
        npu_response=npu_resp,
        dispatch_response={"reply": "hi", "already_natural": True},
    )
    # 'hi' triggers the fast-path conversational shortcut before
    # classification, so we use a slightly longer greeting that gets
    # to the classifier but is NOT an action verb.
    await router.handle("how is the weather today", "user1")

    gap_store.record_gap.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_failure_records_gap() -> None:
    """When registry.dispatch raises, gap row captures the exception."""
    npu_resp = _make_npu_response("home_automation", "lights_off", {"area": "bedroom"})
    router, gap_store = _make_router_with_gap_store(
        npu_response=npu_resp,
        dispatch_side_effect=RuntimeError("connection refused"),
    )
    result = await router.handle("turn off bedroom lights", "user1")

    assert "Error dispatching" in result["reply"]
    gap_store.record_gap.assert_awaited_once()
    call = gap_store.record_gap.await_args.kwargs
    assert call["failure_reason"] == "dispatch_failed"
    path = call["escalation_path"]
    assert any(step["stage"] == "dispatch" for step in path)


@pytest.mark.asyncio
async def test_successful_dispatch_does_NOT_record_gap() -> None:
    """The happy path must not pollute the gap log."""
    npu_resp = _make_npu_response("home_automation", "lights_off", {"area": "bedroom"})
    router, gap_store = _make_router_with_gap_store(
        npu_response=npu_resp,
        dispatch_response={"ok": True, "turned_off": []},
    )
    await router.handle("turn off bedroom lights", "user1")

    gap_store.record_gap.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_capability_available_records_gap() -> None:
    """When neither semantic search nor chat catch-all is available we
    return a generic decline — that still needs a gap so the system
    knows it's missing infrastructure."""
    npu_resp = _make_npu_response(None, None, {})
    router, gap_store = _make_router_with_gap_store(
        npu_response=npu_resp,
        capability_lookup=lambda *_: None,  # nothing registered
    )
    result = await router.handle("anything at all", "user1")

    assert "don't have a capability" in result["reply"]
    gap_store.record_gap.assert_awaited_once()


@pytest.mark.asyncio
async def test_gap_store_failure_does_NOT_break_user_reply() -> None:
    """Fail-open guarantee: if record_gap raises (DB outage), the user
    still gets their reply. Gap logging is best-effort telemetry."""
    npu_resp = _make_npu_response("home_automation", "lights_off", {"area": "bedroom"})
    router, gap_store = _make_router_with_gap_store(
        npu_response=npu_resp,
        dispatch_side_effect=RuntimeError("HA unreachable"),
    )
    # GapStore.record_gap is documented to fail-open, but let's defend
    # the router too in case a future GapStore variant raises.
    gap_store.record_gap.side_effect = Exception("postgres down")

    # The router currently doesn't catch — this test pins what we WANT.
    # If it fails, the next iteration should add the try/except.
    try:
        await router.handle("turn off bedroom lights", "user1")
    except Exception:
        pytest.fail(
            "Gap-store failure must not propagate to user — wrap "
            "record_gap calls in try/except in router.handle."
        )
