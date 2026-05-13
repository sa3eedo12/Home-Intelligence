from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

logger = get_logger("household_ops.notify")
NOTIFY_STREAM = "notify.outbound"


def _redis_client() -> Redis:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return Redis.from_url(redis_url, decode_responses=True)


async def _publish(stream: str, payload: dict[str, Any]) -> None:
    client = _redis_client()
    try:
        await client.xadd(stream, {"payload": json.dumps(payload, default=str)})
    except Exception as exc:
        logger.warning("stream_publish_failed", stream=stream, error=str(exc))
    finally:
        await client.aclose()


async def publish_chore_due(
    chore: Mapping[str, Any], *, capability: str, reason: str
) -> None:
    title = str(chore.get("title") or "Untitled chore")
    chore_id = chore.get("id")
    due_at = chore.get("due_at")
    payload = {
        "text": f"Chore due: {title}",
        "severity": "notice",
        "topic": "chores.due",
        "agent": "household_ops",
        "capability": capability,
        "chore_id": chore_id,
        "due_at": due_at,
        "reason": reason,
        "fingerprint": f"household_ops:chore_due:{chore_id}:{due_at}",
        "ts": datetime.now(UTC).isoformat(),
    }
    await _publish(NOTIFY_STREAM, payload)
