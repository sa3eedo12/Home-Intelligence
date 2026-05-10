from __future__ import annotations

import os

import yaml
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

_llm: OllamaClient | None = None


def _get_llm() -> OllamaClient:
    global _llm
    if _llm is None:
        _llm = OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434"))
    return _llm


@tool("suggest_automation")
async def suggest_automation(window_hours: int = 24) -> dict:
    client = get_ha_client()
    states = await client.list_states()
    entity_ids = [s["entity_id"] for s in states[:10]]

    history = await client.get_history(entity_ids, hours=window_hours)

    summary_lines = []
    for entity_data in history[:10]:
        if entity_data:
            eid = entity_data[0].get("entity_id", "unknown")
            changes = len(entity_data)
            summary_lines.append(f"{eid}: {changes} state changes")

    history_summary = "\n".join(summary_lines) or "No significant activity."

    prompt = f"""You are a Home Assistant automation expert.
Based on the following device activity in the last {window_hours} hours:

{history_summary}

Suggest 2-3 useful Home Assistant automations as YAML. Reply ONLY with valid YAML, no prose."""

    llm = _get_llm()
    model = os.getenv("REASONER_MODEL", "qwen3.6:35b-a3b")
    resp = await llm.chat(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        response_format="yaml",
    )

    raw = resp.get("message", {}).get("content", "")

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        parsed = None

    return {"proposals_yaml": raw, "parsed": parsed, "valid": parsed is not None}
