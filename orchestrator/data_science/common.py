from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from home_agents_sdk.event_log import EventLogStore
from home_agents_sdk.telemetry import get_logger

logger = get_logger("orchestrator.data_science")


def jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def decode_json(value: Any, default: Any | None = None) -> Any:
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {"raw": value}
    return {"raw": str(value)}


def format_ts(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def command_count(status: Any) -> int:
    if not status:
        return 0
    try:
        return int(str(status).rsplit(" ", maxsplit=1)[-1])
    except (TypeError, ValueError):
        return 0


def current_embedding_model(embedder: Any) -> str:
    for attr in ("current_model", "model", "npu_model"):
        value = getattr(embedder, attr, None)
        if value:
            return str(value)
    return "unknown"


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class SingleFlightJob:
    def __init__(
        self,
        *,
        job_name: str,
        pool: Any | None = None,
        event_log_store: Any | None = None,
    ) -> None:
        self.job_name = job_name
        self.pool = pool
        self.event_log_store = event_log_store
        self._lock = asyncio.Lock()

    async def _run_singleflight(
        self,
        work: Callable[[], Awaitable[dict[str, Any]]],
        *,
        job_name: str | None = None,
    ) -> dict[str, Any]:
        active_job_name = job_name or self.job_name
        if self._lock.locked():
            return {"status": "already_running"}

        started = time.perf_counter()
        logger.info("data_science_job_started", job=active_job_name)
        async with self._lock:
            try:
                result = await work()
            except Exception as exc:  # noqa: BLE001
                logger.warning("data_science_job_failed", job=active_job_name, error=str(exc))
                result = {"status": "failed", "error": str(exc)}

            duration = round(time.perf_counter() - started, 3)
            result.setdefault("duration_seconds", duration)
            logger.info(
                "data_science_job_finished",
                job=active_job_name,
                duration_seconds=duration,
                status=result.get("status", "ok"),
            )
            await self._record_result(result, job_name=active_job_name)
            return result

    async def _record_result(self, result: dict[str, Any], *, job_name: str | None = None) -> None:
        active_job_name = job_name or self.job_name
        summary = f"data_science.{active_job_name} status={result.get('status', 'ok')}"
        try:
            recorder = self.event_log_store
            if recorder is None and self.pool is not None:
                recorder = EventLogStore(pool=self.pool)
            record_event = getattr(recorder, "record_event", None)
            if not callable(record_event):
                return
            await record_event(
                agent="data_science",
                capability=active_job_name,
                summary=summary,
                payload=jsonable(result),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "data_science_event_record_failed",
                job=self.job_name,
                error=str(exc),
            )
