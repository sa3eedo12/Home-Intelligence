from __future__ import annotations

import json
import re
from typing import Any

from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.npu import NPUClient, NPUUnavailable
from home_agents_sdk.telemetry import get_logger

from .registry import CapabilityRegistry

logger = get_logger("router")

SYSTEM_PROMPT = """You are a strict JSON router for a single-user home automation assistant.
You MUST pick `agent` and `capability` from the EXACT list of capabilities the user message
provides. Do NOT invent capability names. If nothing in the list fits, return null for both.
Reply ONLY with compact JSON matching this schema (no prose, no code fences):
{
 "agent": "<exact agent id from the list, or null>",
 "capability": "<exact capability id from the list, or null>",
 "inputs": { "<param>": <value>, ... },
 "needs_confirmation": <bool>,
 "reason": "<one short sentence>"
}"""

MIN_SEMANTIC_SCORE = 0.55

# Fast path: short conversational greetings/acks skip the LLM classifier and
# embedding-based fallback entirely, routing straight to personal_assistant.chat.
# Saves ~1-2s on every greeting/ack vs. a full classify + semantic search round.
_FAST_PATH_PATTERNS = [
    re.compile(r"^\s*(hi|hello|hey|howdy|yo|sup|hiya|hi there)\b[\s.!?]*$", re.IGNORECASE),
    re.compile(
        r"^\s*good\s+(morning|afternoon|evening|night)\b[\s.!?]*$", re.IGNORECASE
    ),
    re.compile(
        r"^\s*(thanks|thank you|ty|thx|cheers|appreciate it)\b[\s.!?]*$", re.IGNORECASE
    ),
    re.compile(r"^\s*(ok|okay|cool|nice|great|awesome|got it|alright)\b[\s.!?]*$", re.IGNORECASE),
    re.compile(r"^\s*(bye|goodbye|see ya|good night|gn)\b[\s.!?]*$", re.IGNORECASE),
    re.compile(r"^\s*(how are you|how's it going|what's up|wassup)\b[\s.?]*$", re.IGNORECASE),
]


def _is_conversational_shortcut(text: str) -> bool:
    return any(p.match(text) for p in _FAST_PATH_PATTERNS)


def _format_capability_inventory(caps: list[dict[str, Any]]) -> str:
    """Format the registry's capability list into a compact prompt section."""
    if not caps:
        return "(no capabilities registered)"
    lines = []
    for cap in caps:
        line = f"- {cap['agent']}.{cap['id']}: {cap.get('description', '')}"
        inputs = cap.get("inputs")
        if inputs:
            line += f" | inputs: {json.dumps(inputs, ensure_ascii=False)}"
        lines.append(line)
    return "\n".join(lines)


