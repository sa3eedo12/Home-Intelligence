from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

logger = get_logger("home_automation.notify")
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


async def publish_notification(
    text: str,
    *,
    severity: str = "info",
    topic: str | None = None,
    capability: str | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "text": text,
        "severity": severity,
        "topic": topic,
        "agent": "home_automation",
        "capability": capability,
        "ts": datetime.now(UTC).isoformat(),
    }
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if chat_id_raw:
        try:
            payload["chat_id"] = int(chat_id_raw)
        except ValueError:
            logger.warning("invalid_telegram_chat_id")
    payload.update({key: value for key, value in extra.items() if value is not None})
    await _publish(NOTIFY_STREAM, payload)
