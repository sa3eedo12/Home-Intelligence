from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from orchestrator.activity import ActivityAggregator


def _event(agent: str, status: str, when: datetime | None = None, **extra) -> dict:
    payload = {
        "agent": agent,
        "capability": extra.get("capability", "do_thing"),
        "status": status,
        "duration_ms": extra.get("duration_ms", 10.0),
        "ts": (when or datetime.now(UTC)).isoformat(),
    }
    if "error" in extra:
        payload["error"] = extra["error"]
    return payload


@pytest.mark.asyncio
async def test_aggregator_backfill_populates_snapshot() -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for ev in [
        _event("home_automation", "ok"),
        _event("home_automation", "error", error="boom"),
        _event("system_health", "ok"),
    ]:
        await fake.xadd("events.activity", {"payload": json.dumps(ev)})

    agg = ActivityAggregator(fake)
    await agg.start()
    # Give the background task a tick; backfill happens before start completes.
    await asyncio.sleep(0.05)

    snapshot = agg.snapshot()
    by_agent = {row["agent"]: row for row in snapshot["agents"]}
    assert "home_automation" in by_agent
    assert by_agent["home_automation"]["ok"] == 1
    assert by_agent["home_automation"]["errors"] == 1
    assert by_agent["system_health"]["ok"] == 1

    await agg.stop()


@pytest.mark.asyncio
async def test_aggregator_recent_events_respects_limit() -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for i in range(20):
        await fake.xadd(
            "events.activity",
            {"payload": json.dumps(_event(f"agent_{i % 3}", "ok"))},
        )
    agg = ActivityAggregator(fake)
    await agg.start()
    await asyncio.sleep(0.05)
    recent = agg.recent_events(limit=10)
    assert len(recent) == 10
    await agg.stop()


@pytest.mark.asyncio
async def test_aggregator_drops_events_outside_window() -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    old_ts = datetime.now(UTC) - timedelta(hours=1)
    fresh_ts = datetime.now(UTC)
    # Both will be ingested into deque, but snapshot should filter old ones.
    await fake.xadd("events.activity", {"payload": json.dumps(_event("a1", "ok", when=old_ts))})
    await fake.xadd("events.activity", {"payload": json.dumps(_event("a1", "ok", when=fresh_ts))})

    agg = ActivityAggregator(fake)
    await agg.start()
    await asyncio.sleep(0.05)
    snapshot = agg.snapshot()
    a1 = next(row for row in snapshot["agents"] if row["agent"] == "a1")
    assert a1["ok"] == 1  # only the fresh one counted in window
    await agg.stop()


@pytest.mark.asyncio
async def test_aggregator_broadcasts_live_events_to_subscribers() -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    agg = ActivityAggregator(fake)
    await agg.start()

    received: list[dict] = []

    async def _consumer() -> None:
        async for event in agg.subscribe():
            received.append(event)
            if len(received) >= 1:
                return

    task = asyncio.create_task(_consumer())
    await asyncio.sleep(0.05)

    await fake.xadd("events.activity", {"payload": json.dumps(_event("live_agent", "started"))})

    try:
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        await agg.stop()

    assert received and received[0]["agent"] == "live_agent"
