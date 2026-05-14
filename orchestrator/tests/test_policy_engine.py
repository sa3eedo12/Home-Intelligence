from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.policy_engine import NotifyPayload, PolicyEngine

POLICIES = {
    "quiet_hours": {"enabled": True, "start": "22:30", "end": "07:00", "tz": "Asia/Dubai"},
    "allow_during_quiet": [
        {"severity": "critical"},
        {"topic_pattern": "doorbell.*"},
        {"capability_pattern": "personal_assistant.evening_recap"},
    ],
    "rate_limits": [
        {
            "id": "doorbell.flood",
            "match": {"topic_pattern": "doorbell.*"},
            "max": 6,
            "window_minutes": 10,
            "rollup_message": "{count} more doorbell events in the last {window_minutes} min.",
        }
    ],
    "dedupe": {"default_fingerprint": "{agent}|{topic}|{text|sha256:64}", "window_minutes": 30},
    "manual_overrides": {"ttl_minutes_default": 60, "max_minutes": 720},
}


def _engine(now: datetime) -> tuple[PolicyEngine, FakeRedis]:
    redis = FakeRedis(decode_responses=True)
    return PolicyEngine(POLICIES, redis, now_fn=lambda: now), redis


@pytest.mark.asyncio
async def test_quiet_hours_suppresses_info_but_allows_critical() -> None:
    now = datetime(2026, 1, 1, 19, 0, tzinfo=UTC)  # 23:00 Asia/Dubai
    engine, _redis = _engine(now)

    info_decision = await engine.evaluate(NotifyPayload(chat_id=1, text="ping", severity="info"))
    critical_decision = await engine.evaluate(
        NotifyPayload(chat_id=1, text="ping", severity="critical")
    )

    assert info_decision.action == "suppress"
    assert info_decision.reason == "quiet_hours"
    assert critical_decision.action == "send"


@pytest.mark.asyncio
async def test_allowlist_topic_passes_during_quiet_hours() -> None:
    now = datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    engine, _redis = _engine(now)

    decision = await engine.evaluate(
        NotifyPayload(
            chat_id=1,
            text="motion",
            severity="notice",
            topic="doorbell.motion",
            agent="home_automation",
        )
    )
    assert decision.action == "send"


@pytest.mark.asyncio
async def test_rate_limit_rollup_after_six_messages() -> None:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    engine, _redis = _engine(now)

    decisions = []
    for i in range(7):
        decisions.append(
            await engine.evaluate(
                NotifyPayload(
                    chat_id=1,
                    text=f"doorbell {i}",
                    severity="notice",
                    topic="doorbell.motion",
                    agent="home_automation",
                )
            )
        )

    assert [d.action for d in decisions[:6]] == ["send"] * 6
    assert decisions[6].action == "rollup"


@pytest.mark.asyncio
async def test_dedupe_suppresses_second_duplicate() -> None:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    engine, _redis = _engine(now)

    first = await engine.evaluate(
        NotifyPayload(chat_id=1, text="same", topic="misc", agent="system_health")
    )
    second = await engine.evaluate(
        NotifyPayload(chat_id=1, text="same", topic="misc", agent="system_health")
    )

    assert first.action == "send"
    assert second.action == "suppress"
    assert second.reason == "dedupe"


@pytest.mark.asyncio
async def test_manual_mute_suppresses_agent_until_ttl_expires() -> None:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    engine, redis = _engine(now)
    await redis.set("policy:mute:home_automation", "1", ex=60)

    decision = await engine.evaluate(
        NotifyPayload(chat_id=1, text="x", topic="doorbell.motion", agent="home_automation")
    )
    assert decision.action == "suppress"
    assert decision.reason == "manual_mute"


@pytest.mark.asyncio
async def test_quiet_override_off_disables_quiet_hours() -> None:
    now = datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    engine, redis = _engine(now)
    await redis.set("policy:override:quiet", "off", ex=300)

    decision = await engine.evaluate(NotifyPayload(chat_id=1, text="x", topic="misc", agent="x"))
    assert decision.action == "send"


# ── Member-aware quiet hours (the 22:33 false-suppress regression) ────────


def _member_pool(rows: list[dict]):
    """Build the asyncpg-style pool the policy engine uses to read sleep
    windows. Single-connection, single-fetch — that's all we need."""
    from unittest.mock import AsyncMock, MagicMock

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _engine_with_pool(now: datetime, pool):
    redis = FakeRedis(decode_responses=True)
    return PolicyEngine(POLICIES, redis, now_fn=lambda: now, pool=pool), redis


@pytest.mark.asyncio
async def test_member_sleep_window_overrides_static_quiet_hours_default() -> None:
    """REGRESSION: at 22:33 local (Saeed's bedtime is 00:30) the policy
    engine was suppressing every TV/wind-down notification under
    'quiet_hours' because the static YAML default fired at 22:30.
    With member sleep_time data wired in, 22:33 should be ALLOWED.
    """
    from datetime import time as time_t

    # 22:33 Asia/Dubai = 18:33 UTC
    now = datetime(2026, 5, 14, 18, 33, tzinfo=UTC)
    pool = _member_pool(
        [{"sleep_time": time_t(0, 30), "wake_time": time_t(9, 0)}]
    )
    engine, _redis = _engine_with_pool(now, pool)

    decision = await engine.evaluate(
        NotifyPayload(
            chat_id=1,
            text="🌙 Want me to dim the lights?",
            severity="notice",
            topic="sleep.bedtime",
        )
    )
    assert decision.action == "send"
    assert decision.reason == "allowed"


