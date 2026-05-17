"""Tests for the reflector's capability_gap mining phase.

These pin the self-improvement loop: unresolved gaps come in, clustered
proposals go out, gaps get marked resolved. This is the architectural
centrepiece for "system gets smarter every week."
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.reflector import NightlyReflector


def _make_reflector(
    *,
    gap_store_data: list[dict] | None = None,
    llm_response: str | None = None,
    add_proposal_returns: int = 999,
):
    """Build a NightlyReflector with mocked gap_store + llm + store."""
    pool = MagicMock()
    redis = MagicMock()
    llm = MagicMock()
    if llm_response is not None:
        llm.chat = AsyncMock(
            return_value={"message": {"content": llm_response}}
        )
    else:
        llm.chat = AsyncMock(
            return_value={"message": {"content": "{}"}}
        )
    registry = MagicMock()

    gap_store = MagicMock()
    gap_store.list_unresolved = AsyncMock(return_value=gap_store_data or [])
    gap_store.mark_resolved = AsyncMock(return_value=True)

    reflector = NightlyReflector(
        pool=pool,
        redis=redis,
        llm=llm,
        registry=registry,
        reasoner_model="qwen3:8b",
        fallback_model="qwen3:8b",
        gap_store=gap_store,
    )
    # Replace the real store with a mock so we can assert on
    # add_proposal calls without hitting a real DB.
    reflector.store = MagicMock()
    reflector.store.add_proposal = AsyncMock(return_value=add_proposal_returns)
    return reflector, gap_store


@pytest.mark.asyncio
async def test_no_gaps_returns_empty():
    reflector, gap_store = _make_reflector(gap_store_data=[])

    out = await reflector._mine_capability_gaps()

    assert out == []
    gap_store.mark_resolved.assert_not_called()
    reflector.store.add_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_no_gap_store_returns_empty():
    """A reflector built without a gap_store (test fixture without DB)
    must not crash — just return empty."""
    reflector, _ = _make_reflector(gap_store_data=[])
    reflector.gap_store = None

    out = await reflector._mine_capability_gaps()

    assert out == []


@pytest.mark.asyncio
async def test_climate_cluster_produces_proposal_and_resolves_gaps():
    """The headline test: three thermostat-related gaps cluster into
    one code_change proposal. All three gaps mark_resolved with the
    same proposal_id."""
    gaps = [
        {"id": 1, "user_text": "reduce the bedroom temperature",
         "failure_reason": "chat_fallback_for_action_verb"},
        {"id": 2, "user_text": "set the AC in the office to 22",
         "failure_reason": "chat_fallback_for_action_verb"},
        {"id": 3, "user_text": "make the living room cooler",
         "failure_reason": "chat_fallback_for_action_verb"},
    ]
    llm_proposal = json.dumps({
        "title": "Add climate_set_temperature tool",
        "rationale": "Three user requests targeting thermostats in different "
                     "rooms went to chat fallback. Example: 'reduce the bedroom "
                     "temperature'. A dedicated climate_set_temperature tool "
                     "with area parameter would resolve all three cleanly.",
        "proposed_change_kind": "new_tool",
        "proposed_tool_spec": {
            "tool_id": "climate_set_temperature",
            "description": "Set target temperature on a thermostat by area",
            "inputs": {"area": "string", "temperature": "number"},
        },
        "confidence": 0.9,
        "impact_estimate": "Eliminates hallucinated thermostat replies for "
                           "natural-language requests",
    })
    reflector, gap_store = _make_reflector(
        gap_store_data=gaps,
        llm_response=llm_proposal,
        add_proposal_returns=77,
    )

    out = await reflector._mine_capability_gaps()

    # One cluster ('climate') with three gaps
    assert len(out) == 1
    assert out[0]["domain"] == "climate"
    assert out[0]["gap_count"] == 3
    assert out[0]["proposal_id"] == 77
    assert "climate_set_temperature" in out[0]["title"]
    # All three gaps marked resolved with proposal_id=77
    assert gap_store.mark_resolved.await_count == 3
    for call in gap_store.mark_resolved.await_args_list:
        assert call.kwargs["proposal_id"] == 77
    # Proposal was filed as code_change
    reflector.store.add_proposal.assert_awaited_once()
    proposal_kwargs = reflector.store.add_proposal.await_args.kwargs
    assert proposal_kwargs["kind"] == "code_change"
    assert "climate_set_temperature" in proposal_kwargs["title"]
    # Rationale includes the gap evidence
    assert "Gap ids:" in proposal_kwargs["rationale"]
    assert "[1, 2, 3]" in proposal_kwargs["rationale"]
    # Proposed tool spec embedded
    assert "Proposed tool spec:" in proposal_kwargs["rationale"]


@pytest.mark.asyncio
async def test_low_confidence_proposal_is_skipped():
    """When the LLM returns confidence < 0.4, we skip — better to wait
    for more evidence than file a speculative proposal."""
    gaps = [
        {"id": 1, "user_text": "do something weird",
         "failure_reason": "chat_fallback_for_action_verb"},
    ]
    llm_proposal = json.dumps({
        "title": "Maybe add a thing",
        "confidence": 0.2,
        "rationale": "vague",
    })
    reflector, gap_store = _make_reflector(
        gap_store_data=gaps,
        llm_response=llm_proposal,
    )

    out = await reflector._mine_capability_gaps()

    # Cluster appears but with no proposal_id (skipped)
    assert len(out) == 1
    assert out[0]["proposal_id"] is None
    # Gap stays unresolved
    gap_store.mark_resolved.assert_not_called()
    reflector.store.add_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_empty_title_is_skipped():
    """LLM-detected noise: empty title means 'this cluster is too vague'."""
    gaps = [{"id": 1, "user_text": "x", "failure_reason": "chat_fallback_for_action_verb"}]
    llm_proposal = json.dumps({
        "title": "",
        "confidence": 0.8,
    })
    reflector, gap_store = _make_reflector(
        gap_store_data=gaps,
        llm_response=llm_proposal,
    )

    out = await reflector._mine_capability_gaps()

    assert out[0]["proposal_id"] is None
    gap_store.mark_resolved.assert_not_called()


@pytest.mark.asyncio
async def test_llm_exception_in_one_cluster_does_not_break_others():
    """Robustness: one failing cluster doesn't poison the whole mining
    phase — others still get processed."""
    gaps = [
        # Climate cluster (3) — LLM will error on this
        {"id": 1, "user_text": "set bedroom thermostat", "failure_reason": "x"},
        {"id": 2, "user_text": "raise AC", "failure_reason": "x"},
        {"id": 3, "user_text": "cool the office", "failure_reason": "x"},
        # Cover cluster (2) — LLM will succeed on this
        {"id": 4, "user_text": "open the blinds", "failure_reason": "x"},
        {"id": 5, "user_text": "close the curtains", "failure_reason": "x"},
    ]
    success_response = json.dumps({
        "title": "Add cover_set_position tool",
        "rationale": "open the blinds, close the curtains both failed",
        "confidence": 0.85,
        "impact_estimate": "covers work",
    })
    reflector, gap_store = _make_reflector(gap_store_data=gaps)
    # First cluster (climate) errors, second (cover) succeeds
    reflector.llm.chat = AsyncMock(side_effect=[
        Exception("ollama timeout"),
        {"message": {"content": success_response}},
    ])

    out = await reflector._mine_capability_gaps()

    assert len(out) == 2
    # Climate cluster proposal not filed (LLM errored inside
    # _draft_gap_proposal which catches and returns None)
    climate_result = next(r for r in out if r["domain"] == "climate")
    assert climate_result["proposal_id"] is None
    # Cover cluster succeeded
    cover_result = next(r for r in out if r["domain"] == "cover")
    assert cover_result["proposal_id"] == 999  # default mock return
    # Only cover gaps marked resolved (the 2 cover ids)
    resolved_ids = {c.args[0] for c in gap_store.mark_resolved.await_args_list}
    assert resolved_ids == {4, 5}


@pytest.mark.asyncio
async def test_clusters_capped_at_5():
    """Pathological case: 20 different domains. We cap at 5 largest to
    keep prompt cost predictable."""
    # 10 different "other" domains × 2 gaps each = 20 gaps in 10 clusters
    # No, that's the same cluster ('other'). Let me make distinct clusters.
    gaps = []
    # Domain patterns we know cluster cleanly
    seeds = [
        ("climate", "temperature"),
        ("cover", "blinds"),
        ("media_player", "music"),
        ("fan", "fan"),
        ("lock", "lock the door"),
        ("vacuum", "vacuum"),
        ("notification", "remind me"),
        ("status_query", "what's the"),
    ]
    next_id = 1
    for label, seed in seeds:
        # 2 gaps per cluster
        for _ in range(2):
            gaps.append({"id": next_id, "user_text": f"please {seed}",
                         "failure_reason": "x"})
            next_id += 1

    # LLM returns a generic empty so all are skipped — we just want to
    # count how many clusters got an LLM call.
    reflector, gap_store = _make_reflector(
        gap_store_data=gaps,
        llm_response='{"title": "", "confidence": 0.0}',
    )

    out = await reflector._mine_capability_gaps()

    # Should have processed AT MOST 5 clusters (the cap)
    assert len(out) <= 5
    # And the LLM should have been called exactly that many times
    assert reflector.llm.chat.await_count == len(out)


def test_classify_gap_domain_picks_climate():
    reflector, _ = _make_reflector()
    assert reflector._classify_gap_domain("reduce the bedroom temperature") == "climate"
    assert reflector._classify_gap_domain("set thermostat to 22") == "climate"
    assert reflector._classify_gap_domain("warm up the office") == "climate"


def test_classify_gap_domain_picks_cover():
    reflector, _ = _make_reflector()
    assert reflector._classify_gap_domain("open the blinds") == "cover"
    assert reflector._classify_gap_domain("close the curtains in the bedroom") == "cover"


def test_classify_gap_domain_falls_back_to_other():
    reflector, _ = _make_reflector()
    assert reflector._classify_gap_domain("do the impossible thing") == "other"
