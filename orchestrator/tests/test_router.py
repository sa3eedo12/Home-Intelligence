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


def _make_router(npu_response, dispatch_response=None, semantic_results=None):
    npu = MagicMock()
    npu.chat = AsyncMock(return_value=npu_response)

    registry = MagicMock()
    registry.agents = MagicMock(return_value=["home_automation"])
    registry.get_capability = MagicMock(return_value=None)
    registry.dispatch = AsyncMock(return_value=dispatch_response or {"ok": True})
    registry.semantic_search = AsyncMock(return_value=semantic_results or [])

    router = Router(npu=npu, registry=registry, router_model="test-model")
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
    """agent=null from LLM + semantic search returns low score → graceful reply."""
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
    router = _make_router(npu_resp, semantic_results=semantic)
    result = await router.handle("play jazz music", "user1")
    assert result == {"reply": "I don't have a capability for that yet."}


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
    registry.get_capability = MagicMock(return_value=None)
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": ["light.x"]})

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
    llm.chat.assert_awaited_once()
    # Ollama should have been called with the fallback model name.
    _, kwargs = llm.chat.call_args
    assert kwargs.get("model") == "qwen3:8b"


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
