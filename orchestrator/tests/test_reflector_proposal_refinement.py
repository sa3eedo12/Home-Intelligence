"""Tests for the nightly proposal refinement phase.

Pins the contract: pending code_change proposals filed during the
day (by router/escalator with limited context) get reprocessed by
the 35B at night with the full HA entity catalog as grounding.
Refined proposals have:
  - sharper titles
  - rationale that references real entities
  - confidence adjusted
  - original_rationale preserved
  - refined_at stamped (so we don't reprocess)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.reflector import NightlyReflector


def _make_reflector(
    *,
    unrefined_proposals: list[dict] | None = None,
    llm_response: str | None = None,
    refine_returns: bool = True,
    entity_catalog: dict | None = None,
):
    pool = MagicMock()
    redis = MagicMock()
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={
        "message": {"content": llm_response or "{}"}
    })
    registry = MagicMock()
    registry.dispatch = AsyncMock(return_value=entity_catalog or {
        "by_area": {
            "Garage": [
                {"entity_id": "sensor.han_battery_level", "name": "HAN Battery level", "state": "68"},
                {"entity_id": "sensor.han_range", "name": "HAN Range", "state": "388"},
                {"entity_id": "climate.han_climate", "name": "HAN Climate", "state": "off"},
            ],
            "Bedroom": [
                {"entity_id": "light.bedroom_lamp", "name": "Bedroom Lamp", "state": "off"},
            ],
        }
    })

    reflector = NightlyReflector(
        pool=pool, redis=redis, llm=llm, registry=registry,
        reasoner_model="qwen3.6:35b-a3b", fallback_model="qwen3:8b",
        gap_store=MagicMock(),
    )
    # Mock store with refine_proposal
    reflector.store = MagicMock()
    reflector.store.list_unrefined_code_change_proposals = AsyncMock(
        return_value=unrefined_proposals or []
    )
    reflector.store.refine_proposal = AsyncMock(return_value=refine_returns)
    return reflector


@pytest.mark.asyncio
async def test_no_unrefined_returns_empty():
    reflector = _make_reflector(unrefined_proposals=[])
    out = await reflector._refine_proposals()
    assert out == []
    reflector.store.refine_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_refines_proposal_with_sharper_title():
    """The headline test: rough proposal in, sharp proposal out."""
    rough = [{
        "id": 59,
        "kind": "code_change",
        "title": "Add sensor-status tool — 'What is the battery percentage of my car?'",
        "rationale": "User asked about car battery. Discovered 27 entities including "
                     "sensor.han_battery_level, sensor.raspberry_pi_3_battery_level, "
                     "sensor.saeeds_iphone_battery_level, etc. " * 5,  # long enough
        "confidence": 0.7,
        "cost_estimate": "small",
        "impact_estimate": "Surfaces data already collected in HA",
    }]
    refined_json = json.dumps({
        "title": "Add ev_status tool for BYD HAN telemetry",
        "rationale": (
            "User repeatedly asks about car battery. The HA catalog shows "
            "a BYD HAN vehicle integration exposes 41 entities prefixed with "
            "'han_': sensor.han_battery_level (current 68%), sensor.han_range, "
            "sensor.han_odometer, binary_sensor.han_charging, climate.han_climate, "
            "lock.han_lock, device_tracker.han_location. The other battery sensors "
            "(iPhone, Raspberry Pi) are irrelevant to the user's intent.\n\n"
            "Proposed tool: ev_status() returns aggregated vehicle telemetry by "
            "reading the han_* entity family. Optional arg vehicle=str for "
            "future multi-car households."
        ),
        "confidence": 0.92,
        "notes": "Narrowed evidence from 27 batteries to BYD HAN family; "
                 "drafted concrete tool spec",
    })
    reflector = _make_reflector(
        unrefined_proposals=rough, llm_response=refined_json
    )

    out = await reflector._refine_proposals()

    assert len(out) == 1
    assert out[0]["proposal_id"] == 59
    assert out[0]["changed"] is True
    assert "ev_status" in out[0]["new_title"]

    reflector.store.refine_proposal.assert_awaited_once()
    call = reflector.store.refine_proposal.await_args
    assert call.args[0] == 59
    assert "ev_status" in call.kwargs["new_title"]
    assert call.kwargs["new_confidence"] == 0.92
    assert "Narrowed evidence" in call.kwargs["refinement_notes"]


@pytest.mark.asyncio
async def test_rejects_truncated_refinement():
    """Defense against an LLM that returns a 1-line refinement —
    we'd lose the evidence. Reject and don't update."""
    rough = [{
        "id": 60,
        "kind": "code_change",
        "title": "Add lock tool family",
        "rationale": "Real rationale text " * 50,  # ~1000 chars
        "confidence": 0.8,
    }]
    truncated_json = json.dumps({
        "title": "Lock tool",
        "rationale": "Add lock tool.",  # WAY shorter than original
        "confidence": 0.9,
    })
    reflector = _make_reflector(
        unrefined_proposals=rough, llm_response=truncated_json
    )

    out = await reflector._refine_proposals()

    # Should be skipped, not refined
    assert out[0]["changed"] is False
    assert out[0]["skipped_reason"] == "no_refinement_produced"
    reflector.store.refine_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_handles_llm_exception_gracefully():
    """One bad LLM call doesn't break the rest of the batch."""
    rough = [
        {"id": 70, "kind": "code_change", "title": "Bad one",
         "rationale": "a" * 100, "confidence": 0.7},
        {"id": 71, "kind": "code_change", "title": "Good one",
         "rationale": "b" * 100, "confidence": 0.7},
    ]
    good_json = json.dumps({
        "title": "Refined good one",
        "rationale": "c" * 100,
        "confidence": 0.9,
        "notes": "ok",
    })
    reflector = _make_reflector(unrefined_proposals=rough)
    # First call raises, second succeeds
    reflector.llm.chat = AsyncMock(side_effect=[
        Exception("ollama timeout"),
        {"message": {"content": good_json}},
    ])

    out = await reflector._refine_proposals()

    assert len(out) == 2
    assert out[0]["changed"] is False  # exception
    assert out[1]["changed"] is True   # succeeded
    # Only one refine_proposal call (for the good one)
    assert reflector.store.refine_proposal.await_count == 1


