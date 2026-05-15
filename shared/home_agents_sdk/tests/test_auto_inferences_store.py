from __future__ import annotations

import json

import pytest

from home_agents_sdk.auto_inferences_store import AutoInferencesStore


class _FakeConn:
    def __init__(self) -> None:
        self.fetchrow_args = None
        self.fetch_args = None
        self.fetchval_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetchrow(self, _query, *args):
        self.fetchrow_args = args
        if len(args) >= 6:
            return {"id": 42}
        return {
            "id": args[0],
            "source_event_log_id": 3,
            "source_kind": "sleep.likely_asleep",
            "inference": "Went to bed",
            "confidence": 0.8,
            "reasoning": "signals",
            "proposed_action": '{"agent":"knowledge_notes"}',
            "status": "confirmed",
            "confirmed_action_result": '{"ok":true}',
            "confirmed_at": None,
            "confirmed_by_chat_id": 123,
            "created_at": None,
        }

    async def fetchval(self, _query, *args):
        self.fetchval_args = args
        return 4

    async def fetch(self, _query, *args):
        self.fetch_args = args
        return [
            {
                "id": 1,
                "source_event_log_id": None,
                "source_kind": "sleep.likely_asleep",
                "inference": "Went to bed",
                "confidence": 0.8,
                "reasoning": None,
                "proposed_action": '{"agent":"knowledge_notes"}',
                "status": "proposed",
                "confirmed_action_result": None,
                "confirmed_at": None,
                "confirmed_by_chat_id": None,
                "created_at": None,
            }
        ]


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_auto_inferences_store_insert_encodes_action() -> None:
    pool = _FakePool()
    store = AutoInferencesStore(pool=pool)

    row_id = await store.insert(
        source_event_log_id=3,
        source_kind="sleep.likely_asleep",
        inference="Went to bed",
        confidence=0.8,
        reasoning="signals",
        proposed_action={"agent": "knowledge_notes"},
    )

    assert row_id == 42
    assert json.loads(pool.conn.fetchrow_args[5]) == {"agent": "knowledge_notes"}


@pytest.mark.asyncio
async def test_auto_inferences_store_confirm_decodes_result() -> None:
    store = AutoInferencesStore(pool=_FakePool())

    record = await store.confirm(42, chat_id=123, action_result={"ok": True})

    assert record is not None
    assert record["proposed_action"] == {"agent": "knowledge_notes"}
    assert record["confirmed_action_result"] == {"ok": True}


@pytest.mark.asyncio
async def test_auto_inferences_store_recent_count_and_recent() -> None:
    pool = _FakePool()
    store = AutoInferencesStore(pool=pool)

    count = await store.recent_count_in_window(hours=1)
    recent = await store.recent(status="proposed", limit=5)

    assert count == 4
    assert pool.conn.fetchval_args == (1,)
    assert pool.conn.fetch_args == ("proposed", 5)
    assert recent[0]["proposed_action"] == {"agent": "knowledge_notes"}


class _CorrectionConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.fetch_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetch(self, _query, *args):
        self.fetch_args = args
        return self._rows


class _CorrectionPool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = _CorrectionConn(rows)

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_correction_counts_returns_per_status_breakdown() -> None:
    pool = _CorrectionPool(
        [
            {"status": "confirmed", "n": 3},
            {"status": "rejected", "n": 1},
            {"status": "skipped", "n": 2},
        ]
    )
    store = AutoInferencesStore(pool=pool)

    counts = await store.correction_counts(source_kind="entertainment.left_on", days=14)

    assert counts == {"confirmed": 3, "rejected": 1, "skipped": 2}
    # Args carried through: (kind, days)
    assert pool.conn.fetch_args == ("entertainment.left_on", 14)


@pytest.mark.asyncio
async def test_correction_counts_returns_zeros_when_no_history() -> None:
    pool = _CorrectionPool([])
    store = AutoInferencesStore(pool=pool)

    counts = await store.correction_counts(source_kind="sleep.likely_asleep")

    assert counts == {"confirmed": 0, "rejected": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_correction_counts_returns_zeros_when_no_pool() -> None:
    store = AutoInferencesStore(pool=None)

    counts = await store.correction_counts(source_kind="anything")

    assert counts == {"confirmed": 0, "rejected": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_correction_counts_returns_zeros_for_empty_kind() -> None:
    pool = _CorrectionPool([{"status": "confirmed", "n": 99}])
    store = AutoInferencesStore(pool=pool)

    counts = await store.correction_counts(source_kind="   ")

    # Empty kind is malformed input; return empty without hitting the DB
    assert counts == {"confirmed": 0, "rejected": 0, "skipped": 0}
    assert pool.conn.fetch_args is None


@pytest.mark.asyncio
async def test_recent_for_inference_returns_count() -> None:
    """Counts existing rows with same source_kind+inference in window."""
    pool = _FakePool()
    store = AutoInferencesStore(pool=pool)

    n = await store.recent_for_inference(
        source_kind="entertainment.left_on",
        inference="left Living Room TV on for 6.0h past your usual bedtime",
        hours=6,
    )

    assert n == 4
    assert pool.conn.fetchval_args == (
        "entertainment.left_on",
        "left Living Room TV on for 6.0h past your usual bedtime",
        6,
    )


@pytest.mark.asyncio
async def test_recent_for_inference_returns_zero_on_empty_inputs() -> None:
    pool = _FakePool()
    store = AutoInferencesStore(pool=pool)

    assert await store.recent_for_inference(source_kind="", inference="x", hours=6) == 0
    assert (
        await store.recent_for_inference(source_kind="x", inference="   ", hours=6) == 0
    )


@pytest.mark.asyncio
async def test_recent_for_inference_returns_zero_with_no_pool() -> None:
    store = AutoInferencesStore(pool=None)
    assert (
        await store.recent_for_inference(source_kind="x", inference="y", hours=6) == 0
    )
