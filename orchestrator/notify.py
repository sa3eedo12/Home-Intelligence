from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .policy_engine import NotifyPayload, PolicyEngine

logger = get_logger("notify")

STREAM = "notify.outbound"
GROUP = "orchestrator:notify"


def _to_payload(raw: dict[str, Any], default_chat_id: int) -> NotifyPayload:
    chat_id = int(raw.get("chat_id") or default_chat_id)
    return NotifyPayload(
        chat_id=chat_id,
        text=str(raw.get("text", "")),
        severity=str(raw.get("severity", "info")),
        topic=raw.get("topic"),
        agent=raw.get("agent"),
        capability=raw.get("capability"),
        keyboard=raw.get("keyboard"),
        fingerprint=raw.get("fingerprint"),
    )


async def run_consumer(
    redis: Redis, policy_engine: PolicyEngine, send_fn: Callable[..., Any]
) -> None:
    consumer = "orchestrator-notify-1"

    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    default_chat_id = int((await redis.get("config:telegram_chat_id")) or "0")

    while True:
        try:
            messages = await redis.xreadgroup(
                groupname=GROUP,
                consumername=consumer,
                streams={STREAM: ">"},
                count=10,
                block=1000,
            )
            for stream_name, entries in messages:
                for message_id, fields in entries:
                    payload_raw = json.loads(fields.get("payload", "{}"))
                    payload = _to_payload(payload_raw, default_chat_id)
                    decision = await policy_engine.evaluate(payload)
                    record = {
                        "ts": datetime.now(UTC).isoformat(),
                        "topic": payload.topic,
                        "severity": payload.severity,
                        "agent": payload.agent,
                        "decision": decision.action,
                        "reason": decision.reason,
                        "text": payload.text[:250],
                    }
                    try:
                        if decision.action == "send":
                            await send_fn(payload.chat_id, payload.text, payload.keyboard)
                        elif decision.action == "rollup" and decision.rollup_text:
                            await redis.xadd(
                                STREAM,
                                {
                                    "payload": json.dumps(
                                        {
                                            "chat_id": payload.chat_id,
                                            "text": decision.rollup_text,
                                            "severity": payload.severity,
                                            "topic": payload.topic,
                                            "agent": payload.agent,
                                            "capability": payload.capability,
                                        }
                                    )
                                },
                            )
                    except Exception as exc:
                        logger.warning("notify_send_failed", error=str(exc))
                    finally:
                        await redis.lpush("policy:recent", json.dumps(record))
                        await redis.ltrim("policy:recent", 0, 99)
                        await redis.xack(stream_name, GROUP, message_id)
        except Exception as exc:
            logger.warning("notify_consumer_error", error=str(exc))
