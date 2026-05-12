from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.router import Router


def _make_npu_response(agent, capability, inputs=None, needs_confirmation=False, reason="ok"):
    content = json.dumps({
        "agent": agent,
        "capability": capability,
        "inputs": inputs or {},
        "needs_confirmation": needs_confirmation,
        "reason": reason,
    })
    return {"choices": [{"message": {"content": content}}]}


def _make_router(npu_response, dispatch_response=None, semantic_results=None, llm=None):
    npu = MagicMock()
    npu.chat = AsyncMock(return_value=npu_response)

    registry = MagicMock()
    registry.agents = MagicMock(return_value=["home_automation"])
    # Treat any (agent, capability) lookup as registered for the existing
    # happy-path tests; specific tests below override this.
    registry.get_capability = MagicMock(return_value={"description": "test capability"})
    registry.list_capabilities = MagicMock(
        return_value=[
            {"agent": "home_automation", "id": "list_entities", "description": "list HA entities"},
            {"agent": "home_automation", "id": "get_entity_state", "description": "get state"},
            {"agent": "home_automation", "id": "call_service", "description": "call HA service"},
            {"agent": "home_automation", "id": "set_scene", "description": "activate scene"},
            {
                "agent": "home_automation",
                "id": "doorbell.last_visitor",
                "description": "last doorbell visitor",
            },
        ]
    )
    registry.dispatch = AsyncMock(return_value=dispatch_response or {"ok": True})
    registry.semantic_search = AsyncMock(return_value=semantic_results or [])

    if llm is None:
        # Pass a no-op LLM so the humanizer falls back to JSON dumping.
        # This keeps existing tests deterministic without needing to
        # template a humanized response.
        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("no humanizer in this test"))

    router = Router(
        npu=npu,
        registry=registry,
        router_model="test-model",
        llm=llm,
        llm_fallback_model="qwen3:8b",
        humanizer_model="qwen3:8b",
    )
    return router


@pytest.mark.asyncio
async def test_turn_off_lights():
    npu_resp = _make_npu_response(
        "home_automation",
        "call_service",
        {"domain": "light", "service": "turn_off", "data": {"entity_id": "light.living_room"}},
    )
    router = _make_router(npu_resp, dispatch_response={"ok": True, "result": []})
    result = await router.handle("turn off the living room lights", "user1")
    assert "reply" in result


@pytest.mark.asyncio
async def test_get_temperature():
    npu_resp = _make_npu_response(
        "home_automation", "get_entity_state", {"entity_id": "sensor.bedroom_temperature"}
    )
    router = _make_router(npu_resp, dispatch_response={"ok": True, "result": {"state": "22.5"}})
    result = await router.handle("what's the temperature in the bedroom", "user1")
    assert "reply" in result


@pytest.mark.asyncio
async def test_list_lights():
    npu_resp = _make_npu_response("home_automation", "list_entities", {"domain": "light"})
    router = _make_router(npu_resp, dispatch_response={"ok": True, "result": []})
    result = await router.handle("list all my lights", "user1")
    assert "reply" in result


@pytest.mark.asyncio
async def test_set_scene():
    npu_resp = _make_npu_response("home_automation", "set_scene", {"scene_name": "evening"})
    router = _make_router(npu_resp, dispatch_response={"ok": True, "scene": "scene.evening"})
    result = await router.handle("set the evening scene", "user1")
    assert "reply" in result


@pytest.mark.asyncio
async def test_last_visitor():
    npu_resp = _make_npu_response("home_automation", "doorbell.last_visitor", {"hours": 8})
    dispatch = {"ok": True, "result": {"hours": 8, "summary": "No events", "events": []}}
    router = _make_router(npu_resp, dispatch_response=dispatch)
    result = await router.handle("who was at the door today", "user1")
    assert "reply" in result


