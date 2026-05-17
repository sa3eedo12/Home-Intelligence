"""Escalator tests.

These pin the ReAct loop's contract: bounded iteration, honest give-up,
strict tool-name validation, and the resolve→reply happy path.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.escalator import (
    Escalator,
    _format_catalog,
    _parse_step,
    map_exhausted_outcome_to_failure_reason,
)


def _llm_returning(*contents: str) -> MagicMock:
    """Build a mock LLM that returns the given contents in order."""
    responses = [{"message": {"content": c}} for c in contents]
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=responses)
    return llm


def _registry(caps: list[dict], dispatch_results: dict | None = None,
              dispatch_errors: dict | None = None):
    dispatch_results = dispatch_results or {}
    dispatch_errors = dispatch_errors or {}
    registry = MagicMock()
    registry.list_capabilities = MagicMock(return_value=caps)
    cap_lookup = {(c["agent"], c["id"]): c for c in caps}
    registry.get_capability = MagicMock(
        side_effect=lambda a, c: cap_lookup.get((a, c))
    )

    async def _dispatch(agent, capability, inputs):
        key = (agent, capability)
        if key in dispatch_errors:
            raise dispatch_errors[key]
        return dispatch_results.get(key, {"ok": True})

    registry.dispatch = AsyncMock(side_effect=_dispatch)
    return registry


def test_parse_step_handles_clean_json():
    out = _parse_step('{"action": "resolved", "reply": "ok"}')
    assert out == {"action": "resolved", "reply": "ok"}


def test_parse_step_handles_codefence_json():
    out = _parse_step('```json\n{"action": "give_up", "reason": "nope"}\n```')
    assert out == {"action": "give_up", "reason": "nope"}


def test_parse_step_handles_prose_with_json():
    """The 8b sometimes ignores 'no prose' instructions."""
    out = _parse_step(
        'Sure, here is my next step: {"action": "tool_call", '
        '"agent": "home_automation", "capability": "lights_status", "inputs": {}}'
    )
    assert out["action"] == "tool_call"
    assert out["capability"] == "lights_status"


def test_parse_step_returns_none_on_garbage():
    assert _parse_step("complete garbage no json") is None
    assert _parse_step("") is None


def test_format_catalog_marks_side_effecting():
    caps = [
        {"agent": "x", "id": "read", "description": "read", "side_effects": False},
        {"agent": "x", "id": "write", "description": "write", "side_effects": True},
    ]
    formatted = _format_catalog(caps)
    assert "x.read:" in formatted
    assert "x.write:" in formatted
    # Side-effecting capability has the * marker
    write_line = [l for l in formatted.split("\n") if "x.write" in l][0]
    assert "*" in write_line
    read_line = [l for l in formatted.split("\n") if "x.read" in l][0]
    assert read_line.lstrip().startswith("x.read") or read_line.startswith("    x.read")


@pytest.mark.asyncio
async def test_resolves_on_first_step():
    """The 8B immediately decides it can answer from prior context."""
    llm = _llm_returning(
        '{"action": "resolved", "reply": "Bedroom is 24°C currently."}'
    )
    registry = _registry([
        {"agent": "ha", "id": "climate_status", "description": "thermostat status"},
    ])
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("what is the bedroom temperature?")

    assert resolution is not None
    assert resolution["reply"] == "Bedroom is 24°C currently."
    assert path[-1]["stage"] == "resolved"
    assert path[-1]["outcome"] == "ok"


@pytest.mark.asyncio
async def test_react_loop_calls_tool_then_resolves():
    """Two-step: tool_call → resolve. The headline use case."""
    llm = _llm_returning(
        # Step 1: ask for status
        '{"action": "tool_call", "agent": "ha", "capability": "climate_status",'
        ' "inputs": {"area": "bedroom"}}',
        # Step 2: use observation to compose reply
        '{"action": "resolved", "reply": "Set the bedroom thermostat to 22°C."}',
    )
    registry = _registry(
        caps=[
            {"agent": "ha", "id": "climate_status", "description": "thermostat status"},
        ],
        dispatch_results={
            ("ha", "climate_status"): {
                "ok": True,
                "thermostats": [{"entity_id": "climate.thermostat_2", "current": 24, "target": 23.5}],
            },
        },
    )
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("set the bedroom thermostat to 22")

    assert resolution is not None
    assert resolution["reply"].startswith("Set the bedroom")
    assert len(resolution["tools_used"]) == 1
    assert resolution["tools_used"][0]["capability"] == "climate_status"
    # path: tool_call + resolved
    assert path[0]["stage"] == "tool_call"
    assert path[0]["outcome"] == "ok"
    assert path[-1]["stage"] == "resolved"


@pytest.mark.asyncio
async def test_invalid_capability_gets_corrected():
    """When 8B picks a non-existent capability, escalator tells it so
    and it tries again. After 2 misses we expect give_up — fewer chances
    than max_iterations because we want to fail loudly on bad behavior."""
    llm = _llm_returning(
        '{"action": "tool_call", "agent": "ha", "capability": "nonexistent_thing", "inputs": {}}',
        '{"action": "tool_call", "agent": "ha", "capability": "also_nonexistent", "inputs": {}}',
        '{"action": "give_up", "reason": "no relevant tool"}',
    )
    registry = _registry(caps=[
        {"agent": "ha", "id": "climate_status", "description": "thermostat status"},
    ])
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b", max_iterations=4)

    resolution, path = await esc.resolve("do something arcane")

    assert resolution is None
    # 2 invalid attempts + 1 give_up = at least 3 path entries
    assert any(p["outcome"] == "invalid_capability" for p in path)
    assert path[-1]["stage"] == "give_up"


@pytest.mark.asyncio
async def test_tool_exception_recorded_and_loop_continues():
    """A failing tool call doesn't crash the loop — the LLM gets to see
    the error in the observation and choose a different path."""
    llm = _llm_returning(
        '{"action": "tool_call", "agent": "ha", "capability": "lights_off",'
        ' "inputs": {"area": "bedroom"}}',
        '{"action": "give_up", "reason": "tool errored, no alternative"}',
    )
    registry = _registry(
        caps=[{"agent": "ha", "id": "lights_off", "description": "off"}],
        dispatch_errors={("ha", "lights_off"): RuntimeError("HA unreachable")},
    )
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("turn off bedroom lights")

    assert resolution is None
    # tool_call entry has outcome=exception
    tool_step = [p for p in path if p.get("stage") == "tool_call"][0]
    assert tool_step["outcome"] == "exception"
    # last step is give_up
    assert path[-1]["stage"] == "give_up"


@pytest.mark.asyncio
async def test_max_iterations_terminates_loop():
    """Pathological case: model keeps calling tools and never resolves.
    Loop must terminate at max_iterations without raising."""
    # Always proposes the same tool call (which always returns ok but
    # never resolves)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={
        "message": {
            "content": '{"action": "tool_call", "agent": "ha", '
                       '"capability": "lights_status", "inputs": {}}'
        }
    })
    registry = _registry(
        caps=[{"agent": "ha", "id": "lights_status", "description": "status"}],
        dispatch_results={("ha", "lights_status"): {"on": 3, "off": 7}},
    )
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b", max_iterations=3)

    resolution, path = await esc.resolve("loop forever please")

    assert resolution is None
    assert path[-1]["stage"] == "exhausted"
    assert path[-1]["outcome"] == "max_iterations"
    # Verify the iteration cap was actually honored
    assert llm.chat.await_count == 3


@pytest.mark.asyncio
async def test_llm_exception_terminates_cleanly():
    """A LLM exception (Ollama down, timeout) must not propagate."""
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=Exception("ollama exploded"))
    registry = _registry(caps=[])
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("anything")

    assert resolution is None
    assert path[-1]["outcome"] == "exception"


@pytest.mark.asyncio
async def test_bad_json_response_nudges_once_then_continues():
    """The model sometimes returns prose. We nudge once, then if it
    still produces garbage we let max_iterations terminate."""
    llm = _llm_returning(
        "I think we should look at the lights first",  # not JSON
        '{"action": "resolved", "reply": "All lights off."}',
    )
    registry = _registry(caps=[{"agent": "ha", "id": "lights_off",
                                "description": "off"}])
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("turn off lights")

    assert resolution is not None
    assert resolution["reply"] == "All lights off."
    # First step recorded as bad_json
    assert path[0]["outcome"] == "bad_json"


def test_map_exhausted_outcome_to_failure_reason():
    assert map_exhausted_outcome_to_failure_reason(
        [{"stage": "exhausted", "outcome": "all_errored"}]
    ) == "escalator_all_tools_errored"
    assert map_exhausted_outcome_to_failure_reason(
        [{"stage": "exhausted", "outcome": "no_tool_proposed"}]
    ) == "escalator_no_tool_proposed"
    assert map_exhausted_outcome_to_failure_reason(
        [{"stage": "exhausted", "outcome": "max_iterations"}]
    ) == "escalator_max_iterations"
    assert map_exhausted_outcome_to_failure_reason(
        [{"stage": "give_up", "reason": "x"}]
    ) == "escalator_no_tool_proposed"
    assert map_exhausted_outcome_to_failure_reason([]) == "escalator_no_tool_proposed"
