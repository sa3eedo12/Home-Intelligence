"""Conversational fallback capability.

Routes "hi", "thanks", "how are you?", and general questions that don't map
to any structured tool. Uses the small fast Ollama model so latency is
~50–150 ms on the iGPU.
"""

from __future__ import annotations

import os
import re
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
    "Never invent facts about their home. "
    "CRITICAL: This is a conversation-only tool — you have NO ability to "
    "actually control devices, change settings, run automations, query "
    "sensors, or perform any home action. If the user asks you to DO "
    "something (turn off lights, change temperature, check status, etc.), "
    "do NOT pretend you tried. Do NOT invent error messages claiming the "
    "device failed. Say honestly: 'I couldn't route that to a tool — try "
    "rephrasing it more directly, like \"turn off bedroom lights\" or "
    "\"what's the bedroom temperature\".' Never fabricate execution or "
    "failure narratives for actions you did not perform."
)

# Hard-coded refusal for action-verb requests that reach the chat tool.
# Belt-and-braces alongside the system prompt: even if the LLM ignores
# the prompt instruction, this regex check short-circuits before any
# generation can fabricate. The router has already recorded a gap for
# this case (chat_fallback_for_action_verb), so the user just needs an
# honest reply.
#
# Kept in sync with router._ACTION_VERB_PATTERNS — duplicated rather
# than imported to avoid the agent depending on the orchestrator.
_ACTION_VERB_GUARD = re.compile(
    r"\b("
    r"turn\s+(on|off)|switch\s+(on|off)|toggle|"
    r"reduce|increase|raise|lower|set|adjust|change|dim|brighten|cool|heat|warm|"
    r"open|close|shut|lock|unlock|"
    r"play|pause|stop|resume|skip|mute|unmute|"
    r"start|begin|run|trigger|cancel|abort|schedule|remind"
    r")\b",
    re.IGNORECASE,
)


def _is_action_verb(text: str) -> bool:
    return bool(text) and bool(_ACTION_VERB_GUARD.search(text))


_HONEST_REFUSAL = (
    "I couldn't find a tool that does that. I won't pretend I tried — "
    "I've logged it so it can be added. Try rephrasing more directly "
    "(e.g., 'turn off bedroom lights', 'set bedroom AC to 22'), or "
    "ask me to check the dashboard for what I can already do."
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
    # Guard: if the router fell through to chat for an action-verb
    # request, refuse to fabricate. The router has already recorded a
    # capability_gap row for this and the user deserves the truth.
    if _is_action_verb(text):
        logger.info(
            "chat_refused_action_verb",
            text_preview=text[:120],
        )
        return {
            "reply": _HONEST_REFUSAL,
            "already_natural": True,
            "refused_action_verb": True,
        }

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
