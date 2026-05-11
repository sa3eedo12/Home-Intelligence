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
