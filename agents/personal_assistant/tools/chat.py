"""Conversational fallback capability.

Routes "hi", "thanks", "how are you?", and general questions that don't map
to any structured tool. Uses the small fast Ollama model so latency is
~50–150 ms on the iGPU.
"""

from __future__ import annotations

import os
from typing import Any

from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

logger = get_logger("personal_assistant.chat")

CHAT_SYSTEM = (
    "You are Home Intelligence — a friendly, concise home AI assistant. "
    "Answer in 1-3 short sentences. If the user is just being social ('hi', "
    "'thanks', 'good morning'), respond warmly. If they ask a general question "
    "you can answer from common knowledge, do so briefly. If they ask about "
    "their home (lights, sensors, washer, etc.) and you don't have that data, "
    "say you can fetch it but need them to be specific (e.g., 'which room?'). "
    "Never invent facts about their home."
)


def _llm() -> OllamaClient:
    return OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434"))


def _chat_model() -> str:
    # Prefer the small fast model for chat; fall back to the default chat model.
    return (
        os.getenv("CHAT_MODEL")
        or os.getenv("ROUTER_MODEL")
        or os.getenv("DEFAULT_MODEL")
        or "qwen3:8b"
    )


@tool("chat")
async def chat(text: str) -> dict[str, Any]:
    """Conversational fallback. Returns a natural-language reply directly."""
    client = _llm()
    try:
        resp = await client.chat(
            messages=[
                {"role": "system", "content": CHAT_SYSTEM},
                {"role": "user", "content": text},
            ],
            model=_chat_model(),
            temperature=0.6,
        )
    except Exception as exc:
        logger.warning("chat_llm_failed", error=str(exc))
        return {"reply": "I'm having trouble reaching the local LLM right now."}

    content = (resp.get("message") or {}).get("content")
    if not content:
        return {"reply": "I'm not sure how to respond to that."}
    return {"reply": str(content).strip(), "already_natural": True}
