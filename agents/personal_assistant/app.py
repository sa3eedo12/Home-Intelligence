from __future__ import annotations

import asyncio
import os
from typing import Any

from home_agents_sdk.agent_base import build_app
from home_agents_sdk.bus import EventBus
from home_agents_sdk.telemetry import get_logger

from tools import chat, core, infer  # noqa: F401

logger = get_logger("personal_assistant")
app = build_app("personal_assistant", manifest_path="manifest.yaml")


def _state_value(state: Any) -> str:
    if isinstance(state, dict):
        return str(state.get("state", ""))
    return str(state or "")


def _presence_arrival(payload: dict[str, Any]) -> str | None:
    if payload.get("type") == "presence.changed":
        if str(payload.get("to") or payload.get("state") or "").lower() == "home":
            return str(payload.get("person") or payload.get("entity_id") or "someone")
        return None

    if payload.get("type") != "state_changed":
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    entity_id = str(payload.get("entity_id") or data.get("entity_id") or "")
    if not (entity_id.startswith("person.") or entity_id.startswith("device_tracker.")):
        return None
    old_state = _state_value(data.get("old_state")).lower()
    new_state = _state_value(data.get("new_state")).lower()
    if new_state == "home" and old_state != "home":
        return entity_id.split(".", 1)[-1].replace("_", " ").title()
    return None


@app.on_event("startup")
async def _startup() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    bus = EventBus(redis_url)
    try:
        await bus.connect()
        asyncio.create_task(
            bus.subscribe("events.system", _on_system_event, group="personal_assistant:system")
        )

        async def _handle_home_event(payload: dict[str, Any]) -> None:
            await _on_home_event(payload, bus)

        asyncio.create_task(
            bus.subscribe("events.home", _handle_home_event, group="personal_assistant:home")
        )
        logger.info("personal_assistant_subscribed")
    except Exception as exc:
        logger.warning("personal_assistant_bus_connect_failed", error=str(exc))


async def _on_system_event(payload: dict[str, Any]) -> None:
    logger.info("personal_assistant_system_event", event_type=payload.get("type", "unknown"))


async def _on_home_event(payload: dict[str, Any], bus: EventBus) -> None:
    arrived = _presence_arrival(payload)
    if not arrived:
        return

    reminders = await core.due_reminders(limit=3)
    if not reminders:
        logger.info("personal_assistant_presence_no_due_reminders", person=arrived)
        return

    labels = ", ".join(str(item.get("text")) for item in reminders if item.get("text"))
    await bus.publish(
        "notify.outbound",
        {
            "text": f"Welcome home, {arrived}. Due reminders: {labels}",
            "severity": "notice",
            "topic": "reminders.presence",
            "agent": "personal_assistant",
            "capability": "list_reminders",
            "fingerprint": "personal_assistant:presence_due_reminders:"
            + ",".join(str(item.get("id")) for item in reminders),
        },
    )
