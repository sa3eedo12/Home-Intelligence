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
    "You are Home Intelligence — a passive observation + analysis "
    "assistant. You watch HealthKit data, Home Assistant events, "
    "and appliance cycles, then surface sleep coaching, anomaly "
    "detection, routine inference, and proactive nudges. "
    "Answer in 1-3 short sentences. Be warm for greetings ('hi', "
    "'thanks'). Answer general knowledge questions briefly. "
    "CRITICAL: You do NOT control devices. For turning lights / "
    "thermostats / locks / curtains on or off, the user should use "
    "Siri, the Home app, or Home Assistant directly — those are "
    "faster and authoritative. If the user asks you to DO a device "
    "action (turn on/off, change temperature, lock/unlock), reply "
    "honestly: 'I'm built for analysis, not control — try Siri, the "
    "Home app, or Home Assistant for that.' Never pretend you tried. "
    "Never fabricate device state. "
    "For analysis questions ('how did I sleep this week?', 'what "
    "appliances ran today?', 'show me pending proposals'), answer "
    "from real data if you have a tool. If you don't have a tool, "
    "say so honestly so the gap can be logged."
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

# Device-query guard: catches questions about specific home devices,
# vehicles, environmental readings that the LLM might fabricate answers
# for. Examples that fired this bug live:
#   "what's the battery percentage of my car?"
#   "is the bedroom door locked?"
# The LLM, given no real data, invents "make sure your car is powered
# on" or claims device states. Refuse + log so the system records the
# gap and the user gets the truth.
#
# Match logic: question word + home/device noun in the same text.
# Pure chit-chat ("how are you doing?") has the question word but no
# device noun, so it passes through unmolested.
_QUESTION_GUARD = re.compile(
    r"\b(what(?:'?s)?|where(?:'?s)?|which|how|when|is|are|has|have|does|did|do)\b",
    re.IGNORECASE,
)
_DEVICE_NOUN_GUARD = re.compile(
    r"\b("
    # vehicles
    r"car|cars|vehicle|truck|bike|scooter|battery|ev|charger|charging|"
    # rooms
    r"bedroom|kitchen|living\s+room|office|bathroom|garage|hallway|entryway|"
    # devices
    r"light|lights|lamp|bulb|"
    r"thermostat|temperature|ac|heating|hvac|fan|"
    r"blind|blinds|curtain|curtains|shade|shades|shutter|"
    r"door|doors|lock|locks|"
    r"tv|television|speaker|music|media|player|"
    r"camera|doorbell|"
    r"sensor|motion|presence|"
    r"washer|dryer|dishwasher|oven|fridge|microwave|appliance|"
    r"vacuum|roomba|"
    r"sprinkler|water|irrigation|"
    r"alarm|security|"
    r"humidity|air\s+quality|co2|brightness|"
    r"status|state|level|percentage|setting|mode|reading"
    r")\b",
    re.IGNORECASE,
)


def _is_action_verb(text: str) -> bool:
    return bool(text) and bool(_ACTION_VERB_GUARD.search(text))


def _is_device_query(text: str) -> bool:
    """True if the text is a question about a specific home device or
    reading. We refuse these to prevent fabrication."""
    if not text:
        return False
    return bool(_QUESTION_GUARD.search(text)) and bool(_DEVICE_NOUN_GUARD.search(text))


_HONEST_REFUSAL = (
    "I'm a passive observation + analysis assistant, not a device "
    "controller. For turning things on/off or checking device state, "
    "ask Siri, use the Home app, or Home Assistant directly — they're "
    "faster and they're the system of record for what your devices "
    "actually did. I focus on sleep coaching, appliance memory, "
    "nightly insights, and proactive nudges. Try asking 'what did my "
    "sleep look like this week?' or 'what proposals do I have pending?'."
)

_HONEST_AMBIGUOUS_REFUSAL = (
    "I can't tell what you're referring to — I don't keep our chat "
    "history yet, so a short follow-up like \"it's not on\" is missing "
    "the device context. Could you rephrase with the device name (e.g. "
    "\"the office light is still off\", \"my car battery is low\")?"
)

# Detects ambiguous-pronoun follow-ups like "it's not on", "still on",
# "didn't work", "not yet", "still nothing" — short messages whose
# referent depends on conversation history we don't have. Without this
# guard, the chat LLM happily invents context (e.g. yesterday's "it's
# not on" → "The TV status couldn't be confirmed...").
_AMBIGUOUS_PRONOUN_PATTERN = re.compile(
    r"^\s*("
    r"it(?:'?s|s)?\b"
    r"|that(?:'?s|s)?\b"
    r"|they(?:'?re|re)?\b"
    r"|still\b"
    r"|nope?\b"
    r"|nothing(?:'?s)?\b"
    r"|didn'?t\s+work\b"
    r"|not\s+yet\b"
    r"|same\s+thing\b"
    r"|no\s+change\b"
    r"|won'?t\s+turn\b"
    r")",
    re.IGNORECASE,
)


def _looks_ambiguous_followup(text: str) -> bool:
    """True if the text is short AND opens with a pronoun/follow-up
    phrase whose referent depends on prior conversation. Pure chit-chat
    that happens to contain "it" later in the sentence ("how is it
    going?") passes through because the regex only matches at the start.
    """
    if not text:
        return False
    stripped = text.strip()
    # Long messages probably establish their own context, even if they
    # contain a pronoun.
    if len(stripped) > 120:
        return False
    return bool(_AMBIGUOUS_PRONOUN_PATTERN.match(stripped))

_HONEST_QUERY_REFUSAL = (
    "Direct device queries are usually faster via the Home app or "
    "Home Assistant — they have the authoritative state. I'm built "
    "for multi-source analysis (sleep trends, appliance history, "
    "anomaly patterns, proactive routines). Your question is logged "
    "so I can flag it for follow-up; try asking about trends or "
    "patterns instead."
)


def _llm() -> OllamaClient:
    return OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434"))


def _chat_model() -> str:
    # Prefer the small fast model for chat; fall back to the default chat model.
    return (
        os.getenv("CHAT_MODEL")
        or os.getenv("ROUTER_MODEL")
        or os.getenv("DEFAULT_MODEL")
        or "qwen3-8b-16k"
    )


@tool("chat")
async def chat(text: str) -> dict[str, Any]:
    """Conversational fallback. Returns a natural-language reply directly."""
    # Guard 1: device-status queries — refuse to fabricate state readings.
    # Checked BEFORE action-verb because words like "warm" and "cool"
    # appear in both lists but a question word + device noun unambiguously
    # signals a query ("how warm is the office?") not an action.
    if _is_device_query(text):
        logger.info(
            "chat_refused_device_query",
            text_preview=text[:120],
        )
        return {
            "reply": _HONEST_QUERY_REFUSAL,
            "already_natural": True,
            "refused_device_query": True,
        }

    # Guard 2: action-verb requests — refuse to fabricate execution.
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

    # Guard 3: ambiguous-pronoun follow-ups ("it's not on", "still on",
    # "didn't work") — without conversation history, the LLM invents
    # what "it" refers to. Live evidence: user said "it's not on" about
    # an office light, the LLM replied about the TV (2026-05-20).
    if _looks_ambiguous_followup(text):
        logger.info(
            "chat_refused_ambiguous_followup",
            text_preview=text[:120],
        )
        return {
            "reply": _HONEST_AMBIGUOUS_REFUSAL,
            "already_natural": True,
            "refused_ambiguous_followup": True,
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