@pytest.mark.asyncio
async def test_fallback_low_score():
    """agent=null from LLM + semantic search returns low score → router routes
    to the personal_assistant.chat fallback (smalltalk). The assistant's
    natural reply is passed through unchanged."""
    null_classification = {
        "agent": None,
        "capability": None,
        "inputs": {},
        "needs_confirmation": False,
        "reason": "unknown",
    }
    npu_resp = {"choices": [{"message": {"content": json.dumps(null_classification)}}]}
    semantic = [
        {"score": 0.3, "payload": {"agent": "home_automation", "capability": "list_entities"}}
    ]
    router = _make_router(
        npu_resp,
        semantic_results=semantic,
        dispatch_response={
            "ok": True,
            "result": {
                "reply": "I'm not sure what you mean — can you rephrase?",
                "already_natural": True,
            },
        },
    )
    result = await router.handle("play jazz music", "user1")
    assert result == {"reply": "I'm not sure what you mean — can you rephrase?"}
    args, _ = router._registry.dispatch.call_args  # noqa: SLF001
    assert args == ("personal_assistant", "chat", {"text": "play jazz music"})


@pytest.mark.asyncio
async def test_semantic_fallback_good_score():
    """agent=null from LLM + good semantic hit → routes via semantic search."""
    null_classification = {
        "agent": None,
        "capability": None,
        "inputs": {},
        "needs_confirmation": False,
        "reason": "unknown",
    }
    npu_resp = {"choices": [{"message": {"content": json.dumps(null_classification)}}]}
    semantic = [
        {"score": 0.85, "payload": {"agent": "home_automation", "capability": "list_entities"}}
    ]
    router = _make_router(
        npu_resp, semantic_results=semantic, dispatch_response={"ok": True, "result": []}
    )
    result = await router.handle("show me all entities", "user1")
    assert "reply" in result


@pytest.mark.asyncio
async def test_router_falls_back_to_ollama_when_npu_unavailable():
    """When the NPU client raises NPUUnavailable, the router should retry via
    the OllamaClient with the configured fallback model and use that
    classification."""
    from home_agents_sdk.npu import NPUUnavailable

    from orchestrator.router import Router

    npu = MagicMock()
    npu.chat = AsyncMock(side_effect=NPUUnavailable("lemonade is a stub"))

    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value={
            "message": {
                "content": json.dumps(
                    {
                        "agent": "home_automation",
                        "capability": "list_entities",
                        "inputs": {"domain": "light"},
                        "needs_confirmation": False,
                        "reason": "list lights",
                    }
                )
            }
        }
    )

    registry = MagicMock()
    registry.agents = MagicMock(return_value=["home_automation"])
    registry.list_capabilities = MagicMock(
        return_value=[
            {"agent": "home_automation", "id": "list_entities", "description": "list entities"}
        ]
    )
    registry.get_capability = MagicMock(return_value={"description": "list entities"})
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": ["light.x"]})
    registry.semantic_search = AsyncMock(return_value=[])

    router = Router(
        npu=npu,
        registry=registry,
        router_model="qwen3-1.7b-int4",
        llm=llm,
        llm_fallback_model="qwen3:8b",
    )
    result = await router.handle("list my lights", "user1")
    assert "reply" in result
    npu.chat.assert_awaited_once()
    # llm.chat is now called at least twice: once for the Ollama-fallback
    # classification, and once again by the humanizer for the dispatch result.
    assert llm.chat.await_count >= 2
    # The first call (classification) should have used the fallback model.
    first_call = llm.chat.call_args_list[0]
    assert first_call.kwargs.get("model") == "qwen3:8b"


@pytest.mark.asyncio
async def test_router_returns_no_capability_when_both_backends_fail():
    """If NPU AND Ollama both fail to classify, semantic search misses, and
    we get the friendly fallback reply."""
    from home_agents_sdk.npu import NPUUnavailable

    from orchestrator.router import Router

    npu = MagicMock()
    npu.chat = AsyncMock(side_effect=NPUUnavailable("npu down"))

    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("ollama down"))

    registry = MagicMock()
    registry.agents = MagicMock(return_value=["home_automation"])
    registry.list_capabilities = MagicMock(return_value=[])
    registry.get_capability = MagicMock(return_value=None)
    registry.dispatch = AsyncMock()
    registry.semantic_search = AsyncMock(return_value=[])

    router = Router(
        npu=npu,
        registry=registry,
        router_model="qwen3-1.7b-int4",
        llm=llm,
        llm_fallback_model="qwen3:8b",
    )
    result = await router.handle("list my lights", "user1")
    assert result == {"reply": "I don't have a capability for that yet."}