@pytest.mark.asyncio
async def test_member_sleep_window_suppresses_when_inside_window() -> None:
    """At 02:00 local (well inside the 00:30-09:00 sleep window) the
    notification SHOULD be suppressed — same fix, opposite case."""
    from datetime import time as time_t

    # 02:00 Asia/Dubai = 22:00 UTC the day before
    now = datetime(2026, 5, 14, 22, 0, tzinfo=UTC)
    pool = _member_pool(
        [{"sleep_time": time_t(0, 30), "wake_time": time_t(9, 0)}]
    )
    engine, _redis = _engine_with_pool(now, pool)

    decision = await engine.evaluate(
        NotifyPayload(chat_id=1, text="late ping", severity="notice")
    )
    assert decision.action == "suppress"
    assert decision.reason == "quiet_hours"


@pytest.mark.asyncio
async def test_member_sleep_window_handles_midnight_crossing() -> None:
    """A traditional 23:00 -> 07:00 sleeper: 23:30 IS quiet, 22:00 IS NOT."""
    from datetime import time as time_t

    pool = _member_pool(
        [{"sleep_time": time_t(23, 0), "wake_time": time_t(7, 0)}]
    )

    # 23:30 Asia/Dubai = 19:30 UTC
    engine_inside, _ = _engine_with_pool(
        datetime(2026, 5, 14, 19, 30, tzinfo=UTC), pool
    )
    inside = await engine_inside.evaluate(
        NotifyPayload(chat_id=1, text="x", severity="info")
    )
    assert inside.action == "suppress"

    # 22:00 Asia/Dubai = 18:00 UTC — fresh pool to reset the cache cleanly
    pool2 = _member_pool(
        [{"sleep_time": time_t(23, 0), "wake_time": time_t(7, 0)}]
    )
    engine_outside, _ = _engine_with_pool(
        datetime(2026, 5, 14, 18, 0, tzinfo=UTC), pool2
    )
    outside = await engine_outside.evaluate(
        NotifyPayload(chat_id=1, text="x", severity="info")
    )
    assert outside.action == "send"


@pytest.mark.asyncio
async def test_quiet_hours_falls_back_to_yaml_when_no_member_data() -> None:
    """Empty household_members table -> use the YAML window."""
    pool = _member_pool([])
    # 23:00 local = 19:00 UTC; YAML says start=22:30 so this IS quiet.
    now = datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    engine, _redis = _engine_with_pool(now, pool)

    decision = await engine.evaluate(
        NotifyPayload(chat_id=1, text="x", severity="info")
    )
    assert decision.action == "suppress"
    assert decision.reason == "quiet_hours"


@pytest.mark.asyncio
async def test_quiet_hours_falls_back_when_pool_query_fails() -> None:
    """Flaky DB doesn't break notifications — fall back to YAML."""
    from unittest.mock import AsyncMock, MagicMock

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception("connection lost"))
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    # 23:00 local — YAML default makes this quiet
    now = datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    engine, _redis = _engine_with_pool(now, pool)

    decision = await engine.evaluate(
        NotifyPayload(chat_id=1, text="x", severity="info")
    )
    assert decision.action == "suppress"


@pytest.mark.asyncio
async def test_member_sleep_windows_unioned_quietest_member_wins() -> None:
    """If two members have different sleep schedules, ANY of them being
    asleep mutes notifications. Conservative: better under-notify at
    01:00 than wake the night-owl's spouse."""
    from datetime import time as time_t

    pool = _member_pool(
        [
            {"sleep_time": time_t(22, 0), "wake_time": time_t(6, 0)},
            {"sleep_time": time_t(0, 30), "wake_time": time_t(9, 0)},
        ]
    )
    # 22:30 local = 18:30 UTC. First member is asleep, second is not.
    now = datetime(2026, 5, 14, 18, 30, tzinfo=UTC)
    engine, _redis = _engine_with_pool(now, pool)

    decision = await engine.evaluate(
        NotifyPayload(chat_id=1, text="x", severity="info")
    )
    assert decision.action == "suppress"
    assert decision.reason == "quiet_hours"


@pytest.mark.asyncio
async def test_quiet_override_off_still_works_with_member_windows() -> None:
    """The dashboard 'Quiet off' button must always win over member windows."""
    from datetime import time as time_t

    pool = _member_pool(
        [{"sleep_time": time_t(0, 30), "wake_time": time_t(9, 0)}]
    )
    # 02:00 local — deep inside the sleep window
    now = datetime(2026, 5, 14, 22, 0, tzinfo=UTC)
    engine, redis = _engine_with_pool(now, pool)

    await redis.set("policy:override:quiet", "off")

    decision = await engine.evaluate(
        NotifyPayload(chat_id=1, text="x", severity="info")
    )
    assert decision.action == "send"
