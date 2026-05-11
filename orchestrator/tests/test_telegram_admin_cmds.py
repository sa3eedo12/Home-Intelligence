from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.policy_engine import PolicyEngine
from orchestrator.scheduler import JobInfo
from orchestrator.telegram_bot import _make_jobs, _make_mute, _make_quiet


class DummyMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kwargs) -> None:
        self.replies.append(text)


def _update(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        message=DummyMessage(),
    )


def _context(args: list[str]) -> SimpleNamespace:
    return SimpleNamespace(args=args)


@pytest.mark.asyncio
async def test_quiet_on_writes_override_with_ttl_cap() -> None:
    redis = FakeRedis(decode_responses=True)
    policies = {
        "quiet_hours": {"enabled": True, "start": "22:30", "end": "07:00", "tz": "Asia/Dubai"},
        "manual_overrides": {"ttl_minutes_default": 60, "max_minutes": 720},
    }
    engine = PolicyEngine(policies, redis, now_fn=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    handler = _make_quiet({1}, engine)

    update = _update()
    await handler(update, _context(["on"]))

    value = await redis.get("policy:override:quiet")
    ttl = await redis.ttl("policy:override:quiet")
    assert value == "on"
    assert 0 < ttl <= 720 * 60


@pytest.mark.asyncio
async def test_mute_command_writes_expected_ttl() -> None:
    redis = FakeRedis(decode_responses=True)
    policies = {
        "quiet_hours": {"enabled": True, "start": "22:30", "end": "07:00", "tz": "Asia/Dubai"},
        "manual_overrides": {"ttl_minutes_default": 60, "max_minutes": 720},
    }
    engine = PolicyEngine(policies, redis, now_fn=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    handler = _make_mute({1}, engine)

    update = _update()
    await handler(update, _context(["knowledge_notes", "90"]))

    ttl = await redis.ttl("policy:mute:knowledge_notes")
    assert ttl == 5400


@pytest.mark.asyncio
async def test_jobs_command_formats_output() -> None:
    redis = FakeRedis(decode_responses=True)
    policies = {
        "quiet_hours": {"enabled": True, "start": "22:30", "end": "07:00", "tz": "Asia/Dubai"},
        "manual_overrides": {"ttl_minutes_default": 60, "max_minutes": 720},
    }
    _engine = PolicyEngine(policies, redis, now_fn=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    scheduler = SimpleNamespace(
        list_jobs=lambda: [
            JobInfo(
                id="morning_brief",
                next_run_time="2026-01-02T07:30:00+04:00",
                last_run_time=None,
                last_status="never",
            )
        ]
    )

    handler = _make_jobs({1}, scheduler, "http://localhost:8080")
    update = _update()
    await handler(update, _context([]))

    assert "morning_brief" in update.message.replies[-1]
