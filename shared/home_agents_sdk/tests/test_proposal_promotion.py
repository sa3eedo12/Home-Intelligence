"""Tests for shared.home_agents_sdk.proposal_promotion."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from home_agents_sdk.proposal_promotion import (
    PROMOTION_KINDS,
    promote_proposal_to_knowledge,
)


def _store(**overrides):
    s = AsyncMock()
    s.upsert_profile = AsyncMock()
    return s


def _kg(**overrides):
    kg = AsyncMock()
    kg.put_habit = AsyncMock(return_value={"id": 1})
    kg.put_preference = AsyncMock(return_value={"key": "x"})
    kg.put_routine = AsyncMock(return_value={"id": 1})
    return kg


@pytest.mark.asyncio
async def test_promote_habit_writes_user_profile_and_habits_table() -> None:
    store, kg = _store(), _kg()
    proposal: dict[str, Any] = {
        "id": 7,
        "kind": "habit_inference",
        "title": "User watches TV around 20:30",
        "rationale": "5 evenings in a row",
        "evidence_event_ids": [1, 2, 3, 4, 5],
        "confidence": 0.8,
    }

    result = await promote_proposal_to_knowledge(
        proposal=proposal, reflection_store=store, knowledge_graph=kg
    )

    assert result["ok"] is True
    assert result["promoted"]["knowledge_table"] == "habits"
    store.upsert_profile.assert_awaited_once()
    kg.put_habit.assert_awaited_once()
    kg.put_preference.assert_not_awaited()
    kg.put_routine.assert_not_awaited()
    # Source carries the proposal id so we can audit later
    assert "proposal:7" in store.upsert_profile.await_args.kwargs["source"]


@pytest.mark.asyncio
async def test_promote_preference_writes_preferences_table() -> None:
    store, kg = _store(), _kg()
    proposal = {
        "id": 11,
        "kind": "preference_inference",
        "title": "Prefers Halal food",
        "rationale": "User said so during onboarding",
        "profile_value": {"diet": "halal", "avoid": ["seafood"]},
        "confidence": 1.0,
    }

    result = await promote_proposal_to_knowledge(
        proposal=proposal, reflection_store=store, knowledge_graph=kg
    )

    assert result["ok"] is True
    assert result["promoted"]["knowledge_table"] == "preferences"
    kg.put_preference.assert_awaited_once()
    kg.put_habit.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_routine_writes_routines_table() -> None:
    store, kg = _store(), _kg()
    proposal = {
        "id": 12,
        "kind": "routine_inference",
        "title": "Morning coffee routine",
        "rationale": "Coffee + lights at 7:30 every weekday",
        "profile_value": [{"step": "lights"}, {"step": "coffee"}],
        "confidence": 0.9,
    }

    result = await promote_proposal_to_knowledge(
        proposal=proposal, reflection_store=store, knowledge_graph=kg
    )

    assert result["ok"] is True
    assert result["promoted"]["knowledge_table"] == "routines"
    kg.put_routine.assert_awaited_once()


@pytest.mark.asyncio
async def test_promote_skips_non_promotable_kinds() -> None:
    store, kg = _store(), _kg()
    for kind in ("code_change", "cleanup_action", "suggested_action", "proactive_suggestion"):
        result = await promote_proposal_to_knowledge(
            proposal={"id": 1, "kind": kind, "title": "x"},
            reflection_store=store,
            knowledge_graph=kg,
        )
        assert result["skipped"] is True
        assert result["reason"] == "kind_not_promotable"
    store.upsert_profile.assert_not_awaited()
    kg.put_habit.assert_not_awaited()
    kg.put_preference.assert_not_awaited()
    kg.put_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_handles_missing_knowledge_graph() -> None:
    """Some test/dev environments don't wire a knowledge_graph; the
    profile write should still happen."""
    store = _store()
    proposal = {"id": 1, "kind": "habit_inference", "title": "h"}

    result = await promote_proposal_to_knowledge(
        proposal=proposal, reflection_store=store, knowledge_graph=None
    )

    assert result["ok"] is True
    assert result["skipped"] == "no_knowledge_graph"
    store.upsert_profile.assert_awaited_once()


@pytest.mark.asyncio
async def test_promote_swallows_kg_errors() -> None:
    """A flaky knowledge_graph mustn't break the accept flow."""
    store = _store()
    kg = AsyncMock()
    kg.put_habit = AsyncMock(side_effect=Exception("db down"))

    result = await promote_proposal_to_knowledge(
        proposal={"id": 1, "kind": "habit_inference", "title": "x"},
        reflection_store=store,
        knowledge_graph=kg,
    )

    assert result["ok"] is False
    assert "db down" in result["error"]


def test_promotion_kinds_set_is_documented() -> None:
    assert PROMOTION_KINDS == {
        "habit_inference",
        "preference_inference",
        "routine_inference",
    }
