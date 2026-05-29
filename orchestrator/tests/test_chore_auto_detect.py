"""Tests for orchestrator.chore_auto_detect — chore_log side-effect from
the events.observed stream."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.chore_auto_detect import _process_one


def _fake_store(*, templates=None):
    store = SimpleNamespace(
        list_templates=AsyncMock(return_value=templates or []),
        log_completion=AsyncMock(return_value=99),
    )
    return store


@pytest.mark.asyncio
async def test_process_one_marks_vacuum_chore_complete_on_cleaning_event() -> None:
    store = _fake_store(templates=[
        {"id": 1, "name": "Vacuum the house", "auto_detect_kind": "vacuum",
         "auto_detect_entity": None},
        {"id": 2, "name": "Laundry load", "auto_detect_kind": "washer",
         "auto_detect_entity": None},
    ])
    fields = {
        "kind": "cleaning.completed",
        "payload": json.dumps({
            "payload": {
                "appliance": "vacuum", "entity_id": "vacuum.dreame_l10s",
            },
        }),
    }
    await _process_one(fields, store)
    store.log_completion.assert_awaited_once()
    call = store.log_completion.await_args
    assert call.kwargs["template_id"] == 1
    assert call.kwargs["source"] == "auto_vacuum"
    assert "vacuum.dreame_l10s" in call.kwargs["note"]


@pytest.mark.asyncio
async def test_process_one_prefers_entity_match_when_available() -> None:
    store = _fake_store(templates=[
        {"id": 1, "name": "Vacuum upstairs", "auto_detect_kind": "vacuum",
         "auto_detect_entity": "vacuum.upstairs"},
        {"id": 2, "name": "Vacuum the house", "auto_detect_kind": "vacuum",
         "auto_detect_entity": None},
    ])
    fields = {
        "kind": "cleaning.completed",
        "payload": json.dumps({
            "payload": {"appliance": "vacuum", "entity_id": "vacuum.upstairs"},
        }),
    }
    await _process_one(fields, store)
    assert store.log_completion.await_args.kwargs["template_id"] == 1


@pytest.mark.asyncio
async def test_process_one_ignores_non_eligible_kinds() -> None:
    store = _fake_store(templates=[{"id": 1, "auto_detect_kind": "vacuum",
                                     "name": "x", "auto_detect_entity": None}])
    fields = {
        "kind": "presence.changed",
        "payload": json.dumps({"payload": {"appliance": "human"}}),
    }
    await _process_one(fields, store)
    store.log_completion.assert_not_called()


@pytest.mark.asyncio
async def test_process_one_skips_when_no_matching_template() -> None:
    store = _fake_store(templates=[
        {"id": 1, "name": "Vacuum", "auto_detect_kind": "vacuum",
         "auto_detect_entity": None},
    ])
    fields = {
        "kind": "appliance.cycle_completed",
        "payload": json.dumps({"payload": {"appliance": "washer"}}),
    }
    await _process_one(fields, store)
    # No washer template → no completion
    store.log_completion.assert_not_called()


@pytest.mark.asyncio
async def test_process_one_ignores_dryer_cycle_for_washer_chore() -> None:
    """appliance.cycle_completed fires for washer + dryer + dishwasher;
    the washer chore should not auto-close on a dryer event."""
    store = _fake_store(templates=[
        {"id": 1, "name": "Laundry load", "auto_detect_kind": "washer",
         "auto_detect_entity": None},
    ])
    fields = {
        "kind": "appliance.cycle_completed",
        "payload": json.dumps({"payload": {"appliance": "dryer"}}),
    }
    await _process_one(fields, store)
    store.log_completion.assert_not_called()


@pytest.mark.asyncio
async def test_process_one_accepts_bytes_fields() -> None:
    store = _fake_store(templates=[
        {"id": 1, "name": "Vacuum", "auto_detect_kind": "vacuum",
         "auto_detect_entity": None},
    ])
    fields = {
        b"kind": b"cleaning.completed",
        b"payload": json.dumps({
            "payload": {"appliance": "vacuum", "entity_id": "vacuum.x"},
        }).encode(),
    }
    # Bytes keys won't be matched by .get("kind"); the consumer is built
    # for decode_responses=True clients. This test guards that the
    # decoder path doesn't blow up if a bytes value sneaks through.
    await _process_one(fields, store)
    # No match because str keys, but no crash either.
    store.log_completion.assert_not_called()
