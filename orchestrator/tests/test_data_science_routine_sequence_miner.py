"""Tests for RoutineSequenceMiner — A→B-within-W co-occurrence mining."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from orchestrator.data_science.routine_sequence_miner import (
    RoutineSequenceMiner,
    SequenceCandidate,
)
from tests.data_science_fakes import FakePool


class Conn:
    def __init__(self, rows: list[dict]) -> None:
        self.fetch = AsyncMock(return_value=rows)
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=None)
        self.execute = AsyncMock(return_value="INSERT 0 1")


def _row(eid: int, ts: datetime, agent: str, cap: str) -> dict:
    return {"id": eid, "ts": ts, "agent": agent, "capability": cap}


# ── Core mining behaviour ───────────────────────────────────────


@pytest.mark.asyncio
async def test_mines_high_confidence_sequence() -> None:
    """A→B fires 6 times in a row within 15 min, both well above min
    support → must produce one stored candidate."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows: list[dict] = []
    # 6 pairs of washer_done(8:00..) → dryer_start(8:10..) on consecutive days
    eid = 0
    for day in range(6):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day), "washer", "cycle_complete"))
        eid += 1
        rows.append(_row(
            eid, base + timedelta(days=day, minutes=10), "dryer", "start"
        ))
    # Sprinkle dryer noise so support_b is decent
    for i in range(6):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=10 + i), "dryer", "start"))

    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=4,
        min_confidence=0.50,
        min_lift=1.5,
    )
    result = await miner.run(window_days=30)
    assert result["stored"] == 1
    assert len(result["candidates"]) == 1
    cand = result["candidates"][0]
    assert cand["name"] == "washer.cycle_complete -> dryer.start"
    assert cand["confidence"] == pytest.approx(1.0)
    assert cand["pair_count"] == 6
    # Verify the INSERT was issued with an upsert clause.
    args, _ = conn.execute.await_args
    assert "INSERT INTO routines" in args[0]
    assert "ON CONFLICT (name) DO UPDATE" in args[0]


@pytest.mark.asyncio
async def test_filters_below_min_support_a() -> None:
    """A has only 3 occurrences (< min_support_a=5) → must NOT
    produce a candidate even with perfect 1.0 confidence."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    for day in range(3):  # only 3 (below min_support_a)
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day), "a", "x"))
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day, minutes=5), "b", "y"))
    # padding so support_b is high enough not to trigger
    for i in range(20):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=10 + i), "b", "y"))

    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=2,
        min_confidence=0.5,
        min_lift=1.0,
    )
    result = await miner.run(window_days=30)
    assert result["stored"] == 0
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_filters_below_min_lift() -> None:
    """B is *so* common that A→B confidence == base rate (lift ~ 1) →
    suppress as not a real sequence."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    # 10 A events, each followed by a B
    for i in range(10):
        eid += 1
        rows.append(_row(eid, base + timedelta(hours=i * 6), "a", "x"))
        eid += 1
        rows.append(_row(
            eid, base + timedelta(hours=i * 6, minutes=5), "b", "y"
        ))
    # ...but B fires constantly throughout the day (~every hour)
    for i in range(200):
        eid += 1
        rows.append(_row(eid, base + timedelta(hours=i), "b", "y"))

    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=4,
        min_confidence=0.5,
        min_lift=2.0,
    )
    result = await miner.run(window_days=30)
    assert result["stored"] == 0


@pytest.mark.asyncio
async def test_skips_self_sequences() -> None:
    """Same subject repeating shouldn't appear as A→A (that's just dupes)."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows = [
        _row(i + 1, base + timedelta(minutes=i * 5), "a", "x")
        for i in range(10)
    ]
    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=2,
        min_confidence=0.5,
        min_lift=1.0,
    )
    result = await miner.run(window_days=30)
    assert all(
        c["name"] != "a.x -> a.x" for c in result["candidates"]
    )
    assert result["stored"] == 0


@pytest.mark.asyncio
async def test_window_boundary_excludes_late_events() -> None:
    """A→B at exactly W+1 minutes should NOT count as a sequence."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    # 10 A→B pairs but B fires 60min after A; window is 30min
    for day in range(10):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day), "a", "x"))
        eid += 1
        rows.append(_row(
            eid, base + timedelta(days=day, minutes=60), "b", "y"
        ))
    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=2,
        min_confidence=0.5,
        min_lift=1.0,
    )
    result = await miner.run(window_days=30)
    assert result["stored"] == 0


@pytest.mark.asyncio
async def test_handles_empty_event_log() -> None:
    conn = Conn([])
    miner = RoutineSequenceMiner(pool=FakePool(conn))
    result = await miner.run()
    assert result["candidates"] == []
    assert result["stored"] == 0
    assert result["events_seen"] == 0


@pytest.mark.asyncio
async def test_skips_when_pool_missing() -> None:
    miner = RoutineSequenceMiner(pool=None)
    result = await miner.run()
    assert result["status"] == "skipped"
    assert result["reason"] == "postgres_unavailable"


# ── SequenceCandidate shape ─────────────────────────────────────


def test_candidate_serialization_shape() -> None:
    c = SequenceCandidate(
        subject_a="washer.cycle_complete",
        subject_b="dryer.start",
        pair_count=8,
        support_a=10,
        support_b=12,
        confidence=0.8,
        lift=4.5,
        window_minutes=30,
        sample_event_ids=[1, 2, 3, 4],
    )
    assert c.name == "washer.cycle_complete -> dryer.start"
    steps = c.to_steps()
    assert steps[0]["trigger"] == "washer.cycle_complete"
    assert steps[1]["action"] == "dryer.start"
    assert steps[1]["follows_within_minutes"] == 30
    attrs = c.to_attributes()
    assert attrs["confidence"] == 0.8
    assert attrs["lift"] == 4.5
    assert attrs["pair_count"] == 8
    assert attrs["sample_event_ids"] == [1, 2, 3, 4]


# ── Noise/housekeeping filter ───────────────────────────────────


@pytest.mark.asyncio
async def test_skips_blacklisted_subjects() -> None:
    """data_science.pattern_mining and similar housekeeping subjects
    must not become A or B in candidates."""
    base = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    for day in range(10):
        eid += 1
        rows.append(_row(
            eid, base + timedelta(days=day),
            "data_science", "pattern_mining",
        ))
        eid += 1
        rows.append(_row(
            eid, base + timedelta(days=day, minutes=10), "b", "y"
        ))
    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=2,
        min_confidence=0.5,
        min_lift=1.0,
    )
    result = await miner.run()
    # data_science.pattern_mining should never appear as the trigger
    for c in result["candidates"]:
        assert c["name"] != "data_science.pattern_mining -> b.y"
