from __future__ import annotations

import asyncio
import os

from home_agents_sdk.agent_base import build_app
from home_agents_sdk.bus import EventBus
from home_agents_sdk.telemetry import get_logger

from tools import core  # noqa: F401

logger = get_logger("system_health")
app = build_app("system_health", manifest_path="manifest.yaml")


@app.on_event("startup")
async def _startup() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    bus = EventBus(redis_url)
    try:
        await bus.connect()
        asyncio.create_task(bus.subscribe("events.system", _on_event, group="system_health:events"))
        logger.info("system_health_subscribed")
    except Exception as exc:
        logger.warning("system_health_bus_connect_failed", error=str(exc))


async def _on_event(payload: dict) -> None:
    logger.info("system_health_event", event_type=payload.get("type", "unknown"))