class Router:
    def __init__(
        self,
        npu: NPUClient,
        registry: CapabilityRegistry,
        router_model: str,
        llm: OllamaClient | None = None,
        llm_fallback_model: str | None = None,
        humanizer_model: str | None = None,
    ) -> None:
        self._npu = npu
        self._registry = registry
        self._router_model = router_model
        self._llm = llm
        self._llm_fallback_model = llm_fallback_model
        self._humanizer_model = humanizer_model or llm_fallback_model or router_model

    async def handle(self, text: str, user_id: str) -> dict[str, Any]:
        # Fast path: short conversational greetings/acks go straight to
        # personal_assistant.chat without calling the LLM classifier or the
        # semantic-search fallback. Saves ~1-2s per greeting.
        if _is_conversational_shortcut(text) and (
            self._registry.get_capability("personal_assistant", "chat") is not None
        ):
            try:
                result = await self._registry.dispatch(
                    "personal_assistant", "chat", {"text": text}
                )
                reply_text = await self._humanize(
                    text, "personal_assistant", "chat", result
                )
                return {"reply": reply_text}
            except Exception as exc:
                logger.warning("router_fast_path_failed", error=str(exc))
                # Fall through to the normal path if chat dispatch fails.

        classification = await self._classify(text)
        agent = classification.get("agent")
        capability = classification.get("capability")
        inputs = classification.get("inputs", {})
        needs_confirmation = classification.get("needs_confirmation", False)
        reason = classification.get("reason", "")

        # Validate the LLM's pick exists. If not, fall through to semantic search.
        if agent and capability and self._registry.get_capability(agent, capability) is None:
            logger.info(
                "router_classify_invalid_capability",
                agent=agent,
                capability=capability,
            )
            agent = None
            capability = None

        if agent is None or capability is None:
            fallback = await self._semantic_fallback(text)
            if fallback is not None:
                agent = fallback["agent"]
                capability = fallback["capability"]
            elif self._registry.get_capability("personal_assistant", "chat") is not None:
                # Conversational catch-all: smalltalk, greetings, general questions.
                agent = "personal_assistant"
                capability = "chat"
                inputs = {"text": text}
            else:
                return {"reply": "I don't have a capability for that yet."}

        cap_meta = self._registry.get_capability(agent, capability)
        if cap_meta and cap_meta.get("require_confirmation"):
            needs_confirmation = True

        # Guarantee personal_assistant.chat always receives the user text,
        # even when the LLM classifier forgets to populate `inputs.text`.
        if agent == "personal_assistant" and capability == "chat":
            inputs = {"text": text}

        if needs_confirmation:
            return {
                "reply": f"I need your confirmation: {reason}",
                "confirm": {
                    "agent": agent,
                    "capability": capability,
                    "inputs": inputs,
                    "reason": reason,
                    "workflow_id": None,
                },
            }

        try:
            result = await self._registry.dispatch(agent, capability, inputs)
        except Exception as exc:
            logger.warning("router_dispatch_failed", error=str(exc))
            return {"reply": f"Error dispatching request: {exc}"}

        # Humanize the result unless the agent already returned natural text.
        reply_text = await self._humanize(text, agent, capability, result)
        return {"reply": reply_text}

    async def _classify(self, text: str) -> dict[str, Any]:
        capabilities = self._registry.list_capabilities()
        agents = sorted({cap["agent"] for cap in capabilities})
        agent_list = ", ".join(agents) if agents else "none"
        capability_block = _format_capability_inventory(capabilities)
        user_msg = (
            f"Available agents: {agent_list}\n\n"
            f"Available capabilities (use these EXACT ids):\n{capability_block}\n\n"
            f"User request: {text}"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        # Try the NPU-served router model first (small, fast, INT-quantized).
        try:
            response = await self._npu.chat(model=self._router_model, messages=messages)
            content = response["choices"][0]["message"]["content"]
            return json.loads(content)
        except NPUUnavailable as exc:
            logger.info("router_npu_unavailable_falling_back_to_ollama", error=str(exc))
        except Exception as exc:
            logger.warning("router_classify_npu_failed", error=str(exc))

        # Fallback to Ollama on the iGPU using the same router model name (or
        # an explicit override). This is what lets the stack work on hosts
        # where the NPU is unavailable (e.g. TrueNAS today, where the
        # `lemonade` service is a CPU stub that doesn't speak chat).
        if self._llm is None:
            return _empty_classification()
        ollama_model = self._llm_fallback_model or self._router_model
        try:
            response = await self._llm.chat(
                messages=messages,
                model=ollama_model,
                response_format="json",
            )
            content = (response.get("message") or {}).get("content", "")
            if not content:
                logger.warning("router_classify_ollama_empty_content")
                return _empty_classification()
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("router_classify_ollama_bad_json", error=str(exc))
            return _empty_classification()
        except Exception as exc:
            logger.warning("router_classify_ollama_failed", error=str(exc))
            return _empty_classification()

    async def dispatch(self, agent: str, capability: str, inputs: dict) -> dict:
        """Public proxy to registry dispatch, used by confirmation callbacks."""
        return await self._registry.dispatch(agent, capability, inputs)

    async def execute_pending(self, pending: dict[str, Any]) -> dict[str, Any]:
        agent = pending.get("agent")
        capability = pending.get("capability")
        inputs = pending.get("inputs") or {}
        if not agent or not capability:
            return {"reply": "I couldn't execute that pending action."}

        try:
            result = await self._registry.dispatch(agent, capability, inputs)
        except Exception as exc:
            logger.warning("router_pending_dispatch_failed", error=str(exc))
            return {"reply": f"Error dispatching request: {exc}"}

        prompt_text = pending.get("prompt_text") or pending.get("reason") or "pending action"
        reply_text = await self._humanize(str(prompt_text), agent, capability, result)
        return {"reply": reply_text}

    async def _semantic_fallback(self, text: str) -> dict[str, Any] | None:
        results = await self._registry.semantic_search(text, top_k=3)
        if not results:
            return None
        best = results[0]
        if best["score"] < MIN_SEMANTIC_SCORE:
            return None
        payload = best["payload"]
        return {"agent": payload.get("agent"), "capability": payload.get("capability")}

    async def _humanize(
        self,
        text: str,
        agent: str,
        capability: str,
        raw_result: Any,
    ) -> str:
        """Convert a tool's raw result into a friendly natural-language reply.

        - If the result is already a plain string, return as-is.
        - If the agent flagged the result as `already_natural` (e.g. the chat
          capability), return its `reply` field directly.
        - Otherwise, ask the local LLM to rephrase the result for the user.
        - Falls back to a JSON dump of the result if the LLM is unavailable.
        """
        # Unwrap the SDK's invoke envelope: {"ok": True, "result": <payload>}.
        payload = raw_result
        if isinstance(raw_result, dict) and "result" in raw_result:
            payload = raw_result["result"]

        # Conversational tools can short-circuit humanization.
        if isinstance(payload, dict) and payload.get("already_natural") and "reply" in payload:
            return str(payload["reply"])

        if isinstance(payload, str):
            return payload

        if self._llm is None:
            return json.dumps(payload, ensure_ascii=False, default=str)

        compact = json.dumps(payload, ensure_ascii=False, default=str)
        if len(compact) > 4000:
            compact = compact[:4000] + "…(truncated)"

        system = (
            "You are Home Intelligence — a friendly, concise home AI assistant. "
            "You receive a user message and a JSON result from a tool. "
            "Reply in 1-6 short sentences, using natural language only. "
            "Group items by area when applicable. Use bullet points for lists "
            "longer than three items. Hide technical fields like entity_id and "
            "domain unless the user explicitly asked for them. If the data shows "
            "many items in 'unavailable' state, mention the count once and skip "
            "them unless the user asked to see them."
        )
        user = (
            f"User asked: {text!r}\n"
            f"Capability: {agent}.{capability}\n"
            f"Tool returned (JSON):\n{compact}\n\n"
            "Compose the user-facing reply now."
        )
        try:
            resp = await self._llm.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=self._humanizer_model,
                temperature=0.4,
            )
        except Exception as exc:
            logger.warning("router_humanize_failed", error=str(exc))
            return json.dumps(payload, ensure_ascii=False, default=str)
        content = (resp.get("message") or {}).get("content")
        if not content:
            return json.dumps(payload, ensure_ascii=False, default=str)
        return str(content).strip()


def _empty_classification() -> dict[str, Any]:
    return {
        "agent": None,
        "capability": None,
        "inputs": {},
        "needs_confirmation": False,
        "reason": "",
    }