@pytest.mark.asyncio
async def test_router_drops_invented_capability_and_falls_back_to_semantic():
    """If the LLM picks a capability id that doesn't exist in the registry,
    the router must NOT dispatch that 404; it should drop it and fall through
    to semantic search."""
    npu_resp = _make_npu_response(
        "home_automation",
        "list_lights",  # invented; the real id is `list_entities`
        {"domain": "light"},
    )
    semantic = [
        {"score": 0.92, "payload": {"agent": "home_automation", "capability": "list_entities"}}
    ]
    router = _make_router(
        npu_resp,
        semantic_results=semantic,
        dispatch_response={"ok": True, "result": ["light.x"]},
    )
    real = {"description": "list HA entities"}
    router._registry.get_capability = MagicMock(  # noqa: SLF001
        side_effect=lambda agent, cap: real if cap == "list_entities" else None
    )

    result = await router.handle("list my lights", "user1")
    assert "reply" in result
    args, _ = router._registry.dispatch.call_args  # noqa: SLF001
    # Inputs from the original classification carry over to the semantic match.
    assert args == ("home_automation", "list_entities", {"domain": "light"})


@pytest.mark.asyncio
async def test_router_classify_prompt_includes_capability_inventory():
    """Capability descriptions must be included in the LLM prompt so the
    router can pick valid ids instead of hallucinating."""
    npu_resp = _make_npu_response("home_automation", "list_entities", {"domain": "light"})
    router = _make_router(npu_resp, dispatch_response={"ok": True})

    await router.handle("list lights", "user1")
    messages = router._npu.chat.call_args.kwargs["messages"]  # noqa: SLF001
    user_content = next(m["content"] for m in messages if m["role"] == "user")
    assert "home_automation.list_entities" in user_content
    assert "EXACT" in messages[0]["content"]


@pytest.mark.asyncio
async def test_humanizer_calls_llm_with_user_text_and_tool_payload():
    """The humanizer should call the configured LLM with the user's text and
    the tool's payload, then return the LLM's natural-language reply."""
    humanizer_llm = MagicMock()
    humanizer_llm.chat = AsyncMock(
        return_value={"message": {"content": "Living Room: 1 light on, 2 off."}}
    )

    npu_resp = _make_npu_response("home_automation", "list_entities", {"domain": "light"})
    router = _make_router(
        npu_resp,
        dispatch_response={
            "ok": True,
            "result": {"by_area": {"Living Room": [{"name": "Lamp", "state": "on"}]}},
        },
        llm=humanizer_llm,
    )
    result = await router.handle("list lights", "user1")
    assert result == {"reply": "Living Room: 1 light on, 2 off."}
    humanizer_llm.chat.assert_awaited_once()
    _, kwargs = humanizer_llm.chat.call_args
    assert kwargs.get("model") == "qwen3:8b"


@pytest.mark.asyncio
async def test_execute_pending_dispatches_and_applies_humanizer():
    humanizer_llm = MagicMock()
    humanizer_llm.chat = AsyncMock(return_value={"message": {"content": "Logged. Anything else?"}})
    npu_resp = _make_npu_response("household_ops", "chores_complete", {"chore_id": 7})
    router = _make_router(
        npu_resp,
        dispatch_response={"ok": True, "result": {"ok": True}},
        llm=humanizer_llm,
    )
    pending = {
        "agent": "household_ops",
        "capability": "chores_complete",
        "inputs": {"chore_id": 7},
        "reason": "Mark sheets washed.",
        "prompt_text": "I'll log that bed sheets were washed.",
    }

    result = await router.execute_pending(pending)

    assert result == {"reply": "Logged. Anything else?"}
    router._registry.dispatch.assert_awaited_once_with(  # noqa: SLF001
        "household_ops", "chores_complete", {"chore_id": 7}
    )
    humanizer_llm.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_humanizer_passes_through_already_natural_replies():
    """Capabilities like personal_assistant.chat that return
    `{"reply": "...", "already_natural": True}` should bypass humanization."""
    npu_resp = _make_npu_response(
        "personal_assistant", "chat", {"text": "hi"}
    )
    router = _make_router(
        npu_resp,
        dispatch_response={
            "ok": True,
            "result": {"reply": "Hey! How can I help?", "already_natural": True},
        },
    )
    result = await router.handle("hi", "user1")
    assert result == {"reply": "Hey! How can I help?"}


