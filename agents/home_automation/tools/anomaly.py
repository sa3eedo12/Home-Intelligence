from __future__ import annotations

from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

try:
    import numpy as np  # noqa: F401

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


@tool("anomaly.scan")
async def anomaly_scan(window_hours: int = 24) -> list[dict]:
    client = get_ha_client()
    states = await client.list_states()
    entity_ids = [s["entity_id"] for s in states[:20]]

    if not entity_ids:
        return []

    history = await client.get_history(entity_ids, hours=window_hours)

    anomalies = []
    for entity_history in history:
        if not entity_history:
            continue
        entity_id = entity_history[0].get("entity_id", "unknown")
        change_count = len(entity_history)
        if change_count > 50:
            anomalies.append(
                {
                    "entity_id": entity_id,
                    "change_count": change_count,
                    "window_hours": window_hours,
                    "score": float(change_count / 50),
                }
            )

    return anomalies
