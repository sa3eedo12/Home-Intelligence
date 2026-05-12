from __future__ import annotations

import asyncio
import os

from home_agents_sdk.agent_base import build_app
from home_agents_sdk.bus import EventBus
from home_agents_sdk.telemetry import get_logger

from tools import chat, core  # noqa: F401

logger = get_logger("personal_assistant")
app = build_app("personal_assistant", manifest_path="manifest.yaml")


@app.on_event("startup")
async def _startup() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    bus = EventBus(redis_url)
    try:
        await bus.connect()
        asyncio.create_task(
            bus.subscribe("events.system", _on_event, group="personal_assistant:events")
        )
        logger.info("personal_assistant_subscribed")
    except Exception as exc:
        logger.warning("personal_assistant_bus_connect_failed", error=str(exc))


async def _on_event(payload: dict) -> None:
    logger.info("personal_assistant_event", event_type=payload.get("type", "unknown"))
