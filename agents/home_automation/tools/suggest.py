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

    # Include existing automations so the LLM does not propose duplicates.
    existing_automations: list[str] = []
    try:
        automation_states = await client.list_states(domain="automation")
        existing_automations = [
            s.get("attributes", {}).get("friendly_name", s["entity_id"])
            for s in automation_states
        ]
    except Exception:
        pass  # Non-fatal — proceed without the existing automation context.

    existing_section = ""
    if existing_automations:
        existing_section = (
            "\nExisting automations (do NOT propose duplicates):\n"
            + "\n".join(f"- {name}" for name in existing_automations)
            + "\n"
        )

    prompt = f"""You are a Home Assistant automation expert.
Based on the following device activity in the last {window_hours} hours:

{history_summary}
{existing_section}
Suggest 2-3 useful Home Assistant automations as YAML. Reply ONLY with valid YAML, no prose."""

    llm = _get_llm()
    model = os.getenv("REASONER_MODEL", "qwen36-moe-32k")
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