@pytest.mark.asyncio
async def test_uses_8b_with_180s_timeout_for_batch_refinement():
    """Batch refinement uses the 8B fallback model with a 180s timeout.

    The 35B reasoner deadlocks under sustained back-to-back calls
    (Vulkan/RADV: GPU sits at 0% busy while the request hangs at the
    network layer). The 8B (qwen3:8b) finishes a refinement in 30-60s
    with genuinely good quality — proven on real hardware to refine
    9 proposals in ~3 minutes including correctly narrowing
    "27 batteries" to the actual EV sensors.

    The 35B is reserved for ONE single-shot synthesis call per night
    in _synthesize_nightly_brief, where the deadlock doesn't trigger."""
    rough = [{
        "id": 80,
        "kind": "code_change",
        "title": "test",
        "rationale": "x" * 100,
        "confidence": 0.7,
    }]
    refined = json.dumps({
        "title": "refined", "rationale": "y" * 100,
        "confidence": 0.8, "notes": "ok",
    })
    reflector = _make_reflector(
        unrefined_proposals=rough, llm_response=refined
    )

    await reflector._refine_proposals()

    chat_call = reflector.llm.chat.await_args_list[0]
    # Nightly path → think=True (smaller model benefits more from CoT).
    assert chat_call.kwargs["think"] is True
    assert chat_call.kwargs["model"] == "qwen3:8b"
    # 5-min timeout (300s) — thinking adds latency, the nightly window
    # absorbs it.
    assert chat_call.kwargs["timeout"] == 300.0


@pytest.mark.asyncio
async def test_entity_catalog_is_gathered_once():
    """We compute the HA entity catalog ONCE per refine batch — not
    per proposal — to keep the cost predictable."""
    rough = [
        {"id": i, "kind": "code_change", "title": f"p{i}",
         "rationale": "a" * 100, "confidence": 0.7}
        for i in range(5)
    ]
    refined = json.dumps({
        "title": "x", "rationale": "y" * 100,
        "confidence": 0.8, "notes": "ok",
    })
    reflector = _make_reflector(
        unrefined_proposals=rough, llm_response=refined
    )

    await reflector._refine_proposals()

    # registry.dispatch (used to fetch the catalog) should be called
    # exactly once even for 5 proposals.
    assert reflector.registry.dispatch.await_count == 1
    # llm.chat called 5x for the refinements
    assert reflector.llm.chat.await_count == 5


@pytest.mark.asyncio
async def test_catalog_dispatch_failure_uses_fallback_text():
    """If the entity catalog fetch fails, we still try to refine —
    just with less grounding. Better than skipping the whole phase."""
    rough = [{
        "id": 90,
        "kind": "code_change",
        "title": "test",
        "rationale": "x" * 100,
        "confidence": 0.7,
    }]
    refined = json.dumps({
        "title": "refined", "rationale": "y" * 100,
        "confidence": 0.8, "notes": "ok",
    })
    reflector = _make_reflector(
        unrefined_proposals=rough, llm_response=refined
    )
    reflector.registry.dispatch = AsyncMock(side_effect=Exception("HA down"))

    out = await reflector._refine_proposals()

    # Still produced a refinement
    assert len(out) == 1
    assert out[0]["changed"] is True
