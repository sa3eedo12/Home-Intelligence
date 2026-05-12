from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.pending import clear_pending, get_pending, set_pending


@pytest.mark.asyncio
async def test_pending_set_get_clear_roundtrip() -> None:
    redis = FakeRedis(decode_responses=True)
    payload = {
        "agent": "household_ops",
        "capability": "chores_complete",
        "inputs": {"chore_id": 42},
        "reason": "Log that bed sheets were washed.",
        "prompt_text": "I'll log that bed sheets were washed.",
    }

    await set_pending(redis, 1234, payload)

    stored = await get_pending(redis, 1234)
    assert stored is not None
    assert stored["agent"] == "household_ops"
    assert stored["capability"] == "chores_complete"
    assert stored["inputs"] == {"chore_id": 42}
    assert stored["reason"] == "Log that bed sheets were washed."
    assert stored["prompt_text"] == "I'll log that bed sheets were washed."
    assert stored["created_at"]
    ttl = await redis.ttl("pending:chat:1234")
    assert 0 < ttl <= 300

    await clear_pending(redis, 1234)
    assert await get_pending(redis, 1234) is None
