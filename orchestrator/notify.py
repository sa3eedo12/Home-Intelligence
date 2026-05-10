from __future__ import annotations

import json
from collections.abc import Callable

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

logger = get_logger("notify")

STREAM = "notify.outbound"
GROUP = "orchestrator:notify"


async def run_consumer(redis_url: str, send_fn: Callable) -> None:
    client = Redis.from_url(redis_url, decode_responses=True)
    consumer = "orchestrator-notify-1"

    try:
        await client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    while True:
        try:
            messages = await client.xreadgroup(
                groupname=GROUP,
                consumername=consumer,
                streams={STREAM: ">"},
                count=10,
                block=1000,
            )
            for _stream_name, entries in messages:
                for message_id, fields in entries:
                    try:
                        payload = json.loads(fields.get("payload", "{}"))
                        chat_id = int(payload.get("chat_id", 0))
                        text = payload.get("text", "")
                        keyboard = payload.get("keyboard")
                        # TODO PR 4: enforce quiet hours and rate limits here
                        await send_fn(chat_id, text, keyboard)
                    except Exception as exc:
                        logger.warning("notify_send_failed", error=str(exc))
                    finally:
                        await client.xack(STREAM, GROUP, message_id)
        except Exception as exc:
            logger.warning("notify_consumer_error", error=str(exc))
