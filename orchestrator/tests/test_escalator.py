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
async def test_resolves_only_after_tool_call_succeeds():
    """Anti-fabrication: the escalator MUST have called at least one
    tool before resolving. A zero-tool resolution would mean the 8B
    invented an answer — same fabrication class we fixed in chat.py."""
    llm = _llm_returning(
        # Step 1: call status tool
        '{"action": "tool_call", "agent": "ha", "capability": "climate_status",'
        ' "inputs": {"area": "bedroom"}}',
        # Step 2: use observation to compose reply
        '{"action": "resolved", "reply": "Bedroom is 24°C currently."}'
    )
    registry = _registry(
        caps=[{"agent": "ha", "id": "climate_status", "description": "thermostat status"}],
        dispatch_results={
            ("ha", "climate_status"): {"thermostats": [{"area": "Bedroom", "current": 24}]},
        },
    )
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("what is the bedroom temperature?")

    assert resolution is not None
    assert resolution["reply"] == "Bedroom is 24°C currently."
    assert len(resolution["tools_used"]) == 1


@pytest.mark.asyncio
async def test_zero_tool_resolution_treated_as_giveup():
    """If the LLM jumps straight to 'resolved' without any tool call,
    treat it as fabrication and give up cleanly."""
    llm = _llm_returning(
        '{"action": "resolved", "reply": "The thermostat is set to 22°C."}',
    )
    registry = _registry(
        caps=[{"agent": "ha", "id": "climate_status", "description": "status"}],
    )
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("what is the bedroom temperature?")

    # Must NOT trust the unsubstantiated claim
    assert resolution is None
    assert path[-1]["outcome"] == "no_tool_used_suspect_fabrication"
    assert "set to 22" in path[-1]["reply_preview"]


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
    """The model sometimes returns prose. We nudge once, then it
    eventually calls a tool + resolves."""
    llm = _llm_returning(
        "I think we should look at the lights first",  # not JSON
        '{"action": "tool_call", "agent": "ha", "capability": "lights_off",'
        ' "inputs": {}}',
        '{"action": "resolved", "reply": "All lights off."}',
    )
    registry = _registry(
        caps=[{"agent": "ha", "id": "lights_off", "description": "off"}],
        dispatch_results={("ha", "lights_off"): {"ok": True}},
    )
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


@pytest.mark.asyncio
async def test_strips_agent_prefix_from_capability():
    """The 8b sometimes returns capability='home_automation.climate_status'
    instead of just 'climate_status'. Strip the prefix defensively so
    we don't waste iterations bouncing through invalid_capability."""
    llm = _llm_returning(
        # Buggy call with doubled prefix
        '{"action": "tool_call", "agent": "home_automation", '
        '"capability": "home_automation.climate_status", "inputs": {}}',
        '{"action": "resolved", "reply": "Bedroom is 24°C."}',
    )
    registry = _registry(
        caps=[{"agent": "home_automation", "id": "climate_status",
               "description": "thermostat status"}],
        dispatch_results={
            ("home_automation", "climate_status"): {"thermostats": [
                {"area": "Bedroom", "current": 24, "target": 23.5}
            ]},
        },
    )
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b")

    resolution, path = await esc.resolve("what's the bedroom temperature?")

    assert resolution is not None
    # First step should be a successful tool_call (NOT invalid_capability)
    tool_steps = [p for p in path if p.get("stage") == "tool_call"]
    assert len(tool_steps) == 1
    assert tool_steps[0]["outcome"] == "ok"
    assert tool_steps[0]["capability"] == "climate_status"


