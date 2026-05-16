from __future__ import annotations

import asyncio
import os

from home_agents_sdk.agent_base import build_app
from home_agents_sdk.bus import EventBus
from home_agents_sdk.telemetry import get_logger

from tools import (
    anomaly,
    appliance,
    area,
    climate,
    core,
    doorbell,
    ha_mcp_client,
    lights_control,
    scenes,
    suggest,
)

_TOOL_MODULES = (
    anomaly,
    appliance,
    area,
    climate,
    core,
    doorbell,
    ha_mcp_client,
    lights_control,
    scenes,
    suggest,
)

logger = get_logger("home_automation")
app = build_app("home_automation", manifest_path="manifest.yaml")


@app.on_event("startup")
async def _startup() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    bus = EventBus(redis_url)
    try:
        await bus.connect()
        asyncio.create_task(
            bus.subscribe("events.home", _on_home_event, group="home_automation:events")
        )
        logger.info("home_automation_subscribed_events_home")
    except Exception as exc:
        logger.warning("home_automation_bus_connect_failed", error=str(exc))


async def _on_home_event(payload: dict) -> None:
    event_type = payload.get("type", "")
    if event_type.startswith("doorbell_"):
        try:
            result = await doorbell.summarize_event(
                event_type=event_type,
                entity_id=payload.get("entity_id"),
            )
            logger.info("doorbell_event_processed", summary=result.get("summary"))
        except Exception as exc:
            logger.warning("doorbell_event_failed", error=str(exc))