@pytest.mark.asyncio
async def test_humanizer_falls_back_to_json_when_llm_down():
    """If the humanizer LLM raises, return a stringified JSON of the payload
    rather than crashing or returning a user-hostile error."""
    npu_resp = _make_npu_response("home_automation", "list_entities", {"domain": "light"})
    router = _make_router(
        npu_resp,
        dispatch_response={"ok": True, "result": {"total_visible": 3}},
    )
    result = await router.handle("list lights", "user1")
    assert "reply" in result
    assert "total_visible" in result["reply"]


@pytest.mark.asyncio
async def test_no_match_falls_back_to_personal_assistant_chat():
    """When neither classification nor semantic search finds a tool, the
    router routes to personal_assistant.chat (if registered) instead of
    the 'I don't have a capability' reply."""
    null_classification = {
        "agent": None,
        "capability": None,
        "inputs": {},
        "needs_confirmation": False,
        "reason": "smalltalk",
    }
    npu_resp = {"choices": [{"message": {"content": json.dumps(null_classification)}}]}
    router = _make_router(npu_resp)
    router._registry.get_capability = MagicMock(  # noqa: SLF001
        side_effect=lambda agent, cap: (
            {"description": "chat"} if (agent, cap) == ("personal_assistant", "chat") else None
        )
    )
    router._registry.semantic_search = AsyncMock(return_value=[])  # noqa: SLF001
    router._registry.dispatch = AsyncMock(  # noqa: SLF001
        return_value={"ok": True, "result": {"reply": "Hi!", "already_natural": True}}
    )

    result = await router.handle("hi there", "user1")
    assert result == {"reply": "Hi!"}
    args, _ = router._registry.dispatch.call_args  # noqa: SLF001
    assert args == ("personal_assistant", "chat", {"text": "hi there"})


@pytest.mark.asyncio
async def test_no_match_and_no_chat_capability_returns_friendly_message():
    """If neither a tool match nor a chat fallback is registered, the user
    gets the original 'I don't have a capability' reply (back-compat)."""
    null_classification = {
        "agent": None,
        "capability": None,
        "inputs": {},
        "needs_confirmation": False,
        "reason": "unknown",
    }
    npu_resp = {"choices": [{"message": {"content": json.dumps(null_classification)}}]}
    router = _make_router(npu_resp)
    router._registry.get_capability = MagicMock(return_value=None)  # noqa: SLF001
    router._registry.semantic_search = AsyncMock(return_value=[])  # noqa: SLF001
    result = await router.handle("play jazz", "user1")
    assert result == {"reply": "I don't have a capability for that yet."}


@pytest.mark.asyncio
async def test_chat_inputs_always_filled_with_user_text():
    """Even if the LLM classifier picks chat but omits the text input, the
    router must always pass the user's text to chat()."""
    bad_classification = {
        "agent": "personal_assistant",
        "capability": "chat",
        "inputs": {},  # LLM forgot the text
        "needs_confirmation": False,
        "reason": "smalltalk",
    }
    npu_resp = {"choices": [{"message": {"content": json.dumps(bad_classification)}}]}
    router = _make_router(
        npu_resp,
        dispatch_response={
            "ok": True,
            "result": {"reply": "Hi!", "already_natural": True},
        },
    )
    router._registry.get_capability = MagicMock(  # noqa: SLF001
        side_effect=lambda agent, cap: (
            {"description": "chat"} if (agent, cap) == ("personal_assistant", "chat") else None
        )
    )

    result = await router.handle("hi there", "user1")
    assert result == {"reply": "Hi!"}
    args, _ = router._registry.dispatch.call_args  # noqa: SLF001
    assert args == ("personal_assistant", "chat", {"text": "hi there"})