@pytest.mark.asyncio
async def test_exhausted_with_harvested_entities_emits_synthetic_giveup():
    """The headline EV test: 8b kept calling search_entities and
    get_entity_state, found han_battery_level, but never produced
    'resolved' or 'give_up'. Exhausted at max_iterations. The
    harvested entities MUST surface so the router can file an inline
    proposal."""
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={
        "message": {"content":
            '{"action": "tool_call", "agent": "ha", "capability": "search_entities", "inputs": {"query": "battery"}}'
        }
    })
    registry = _registry(
        caps=[{"agent": "ha", "id": "search_entities", "description": "search"}],
        dispatch_results={
            ("ha", "search_entities"): {
                "query": "battery",
                "total_matched": 1,
                "hits": [
                    {"entity_id": "sensor.han_battery_level", "name": "HAN Battery level",
                     "state": "68", "area": "Garage", "domain": "sensor"}
                ],
            },
        },
    )
    esc = Escalator(llm=llm, registry=registry, model="qwen3:8b", max_iterations=3)

    resolution, path = await esc.resolve("what is the battery percentage of my car?")

    assert resolution is None
    # An "exhausted" record AND a synthetic "give_up" with the harvested entity
    exhausted = [p for p in path if p.get("stage") == "exhausted"]
    assert len(exhausted) == 1
    assert "sensor.han_battery_level" in exhausted[0]["discovered_entities"]
    give_ups = [p for p in path if p.get("stage") == "give_up"]
    assert len(give_ups) == 1, "synthetic give_up should be emitted for inline-proposal layer"
    assert "sensor.han_battery_level" in give_ups[0]["discovered_entities"]
    assert "Exhausted iterations" in give_ups[0]["reason"]


def test_extract_entity_ids_walks_nested_dict():
    """The harvest helper must catch entity_ids in any nested dict,
    list, or string field — tool results aren't normalized."""
    from orchestrator.escalator import _extract_entity_ids
    sample = {
        "result": {
            "hits": [
                {"entity_id": "sensor.han_battery_level", "state": "68"},
                {"entity_id": "binary_sensor.han_charging", "state": "off"},
            ],
            "by_area": {
                "Garage": [{"entity_id": "climate.han_climate"}],
            },
        },
        "summary": "Found 3 entities in Garage: climate.han_climate, sensor.han_range",
    }
    out = _extract_entity_ids(sample)
    # Direct entity_id fields + regex-matched in summary string
    assert "sensor.han_battery_level" in out
    assert "binary_sensor.han_charging" in out
    assert "climate.han_climate" in out
    assert "sensor.han_range" in out  # picked up from the summary string


def test_extract_entity_ids_rejects_unknown_domains():
    """Don't pollute proposal evidence with random dotted strings."""
    from orchestrator.escalator import _extract_entity_ids
    sample = {"message": "some.random.path and user.id but sensor.real_one is real"}
    out = _extract_entity_ids(sample)
    assert "sensor.real_one" in out
    # "some.random" has no _HA_DOMAIN prefix
    assert "some.random" not in out
    # "user.id" doesn't either
    assert "user.id" not in out


def test_filter_relevant_entities_drops_unrelated():
    """The live EV test harvested 39 entities including HA update
    sensors and iPhone batteries. Filter must drop entities whose
    entity_id contains NONE of the user's tokens."""
    from orchestrator.escalator import _filter_relevant_entities
    harvested = [
        "update.button_card_update",
        "update.ultra_card_update",
        "sensor.saeeds_deebot_unit_care_lifespan",
        "sensor.raspberry_pi_3_battery_level",
        "sensor.saeeds_iphone_battery_level",
        "sensor.han_battery_level",
        "sensor.han_range",
        "binary_sensor.han_charging",
    ]
    out = _filter_relevant_entities(
        harvested, "What is the battery percentage of my car?"
    )
    # Update entities (no token match) dropped
    assert "update.button_card_update" not in out
    # Battery entities kept (token=battery matches)
    assert "sensor.han_battery_level" in out
    assert "sensor.raspberry_pi_3_battery_level" in out  # imperfect but acceptable
    # 'car' / 'battery' both stopwords-free; deebot doesn't match either
    assert "sensor.saeeds_deebot_unit_care_lifespan" not in out


def test_filter_relevant_entities_falls_back_when_no_match():
    """If filtering drops everything, keep the original list rather
    than produce a proposal with zero evidence."""
    from orchestrator.escalator import _filter_relevant_entities
    out = _filter_relevant_entities(
        ["sensor.han_battery_level"], "tell me a joke"
    )
    # 'joke' doesn't appear in any harvested entity but we keep the
    # entity so the proposal still has SOMETHING to cite
    assert out == ["sensor.han_battery_level"]


def test_filter_relevant_entities_strips_stopwords():
    """Stopwords like 'the', 'is', 'percentage' shouldn't be used as
    match tokens — they'd match too much."""
    from orchestrator.escalator import _filter_relevant_entities
    out = _filter_relevant_entities(
        ["sensor.something_irrelevant"], "what is the percentage"
    )
    # All input words are stopwords; the filter falls back to original
    # rather than dropping the only entity
    assert out == ["sensor.something_irrelevant"]
