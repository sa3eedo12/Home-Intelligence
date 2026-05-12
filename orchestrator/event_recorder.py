from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from home_agents_sdk.agent_base import ACTIVITY_STREAM
from home_agents_sdk.event_log import EventLogStore
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

logger = get_logger("orchestrator.event_recorder")

CONSUMER_GROUP = "orchestrator:event_recorder"


class EventRecorder:
    """Consumes successful agent activity and persists it to episodic memory."""

    def __init__(self, redis: Redis, store: EventLogStore) -> None:
        self._redis = redis
        self._store = store
        self._consumer = f"recorder-{uuid.uuid4().hex[:8]}"
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        try:
            await self._redis.xgroup_create(
                ACTIVITY_STREAM,
                CONSUMER_GROUP,
                id="$",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("event_recorder_group_create_failed", error=str(exc))
        except Exception as exc:
            logger.warning("event_recorder_group_create_failed", error=str(exc))
        self._task = asyncio.create_task(self._run(), name="event-recorder")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass

    async def handle_payload(self, payload: dict[str, Any]) -> bool:
        if payload.get("status") != "ok":
            return False
        extra = payload.get("extra")
        if isinstance(extra, dict) and extra.get("event_log_recorded"):
            return False
        agent = str(payload.get("agent") or "unknown")
        capability = str(payload.get("capability") or "unknown")
        result = await self._store.record_event(
            agent=agent,
            capability=capability,
            summary=_summarize_activity(payload),
            payload={"activity": payload},
            ts=payload.get("ts") if isinstance(payload.get("ts"), str) else None,
        )
        if not result.get("ok"):
            logger.warning(
                "event_recorder_write_failed",
                agent=agent,
                capability=capability,
                error=result.get("error"),
            )
            return False
        return True

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=self._consumer,
                    streams={ACTIVITY_STREAM: ">"},
                    count=50,
                    block=2000,
                )
                backoff = 1.0
                for stream_name, entries in messages or []:
                    for message_id, fields in entries:
                        await self._handle_fields(fields)
                        await self._redis.xack(stream_name, CONSUMER_GROUP, message_id)
            except asyncio.CancelledError:
                raise
            except ResponseError as exc:
                logger.warning("event_recorder_stream_response_error", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as exc:
                logger.warning("event_recorder_loop_error", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_fields(self, fields: dict[str, Any]) -> None:
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict):
            await self.handle_payload(payload)


def _summarize_activity(payload: dict[str, Any]) -> str:
    agent = str(payload.get("agent") or "unknown")
    capability = str(payload.get("capability") or "unknown")
    duration = payload.get("duration_ms")
    summary = f"{agent}.{capability} completed successfully"
    try:
        duration_ms = float(duration)
    except (TypeError, ValueError):
        return summary
    if duration_ms > 0:
        summary += f" in {round(duration_ms)} ms"
    return summary
