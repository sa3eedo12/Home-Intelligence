from __future__ import annotations

import asyncio
import os
import time

from home_agents_sdk.agent_base import build_app
from home_agents_sdk.bus import EventBus
from home_agents_sdk.telemetry import get_logger

from tools import core

logger = get_logger("dashboard_curator")
app = build_app("dashboard_curator", manifest_path="manifest.yaml")

_activity_summary_task: asyncio.Task[None] | None = None
_last_activity_summary_at = 0.0


def _activity_debounce_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("DASHBOARD_ACTIVITY_DEBOUNCE_SECONDS", "30")))
    except ValueError:
        return 30.0


def _activity_window_minutes() -> int:
    try:
        return max(1, int(os.getenv("DASHBOARD_ACTIVITY_WINDOW_MINUTES", "15")))
    except ValueError:
        return 15


@app.on_event("startup")
async def _startup() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    bus = EventBus(redis_url)
    try:
        await bus.connect()
        asyncio.create_task(
            bus.subscribe("events.activity", _on_activity_event, group="dashboard_curator:activity")
        )
        logger.info("dashboard_curator_subscribed_events_activity")
    except Exception as exc:
        logger.warning("dashboard_curator_bus_connect_failed", error=str(exc))


async def _on_activity_event(_payload: dict) -> None:
    global _activity_summary_task
    if _activity_summary_task is not None and not _activity_summary_task.done():
        return
    _activity_summary_task = asyncio.create_task(_run_debounced_summary())


async def _run_debounced_summary() -> None:
    global _last_activity_summary_at
    elapsed = time.monotonic() - _last_activity_summary_at
    delay = max(0.0, _activity_debounce_seconds() - elapsed)
    if delay:
        await asyncio.sleep(delay)

    _last_activity_summary_at = time.monotonic()
    try:
        result = await core.summarize_activity(window_minutes=_activity_window_minutes())
        stats = result.get("stats", {}) if isinstance(result, dict) else {}
        logger.info("dashboard_activity_summarized", total_events=stats.get("total_events"))
    except Exception as exc:
        logger.warning("dashboard_activity_summary_failed", error=str(exc))
