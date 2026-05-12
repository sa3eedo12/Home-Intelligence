from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.pending import get_pending, set_pending
from orchestrator.policy_engine import PolicyEngine
from orchestrator.scheduler import JobInfo
from orchestrator.telegram_bot import _make_cancel, _make_jobs, _make_mute, _make_quiet, _make_text


class DummyMessage:
    def __init__(self, text: str = "", chat_id: int = 100) -> None:
        self.text = text
        self.chat_id = chat_id
        self.replies: list[str] = []
        self.reply_kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)


def _update(user_id: int = 1, text: str = "", chat_id: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=DummyMessage(text=text, chat_id=chat_id),
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


@pytest.mark.asyncio
async def test_cancel_command_clears_pending_action() -> None:
    redis = FakeRedis(decode_responses=True)
    await set_pending(
        redis,
        100,
        {
            "agent": "household_ops",
            "capability": "chores_complete",
            "inputs": {"chore_id": 1},
            "reason": "Mark sheets washed.",
            "prompt_text": "I'll log that bed sheets were washed.",
        },
    )
    handler = _make_cancel({1}, redis)

    update = _update(chat_id=100)
    await handler(update, _context([]))

    assert await get_pending(redis, 100) is None
    assert update.message.replies[-1] == "Cancelled pending action."


@pytest.mark.asyncio
async def test_yes_after_pending_action_executes_and_clears_without_routing() -> None:
    redis = FakeRedis(decode_responses=True)
    pending = {
        "agent": "household_ops",
        "capability": "chores_complete",
        "inputs": {"chore_id": 7},
        "reason": "Mark sheets washed.",
        "prompt_text": "I'll log that bed sheets were washed.",
    }
    await set_pending(redis, 100, pending)
    router = SimpleNamespace(
        handle=AsyncMock(),
        execute_pending=AsyncMock(return_value={"reply": "Logged. Anything else?"}),
    )
    handler = _make_text({1}, router, redis)

    update = _update(text="yes", chat_id=100)
    await handler(update, _context([]))

    router.execute_pending.assert_awaited_once()
    router.handle.assert_not_called()
    assert await get_pending(redis, 100) is None
    assert update.message.replies[-1] == "Logged. Anything else?"
