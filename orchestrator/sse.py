"""Server-Sent Events endpoint feeding the live dashboard."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

logger = get_logger("orchestrator.sse")

router = APIRouter(tags=["dashboard"])

HEARTBEAT_INTERVAL_SECONDS = 15
PRESENCE_POLL_SECONDS = 10


def _sse_format(event: str, data: Any) -> str:
    serialized = data if isinstance(data, str) else json.dumps(data, default=str)
    return f"event: {event}\ndata: {serialized}\n\n"


async def _curator_poll(redis: Redis) -> AsyncIterator[dict[str, Any]]:
    """Poll the curator's narrative keys; yield only when they change."""
    keys = ["dashboard:narrative", "dashboard:alert_narrative"]
    last_seen: dict[str, str | None] = {k: None for k in keys}
    while True:
        try:
            values = await redis.mget(keys)
        except Exception:
            await asyncio.sleep(5)
            continue
        for key, raw in zip(keys, values, strict=False):
            if raw and raw != last_seen[key]:
                last_seen[key] = raw
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                yield {"key": key, "record": parsed}
        await asyncio.sleep(5)


async def _presence_poll(
    fetch: Callable[[], Awaitable[list[str]]],
) -> AsyncIterator[dict[str, Any]]:
    """Poll people_home every PRESENCE_POLL_SECONDS; yield ONLY when it
    changes. Without this, the dashboard's "Who's home" string was rendered
    server-side at page load and never updated — when the user left the
    house, it kept showing them as home until manual refresh.
    """
    last_seen: list[str] | None = None
    while True:
        try:
            current = await fetch()
        except Exception:  # noqa: BLE001
            await asyncio.sleep(PRESENCE_POLL_SECONDS)
            continue
        # Sort so order changes don't trigger spurious events
        normalized = sorted(current or [])
        if last_seen != normalized:
            last_seen = normalized
            yield {"people_home": normalized}
        await asyncio.sleep(PRESENCE_POLL_SECONDS)


@router.get("/dashboard/stream")
async def dashboard_stream(request: Request) -> StreamingResponse:
    aggregator = request.app.state.activity_aggregator
    redis: Redis = request.app.state.redis
    # Pull the people_home fetcher off app.state if wired, else no-op.
    presence_fetch: Callable[[], Awaitable[list[str]]] | None = getattr(
        request.app.state, "people_home_fetch", None
    )

    async def event_source() -> AsyncIterator[str]:
        # Initial snapshot so the page has data immediately.
        try:
            snapshot = aggregator.snapshot()
            yield _sse_format("snapshot", snapshot)
        except Exception as exc:
            logger.warning("sse_snapshot_failed", error=str(exc))

        activity_iter = aggregator.subscribe().__aiter__()
        curator_iter = _curator_poll(redis).__aiter__()
        presence_iter = (
            _presence_poll(presence_fetch).__aiter__() if presence_fetch else None
        )

        async def _next(it: AsyncIterator[Any]) -> Any:
            return await it.__anext__()

        activity_task = asyncio.create_task(_next(activity_iter), name="sse-activity")
        curator_task = asyncio.create_task(_next(curator_iter), name="sse-curator")
        presence_task: asyncio.Task[Any] | None = (
            asyncio.create_task(_next(presence_iter), name="sse-presence")
            if presence_iter is not None
            else None
        )
        heartbeat_task: asyncio.Task[None] = asyncio.create_task(
            asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS), name="sse-heartbeat"
        )
        try:
            while True:
                if await request.is_disconnected():
                    break
                tasks = {activity_task, curator_task, heartbeat_task}
                if presence_task is not None:
                    tasks.add(presence_task)
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if task is activity_task:
                        try:
                            event = task.result()
                        except StopAsyncIteration:
                            return
                        yield _sse_format("activity", event)
                        activity_task = asyncio.create_task(
                            _next(activity_iter), name="sse-activity"
                        )
                    elif task is curator_task:
                        try:
                            event = task.result()
                        except StopAsyncIteration:
                            return
                        yield _sse_format("curator", event)
                        curator_task = asyncio.create_task(
                            _next(curator_iter), name="sse-curator"
                        )
                    elif presence_task is not None and task is presence_task:
                        try:
                            event = task.result()
                        except StopAsyncIteration:
                            presence_task = None
                            continue
                        yield _sse_format("presence", event)
                        if presence_iter is not None:
                            presence_task = asyncio.create_task(
                                _next(presence_iter), name="sse-presence"
                            )
                    elif task is heartbeat_task:
                        yield _sse_format(
                            "heartbeat", {"ts": datetime.now(UTC).isoformat()}
                        )
                        heartbeat_task = asyncio.create_task(
                            asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS), name="sse-heartbeat"
                        )
        except asyncio.CancelledError:
            raise
        finally:
            for task in (activity_task, curator_task, heartbeat_task, presence_task):
                if task is not None:
                    task.cancel()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_source(), media_type="text/event-stream", headers=headers)
