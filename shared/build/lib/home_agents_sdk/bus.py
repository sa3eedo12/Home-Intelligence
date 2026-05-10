from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError


class EventBus:
    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self.client: Redis | None = None

    async def connect(self) -> None:
        self.client = Redis.from_url(self.redis_url, decode_responses=True)
        await self.client.ping()

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        if self.client is None:
            raise RuntimeError("EventBus is not connected")
        event_id = await self.client.xadd(stream, {"payload": json.dumps(payload)})
        return str(event_id)

    async def subscribe(
        self,
        stream: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        group: str | None = None,
    ) -> None:
        if self.client is None:
            raise RuntimeError("EventBus is not connected")

        consumer = f"consumer-{uuid.uuid4().hex[:8]}"
        if group is not None:
            try:
                await self.client.xgroup_create(stream, group, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

        while True:
            if group:
                messages = await self.client.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=10,
                    block=1000,
                )
            else:
                messages = await self.client.xread({stream: "$"}, count=10, block=1000)

            for stream_name, entries in messages:
                for message_id, fields in entries:
                    payload = json.loads(fields.get("payload", "{}"))
                    await handler(payload)
                    if group:
                        await self.client.xack(stream_name, group, message_id)
