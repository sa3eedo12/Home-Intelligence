"""In-memory aggregator for the events.activity Redis stream.

Subscribes once at orchestrator startup and keeps a rolling window of the
most recent activity events per agent. The dashboard reads the snapshot
synchronously; the SSE endpoint also streams individual events live.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

logger = get_logger("orchestrator.activity")

ACTIVITY_STREAM = "events.activity"
WINDOW_MINUTES = 5
MAX_EVENTS_PER_AGENT = 200


class ActivityAggregator:
    """Maintains per-agent rolling activity state for the dashboard."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=MAX_EVENTS_PER_AGENT)
        )
        self._current: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        # Backfill the last window so the dashboard isn't empty on first load.
        try:
            cutoff_ms = int(
                (datetime.now(UTC) - timedelta(minutes=WINDOW_MINUTES)).timestamp() * 1000
            )
            entries = await self._redis.xrange(ACTIVITY_STREAM, min=f"{cutoff_ms}-0", count=2000)
            for _msg_id, fields in entries:
                self._ingest_fields(fields)
        except Exception as exc:
            logger.warning("activity_backfill_failed", error=str(exc))
        self._task = asyncio.create_task(self._run(), name="activity-aggregator")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def snapshot(self) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(minutes=WINDOW_MINUTES)
        per_agent: list[dict[str, Any]] = []
        for agent, events in sorted(self._events.items()):
            kept = [e for e in events if _parse_ts(e.get("ts")) >= cutoff]
            ok = sum(1 for e in kept if e.get("status") == "ok")
            errors = sum(1 for e in kept if e.get("status") == "error")
            durations = [
                float(e.get("duration_ms") or 0.0) for e in kept if e.get("status") == "ok"
            ]
            avg_ms = round(sum(durations) / len(durations), 1) if durations else 0.0
            sparkline = _bucket_per_minute(kept, WINDOW_MINUTES)
            current = self._current.get(agent)
            state = "idle"
            if current and current.get("status") == "started":
                if (time.time() - current.get("_seen_at", 0)) < 30:
                    state = "working"
            if errors and (not current or current.get("status") != "started"):
                state = "error"
            per_agent.append(
                {
                    "agent": agent,
                    "state": state,
                    "current": current,
                    "ok": ok,
                    "errors": errors,
                    "avg_ms": avg_ms,
                    "sparkline": sparkline,
                    "last_event": kept[-1] if kept else None,
                }
            )
        return {
            "window_minutes": WINDOW_MINUTES,
            "agents": per_agent,
            "total_events": sum(len(d) for d in self._events.values()),
        }

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for events in self._events.values():
            merged.extend(events)
        merged.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return merged[:limit]

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._subscribers.discard(queue)

    def _ingest_fields(self, fields: dict[str, Any]) -> None:
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except (TypeError, ValueError):
            return
        agent = payload.get("agent")
        if not isinstance(agent, str):
            return
        self._events[agent].append(payload)
        status = payload.get("status")
        if status == "started":
            payload_with_seen = dict(payload)
            payload_with_seen["_seen_at"] = time.time()
            self._current[agent] = payload_with_seen
        elif status in {"ok", "error"}:
            self._current.pop(agent, None)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # Drop oldest event for slow consumers rather than block ingestion.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def _run(self) -> None:
        last_id = "$"
        backoff = 1.0
        while not self._stopping:
            try:
                messages = await self._redis.xread(
                    {ACTIVITY_STREAM: last_id}, count=50, block=2000
                )
                backoff = 1.0
                for _stream, entries in messages or []:
                    for msg_id, fields in entries:
                        last_id = msg_id
                        self._ingest_fields(fields)
                        try:
                            payload = json.loads(fields.get("payload", "{}"))
                        except (TypeError, ValueError):
                            continue
                        self._broadcast(payload)
            except asyncio.CancelledError:
                raise
            except ResponseError as exc:
                logger.warning("activity_stream_response_error", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as exc:
                logger.warning("activity_loop_error", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def _parse_ts(raw: Any) -> datetime:
    if not isinstance(raw, str):
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _bucket_per_minute(events: list[dict[str, Any]], window_minutes: int) -> list[int]:
    """Return per-minute counts (oldest -> newest) over the last `window_minutes`."""
    buckets = [0] * window_minutes
    now = datetime.now(UTC)
    for event in events:
        ts = _parse_ts(event.get("ts"))
        delta_minutes = int((now - ts).total_seconds() // 60)
        if 0 <= delta_minutes < window_minutes:
            buckets[window_minutes - 1 - delta_minutes] += 1
    return buckets
