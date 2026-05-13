from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

logger = get_logger("system_health.publish")
EVENTS_SYSTEM_STREAM = "events.system"


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


async def publish_metric_breach(
    *,
    metric: str,
    value: Any,
    threshold: Any,
    severity: str = "warn",
    summary: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "system.metric_breach",
        "agent": "system_health",
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "severity": severity,
        "host": socket.gethostname(),
        "ts": datetime.now(UTC).isoformat(),
    }
    if summary:
        payload["summary"] = summary
    payload.update(details)
    await _publish(EVENTS_SYSTEM_STREAM, payload)
    return payload
