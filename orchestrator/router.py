from __future__ import annotations

import json
from typing import Any

from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.npu import NPUClient, NPUUnavailable
from home_agents_sdk.telemetry import get_logger

from .registry import CapabilityRegistry

logger = get_logger("router")

SYSTEM_PROMPT = """You are a strict JSON router for a single-user home automation assistant.
Reply ONLY with compact JSON matching this schema (no prose, no code fences):
{
 "agent": "<one of the available agent ids, or null>",
 "capability": "<a capability id from that agent, or null>",
 "inputs": { "<param>": <value>, ... },
 "needs_confirmation": <bool>,
 "reason": "<one short sentence>"
}"""

MIN_SEMANTIC_SCORE = 0.55


class Router:
    def __init__(
        self,
        npu: NPUClient,
        registry: CapabilityRegistry,
        router_model: str,
        llm: OllamaClient | None = None,
        llm_fallback_model: str | None = None,
    ) -> None:
        self._npu = npu
        self._registry = registry
        self._router_model = router_model
        self._llm = llm
        self._llm_fallback_model = llm_fallback_model

    async def handle(self, text: str, user_id: str) -> dict[str, Any]:
        classification = await self._classify(text)
        agent = classification.get("agent")
        capability = classification.get("capability")
        inputs = classification.get("inputs", {})
        needs_confirmation = classification.get("needs_confirmation", False)
        reason = classification.get("reason", "")

        if agent is None or capability is None:
            fallback = await self._semantic_fallback(text)
            if fallback is None:
                return {"reply": "I don't have a capability for that yet."}
            agent = fallback["agent"]
            capability = fallback["capability"]

        cap_meta = self._registry.get_capability(agent, capability)
        if cap_meta and cap_meta.get("require_confirmation"):
            needs_confirmation = True

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
            return {"reply": str(result)}
        except Exception as exc:
            logger.warning("router_dispatch_failed", error=str(exc))
            return {"reply": f"Error dispatching request: {exc}"}

    async def _classify(self, text: str) -> dict[str, Any]:
        agents = self._registry.agents()
        agent_list = ", ".join(agents) if agents else "none"
        user_msg = f"Available agents: {agent_list}\n\nUser request: {text}"
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

    async def _semantic_fallback(self, text: str) -> dict[str, Any] | None:
        results = await self._registry.semantic_search(text, top_k=3)
        if not results:
            return None
        best = results[0]
        if best["score"] < MIN_SEMANTIC_SCORE:
            return None
        payload = best["payload"]
        return {"agent": payload.get("agent"), "capability": payload.get("capability")}


def _empty_classification() -> dict[str, Any]:
    return {
        "agent": None,
        "capability": None,
        "inputs": {},
        "needs_confirmation": False,
        "reason": "",
    }
