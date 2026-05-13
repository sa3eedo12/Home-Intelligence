from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

logger = get_logger("knowledge_notes.publish")
MEMORY_UPDATES_STREAM = "memory.updates"


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


async def publish_memory_update(
    *,
    update_type: str,
    capability: str,
    entity_kind: str,
    action: str,
    **details: Any,
) -> dict[str, Any]:
    payload = {
        "type": update_type,
        "agent": "knowledge_notes",
        "capability": capability,
        "entity_kind": entity_kind,
        "action": action,
        "ts": datetime.now(UTC).isoformat(),
        **details,
    }
    await _publish(MEMORY_UPDATES_STREAM, payload)
    return payload
