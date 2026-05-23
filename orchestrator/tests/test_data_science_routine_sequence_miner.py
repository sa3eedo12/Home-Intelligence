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
    def __init__(self, rows: list[dict], *, existing_status: str | None = None) -> None:
        self.fetch = AsyncMock(return_value=rows)
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=existing_status)
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
async def test_skips_routines_user_already_dismissed() -> None:
    """If the routines row exists with status='dismissed', re-mining
    must not resurrect it as 'suggested' — user said no."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    for day in range(6):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day), "washer", "cycle_complete"))
        eid += 1
        rows.append(_row(
            eid, base + timedelta(days=day, minutes=10), "dryer", "start"
        ))
    for i in range(6):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=10 + i), "dryer", "start"))

    conn = Conn(rows, existing_status="dismissed")
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=4,
        min_confidence=0.50,
        min_lift=1.5,
    )
    result = await miner.run(window_days=30)
    assert result["stored"] == 0
    # Candidate found, but INSERT never issued because of dismissed status.
    assert len(result["candidates"]) == 1
    conn.execute.assert_not_called()


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


# ── Cadence-regularity filter ─────────────────────────────────


@pytest.mark.asyncio
async def test_skips_cron_like_followups_auto_detected() -> None:
    """A subject that fires every 30 minutes ± 5 seconds (cron-like) must
    not become B in any candidate, even if its co-occurrence stats look
    perfect. This was the actual bug observed on the first production
    run: `system_health.anomaly_check` ran every 30 min and won every
    suggestion slot, producing 29 garbage 'X -> anomaly_check' rows."""
    base = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    # 12 user events ("lights off") on different days
    for day in range(12):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day), "home_automation", "lights_off"))
    # 200 cron events: a "system_health.cron_metric" subject (NOT in the
    # static skip list) that fires every 30 min ± 3 sec. CV will be ~0.
    cron_start = base
    for tick in range(200):
        eid += 1
        jitter_seconds = (tick % 5) - 2  # ±2 seconds jitter
        rows.append(_row(
            eid,
            cron_start + timedelta(minutes=30 * tick, seconds=jitter_seconds),
            "system_health",
            "cron_metric",
        ))

    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=4,
        min_confidence=0.50,
        min_lift=1.5,
    )
    result = await miner.run(window_days=400)
    # Auto-detected cron — cron_metric must not appear as B
    for c in result["candidates"]:
        assert "cron_metric" not in c["name"].split(" -> ")[1], (
            f"cron-like subject leaked into candidates: {c['name']!r}"
        )


@pytest.mark.asyncio
async def test_static_skip_list_blocks_known_housekeeping() -> None:
    """Even if cadence detection misses a subject (e.g. it fires fewer
    than min_samples times in the window), the static _SKIP_SUBJECTS
    list still keeps the obvious housekeeping ones out."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    for day in range(8):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day), "washer", "cycle_complete"))
        eid += 1
        # system_health.anomaly_check is on the static skip list
        rows.append(_row(
            eid,
            base + timedelta(days=day, minutes=10),
            "system_health",
            "anomaly_check",
        ))

    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=4,
        min_confidence=0.5,
        min_lift=1.0,
    )
    result = await miner.run(window_days=30)
    for c in result["candidates"]:
        assert "system_health.anomaly_check" not in c["name"], (
            f"static-blocked subject leaked: {c['name']!r}"
        )


def test_detect_cron_like_subjects_helper() -> None:
    """The _detect_cron_like_subjects helper should flag tight cadences
    and leave noisy human events alone."""
    from orchestrator.data_science.routine_sequence_miner import (
        _Event,
        _detect_cron_like_subjects,
    )

    base = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)
    events: list[_Event] = []
    eid = 0
    # Tight cron: 60 min ± 1 sec
    for i in range(20):
        eid += 1
        events.append(_Event(
            event_id=eid,
            ts=base + timedelta(hours=i, seconds=(i % 3) - 1),
            subject="cron.tight",
        ))
    # Bursty human events: very irregular
    for offset_minutes in [0, 5, 600, 605, 1440, 1500, 2880, 3000, 4500, 4700]:
        eid += 1
        events.append(_Event(
            event_id=eid,
            ts=base + timedelta(minutes=offset_minutes),
            subject="human.bursty",
        ))

    cron_like = _detect_cron_like_subjects(events)
    assert "cron.tight" in cron_like
    assert "human.bursty" not in cron_like


def test_detect_cron_like_subjects_ignores_subjects_with_few_samples() -> None:
    """A subject with only 3 observations doesn't have enough data to
    decide cron-ness — leave it in play."""
    from orchestrator.data_science.routine_sequence_miner import (
        _Event,
        _detect_cron_like_subjects,
    )
    base = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)
    events = [
        _Event(event_id=i, ts=base + timedelta(hours=i), subject="rare")
        for i in range(3)
    ]
    cron_like = _detect_cron_like_subjects(events)
    assert "rare" not in cron_like


@pytest.mark.asyncio
async def test_excludes_ha_tool_chain_self_verification() -> None:
    """home_automation.get_entity_state / list_entities / search_entities
    / call_service_in_area are read-only helpers the agent calls right
    after a write to verify the change. Mining 'lights_on ->
    get_entity_state' is uninteresting tool-chain noise, not a routine.
    First production run on TrueNAS surfaced 4 such candidates — this
    test pins the fix."""
    base = datetime(2026, 5, 11, 8, 0, tzinfo=UTC)
    rows: list[dict] = []
    eid = 0
    # 10 days of lights_on -> get_entity_state within 30s, very strong stats
    for day in range(10):
        eid += 1
        rows.append(_row(eid, base + timedelta(days=day), "home_automation", "lights_on"))
        eid += 1
        rows.append(_row(
            eid, base + timedelta(days=day, seconds=30),
            "home_automation", "get_entity_state",
        ))

    conn = Conn(rows)
    miner = RoutineSequenceMiner(
        pool=FakePool(conn),
        window_minutes=30,
        min_support_a=5,
        min_pair_count=4,
        min_confidence=0.5,
        min_lift=1.0,
    )
    result = await miner.run(window_days=30)
    for c in result["candidates"]:
        assert "home_automation.get_entity_state" not in c["name"], (
            f"HA query tool leaked into candidates: {c['name']!r}"
        )
        assert "home_automation.list_entities" not in c["name"]
        assert "home_automation.search_entities" not in c["name"]
