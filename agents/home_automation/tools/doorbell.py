from __future__ import annotations

import base64
import os

from home_agents_sdk import vision
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client
from .notify_helper import publish_notification

_llm: OllamaClient | None = None


def _get_llm() -> OllamaClient:
    global _llm
    if _llm is None:
        _llm = OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434"))
    return _llm


@tool("doorbell.snapshot")
async def snapshot(entity_id: str | None = None) -> dict:
    entity_id = entity_id or os.getenv("DOORBELL_ENTITY_ID", "camera.front_door")
    client = get_ha_client()
    image_bytes = await client.get_camera_snapshot(entity_id)
    return {
        "entity_id": entity_id,
        "image_b64": base64.b64encode(image_bytes).decode(),
        "size_bytes": len(image_bytes),
    }


@tool("doorbell.summarize_event")
async def summarize_event(event_type: str, entity_id: str | None = None) -> dict:
    entity_id = entity_id or os.getenv("DOORBELL_ENTITY_ID", "camera.front_door")
    client = get_ha_client()

    try:
        image_bytes = await client.get_camera_snapshot(entity_id)
    except Exception:
        image_bytes = b""

    detections = await vision.detect_objects(
        image_bytes, classes=["person", "package", "car", "dog", "cat"]
    )

    detection_labels = [d.get("class", "unknown") for d in detections]
    if "person" in detection_labels:
        llm = _get_llm()
        prompt = (
            f"Doorbell {event_type} event. Detected: {', '.join(detection_labels)}. "
            "Write a one-sentence summary for a homeowner notification."
        )
        resp = await llm.chat(
            [{"role": "user", "content": prompt}],
            model=os.getenv("DEFAULT_MODEL", "qwen3-8b-8k"),
        )
        summary = resp.get("message", {}).get(
            "content", f"Visitor detected at doorbell ({event_type})."
        )
    else:
        detected_str = ", ".join(detection_labels) or "nothing notable"
        summary = f"Doorbell {event_type} event. Detected: {detected_str}."

    severity = "high" if "person" in detection_labels else "info"

    await publish_notification(
        f"🔔 {summary}",
        severity=severity,
        topic="doorbell.event",
        capability="doorbell.summarize_event",
        event_type=event_type,
        entity_id=entity_id,
    )

    return {"detections": detections, "summary": summary, "entity_id": entity_id}


@tool("doorbell.last_visitor")
async def last_visitor(hours: int) -> dict:
    summary = f"No doorbell events found in the last {hours} hour(s) in the local log."
    return {"hours": hours, "summary": summary, "events": []}
