from __future__ import annotations

import json
import os
from typing import Any

from home_agents_sdk import tool
from home_agents_sdk.event_log import EventLogStore
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.telemetry import get_logger

from tools.core import _pool

logger = get_logger("personal_assistant.infer")

SYSTEM_PROMPT = """You infer possible episodic memories for a local-first home assistant.
Be conservative. Use only the user's context and recent event log evidence.
Return exactly one compact JSON object and no prose:
{
  "inference": "one concise inferred fact, or empty string",
  "confidence": 0.0,
  "clarifying_question": "question to ask when evidence is weak, or empty string",
  "proposed_action": {
    "agent": "knowledge_notes",
    "capability": "record_event",
    "payload": {
      "agent": "personal_assistant",
      "capability": "inferred_event",
      "summary": "short memory to log after confirmation",
      "payload": {"source": "infer"}
    }
  }
}
Rules:
- Propose ONE inference only.
- Set confidence below 0.6 unless recent events strongly support the inference.
- If confidence is below 0.6, leave inference empty, set proposed_action to null,
  and ask a clarifying question.
- Any proposed action must be safe and must only log a memory; do not control devices.
"""


def _llm() -> OllamaClient:
    return OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434"))


def _model() -> str:
    return os.getenv("DEFAULT_MODEL", "qwen3-8b-8k")


async def _event_store() -> EventLogStore:
    return EventLogStore(pool=await _pool())


def _clarify(question: str, confidence: float = 0.0) -> dict[str, Any]:
    return {
        "inference": "",
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "needs_confirmation": False,
        "clarifying_question": question,
        "proposed_action": None,
    }


def _extract_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _normalize_action(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    agent = value.get("agent")
    capability = value.get("capability")
    payload = value.get("payload")
    if (
        not isinstance(agent, str)
        or not isinstance(capability, str)
        or not isinstance(payload, dict)
    ):
        return None
    return {"agent": agent, "capability": capability, "payload": payload}


@tool("infer")
async def infer(context: str) -> dict[str, Any]:
    if not context.strip():
        return _clarify("What would you like me to infer or log?")

    try:
        store = await _event_store()
        recent = await store.recall_recent(window_minutes=24 * 60)
        events = recent.get("items", [])[:50]
    except Exception as exc:
        logger.warning("infer_recall_failed", error=str(exc))
        events = []
    compact_events = json.dumps(events, ensure_ascii=False, default=str)
    if len(compact_events) > 6000:
        compact_events = compact_events[:6000] + "…"

    user_prompt = (
        f"User context message:\n{context}\n\n"
        f"Recent events from the last 24h (JSON, newest first):\n{compact_events}\n\n"
        "Infer whether there is one memory worth logging after user confirmation."
    )
    try:
        response = await _llm().chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=_model(),
            temperature=0.1,
            response_format="json",
            # Reactive path: this runs on every observer event. Skip the
            # thinking trace — the structured JSON output is what matters,
            # and a 35B model emitting <thinking>…</thinking> first added
            # ~60-90s of latency that blocked the next reactive trigger.
            think=False,
        )
        parsed = _extract_json(str((response.get("message") or {}).get("content") or "{}"))
    except Exception as exc:
        logger.warning("infer_llm_failed", error=str(exc))
        return _clarify("I need a bit more context before I can infer that safely.")

    try:
        confidence = max(0.0, min(float(parsed.get("confidence") or 0.0), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    action = _normalize_action(parsed.get("proposed_action"))
    inference = str(parsed.get("inference") or "").strip()

    if confidence < 0.6 or not inference or action is None:
        question = str(
            parsed.get("clarifying_question") or "Can you confirm what happened?"
        ).strip()
        return _clarify(question, confidence)

    return {
        "inference": inference,
        "confidence": confidence,
        "needs_confirmation": True,
        "clarifying_question": "",
        "proposed_action": action,
    }
