from __future__ import annotations

import json
import os
import re
from typing import Any

from home_agents_sdk.gap_store import GapStore
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.npu import NPUClient, NPUUnavailable
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger

from .registry import CapabilityRegistry
from .safety import SafetyPolicy

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


# Action-verb detection: when the user's message starts with (or
# prominently contains) an action verb but the router still routes them
# to personal_assistant.chat, that's almost certainly a missing-tool
# situation, not a chat request. We record a capability gap in that case
# so the reflector can mine the pattern even when the chat tool
# successfully (or not) returns a reply.
#
# The list is deliberately broad — false positives here just cost us
# extra gap rows that the reflector will see as low-priority noise.
# False negatives (missed action verbs that get fabricated answers)
# are the real harm.
_ACTION_VERB_PATTERNS = [
    re.compile(
        r"\b("
        # core control verbs
        r"turn\s+(on|off)|switch\s+(on|off)|toggle|"
        # climate / numeric adjustments
        r"reduce|increase|raise|lower|set|adjust|change|dim|brighten|cool|heat|warm|"
        # movement / openness
        r"open|close|shut|lock|unlock|"
        # media
        r"play|pause|stop|resume|skip|mute|unmute|"
        # automation
        r"start|begin|run|trigger|cancel|abort|schedule|remind|"
        # query that expects action follow-through
        r"check\s+(?:and|then)|do\s+(?:a|the|this|that)"
        r")\b",
        re.IGNORECASE,
    ),
]


def _is_action_verb_request(text: str) -> bool:
    """True if the user's text reads like an action they want performed,
    not a question or a chat. Used to detect when chat-fallback is
    probably wrong and a capability gap should be recorded."""
    if not text:
        return False
    return any(p.search(text) for p in _ACTION_VERB_PATTERNS)


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
        safety: SafetyPolicy | None = None,
        proposal_store: ReflectionStore | None = None,
        gap_store: GapStore | None = None,
        escalator: Any | None = None,
    ) -> None:
        self._npu = npu
        self._registry = registry
        self._router_model = router_model
        self._llm = llm
        self._llm_fallback_model = llm_fallback_model
        self._humanizer_model = humanizer_model or llm_fallback_model or router_model
        self._safety = safety or SafetyPolicy(
            os.environ.get("SAFETY_POLICY_PATH", "policies/safety.yaml")
        )
        self._proposal_store = proposal_store or ReflectionStore(None)
        # gap_store is optional so existing tests don't break; if absent
        # we silently skip recording (behaviour matches pre-feature
        # baseline). Production code wires in a real one in app.py.
        self._gap_store = gap_store or GapStore(None)
        # escalator is also optional. When present, classify-failed and
        # invalid-capability paths go through it before falling all the
        # way to the conversational catch-all. The Escalator type is
        # `Any` here to avoid a circular import — actual contract is the
        # `EscalatorProtocol` in escalator.py.
        self._escalator = escalator

    async def _record_gap_safe(self, **kwargs: Any) -> None:
        """Defensive wrapper around GapStore.record_gap. GapStore itself
        already fails open on DB errors, but we add a second guard here
        so a future store variant (or a bug in instrumentation) can
        never break user replies. Telemetry must never be on the
        critical path."""
        try:
            await self._gap_store.record_gap(**kwargs)
        except Exception as exc:
            logger.warning("router_gap_record_failed", error=str(exc))

    async def handle(
        self,
        text: str,
        user_id: str,
        autonomous: bool = False,
        member_id: int | None = None,
        member_name: str | None = None,
    ) -> dict[str, Any]:
        # Fast path: short conversational greetings/acks go straight to
        # personal_assistant.chat without calling the LLM classifier or the
        # semantic-search fallback. Saves ~1-2s per greeting.
        if _is_conversational_shortcut(text) and (
            self._registry.get_capability("personal_assistant", "chat") is not None
        ):
            inputs = {"text": text}
            safety_response = await self._safety_gate(
                text=text,
                user_id=user_id,
                agent="personal_assistant",
                capability="chat",
                inputs=inputs,
                autonomous=autonomous,
                reason="conversational shortcut",
                member_id=member_id,
                member_name=member_name,
            )
            if safety_response is not None:
                return safety_response
            try:
                result = await self._registry.dispatch("personal_assistant", "chat", inputs)
                reply_text = await self._humanize(text, "personal_assistant", "chat", result)
                return {"reply": reply_text}
            except Exception as exc:
                logger.warning("router_fast_path_failed", error=str(exc))
                # Fall through to the normal path if chat dispatch fails.

        classification = await self._classify(text)
        agent = classification.get("agent")
        capability = classification.get("capability")
        inputs = classification.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        needs_confirmation = classification.get("needs_confirmation", False)
        reason = classification.get("reason", "")

        # Track for gap recording. Snapshot what the classifier picked
        # BEFORE we mutate agent/capability for invalid-capability
        # fallback, so gap rows show the original (wrong) pick.
        classifier_pick = {
            "agent": agent,
            "capability": capability,
            "inputs": inputs,
            "reason": reason,
        }
        escalation_path: list[dict[str, Any]] = []

        # Validate the LLM's pick exists. If not, fall through to semantic search.
        invalid_capability_attempted = False
        if agent and capability and self._registry.get_capability(agent, capability) is None:
            logger.info(
                "router_classify_invalid_capability",
                agent=agent,
                capability=capability,
            )
            invalid_capability_attempted = True
            escalation_path.append({
                "stage": "router_classify",
                "outcome": "invalid_capability",
                "agent": agent,
                "capability": capability,
            })
            agent = None
            capability = None

        if agent is None or capability is None:
            fallback = await self._semantic_fallback(text)
            if fallback is not None:
                agent = fallback["agent"]
                capability = fallback["capability"]
                escalation_path.append({
                    "stage": "semantic_fallback",
                    "outcome": "matched",
                    "agent": agent,
                    "capability": capability,
                })
            else:
                # Escalator gets a shot BEFORE chat-catchall. The 8B
                # with iterative tool use can often resolve what the
                # 0.6b router couldn't: ambiguous areas, multi-step
                # composition, picking the right tool from a noisy
                # catalog. Only invoked when the escalator is wired
                # AND the request is an action verb OR the classifier
                # picked something invalid (signals real intent that
                # failed to route, not just chit-chat).
                escalator_resolved = None
                if self._escalator is not None and (
                    _is_action_verb_request(text) or invalid_capability_attempted
                ):
                    try:
                        escalator_resolved, esc_path = await self._escalator.resolve(
                            text, prior_attempt=classifier_pick
                        )
                        escalation_path.extend(esc_path)
                    except Exception as exc:
                        logger.warning("router_escalator_failed", error=str(exc))
                        escalation_path.append({
                            "stage": "escalator",
                            "outcome": "exception",
                            "error": str(exc),
                        })

                if escalator_resolved is not None:
                    # Escalator did the work and returned a user-ready
                    # reply. Note: NO gap is recorded — escalation
                    # succeeding is the system working as designed.
                    logger.info(
                        "router_escalator_resolved",
                        tools_used=len(escalator_resolved.get("tools_used") or []),
                    )
                    return {"reply": escalator_resolved["reply"]}

                # Compute the failure_reason from the escalator's terminal
                # step BEFORE we append chat_catchall to the path — otherwise
                # the mapper sees chat_catchall instead of the escalator's
                # give_up / exhausted outcome.
                escalator_failure_reason: str | None = None
                if self._escalator is not None:
                    from .escalator import (
                        map_exhausted_outcome_to_failure_reason,
                    )
                    escalator_failure_reason = (
                        map_exhausted_outcome_to_failure_reason(escalation_path)
                    )

                # Escalator gave up (or wasn't wired) — fall through to
                # the existing chat catch-all or generic decline.
                if self._registry.get_capability("personal_assistant", "chat") is not None:
                    # Conversational catch-all: smalltalk, greetings, general questions.
                    agent = "personal_assistant"
                    capability = "chat"
                    inputs = {"text": text}
                    escalation_path.append({
                        "stage": "chat_catchall",
                        "outcome": "matched",
                    })
                    # Record a gap when chat-catchall fires for an action-
                    # verb request. This is the hallucination root cause: a
                    # tiny router model couldn't compose the right tool call
                    # and the catch-all chat tool used to invent execution
                    # narratives. With this gap row + the chat.py refusal,
                    # the loop closes around real user pain points.
                    if _is_action_verb_request(text):
                        failure_reason = (
                            escalator_failure_reason
                            or "chat_fallback_for_action_verb"
                        )
                        await self._record_gap_safe(
                            user_text=text,
                            failure_reason=failure_reason,
                            router_pick=classifier_pick,
                            escalation_path=escalation_path,
                            member_id=member_id,
                            member_name=member_name,
                        )
                    elif invalid_capability_attempted:
                        # Classifier picked something invalid and then we
                        # fell to chat. Record the gap even for non-action
                        # text so the reflector can see naming drift.
                        await self._record_gap_safe(
                            user_text=text,
                            failure_reason="invalid_capability",
                            router_pick=classifier_pick,
                            escalation_path=escalation_path,
                            member_id=member_id,
                            member_name=member_name,
                        )
                else:
                    # No fallback path at all — record this as a hard gap
                    # before returning the generic decline.
                    await self._record_gap_safe(
                        user_text=text,
                        failure_reason=(
                            "invalid_capability"
                            if invalid_capability_attempted
                            else "escalator_no_tool_proposed"
                        ),
                        router_pick=classifier_pick,
                        escalation_path=escalation_path,
                        user_reply="I don't have a capability for that yet.",
                        member_id=member_id,
                        member_name=member_name,
                    )
                    return {"reply": "I don't have a capability for that yet."}

        cap_meta = self._registry.get_capability(agent, capability)
        if cap_meta and cap_meta.get("require_confirmation"):
            needs_confirmation = True

        # Guarantee personal_assistant.chat always receives the user text,
        # even when the LLM classifier forgets to populate `inputs.text`.
        if agent == "personal_assistant" and capability == "chat":
            inputs = {"text": text}

        safety_response = await self._safety_gate(
            text=text,
            user_id=user_id,
            agent=agent,
            capability=capability,
            inputs=inputs,
            autonomous=autonomous,
            reason=str(reason or ""),
            member_id=member_id,
            member_name=member_name,
        )
        if safety_response is not None:
            return safety_response

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
            escalation_path.append({
                "stage": "dispatch",
                "outcome": "exception",
                "agent": agent,
                "capability": capability,
                "error": f"{type(exc).__name__}: {exc!s}",
            })
            await self._record_gap_safe(
                user_text=text,
                failure_reason="dispatch_failed",
                router_pick=classifier_pick,
                escalation_path=escalation_path,
                user_reply=f"Error dispatching request: {exc}",
                member_id=member_id,
                member_name=member_name,
            )
            return {"reply": f"Error dispatching request: {exc}"}

        # Humanize the result unless the agent already returned natural text.
        reply_text = await self._humanize(text, agent, capability, result)
        return {"reply": reply_text}

    async def _safety_gate(
        self,
        *,
        text: str,
        user_id: str,
        agent: str,
        capability: str,
        inputs: dict[str, Any],
        autonomous: bool,
        reason: str,
        member_id: int | None = None,
        member_name: str | None = None,
    ) -> dict[str, Any] | None:
        explanation = self._safety.explain(agent, capability, inputs)
        tier = str(explanation.get("tier") or "suggest")
        safety_reason = str(explanation.get("reason") or "the safety policy requires review")

        if tier == "never":
            return {
                "reply": (
                    "I won't do that automatically. "
                    f"{safety_reason} Please do it yourself manually."
                )
            }

        if tier == "suggest" and autonomous:
            proposal_id = await self._add_safety_proposal(
                text=text,
                user_id=user_id,
                agent=agent,
                capability=capability,
                inputs=inputs,
                reason=reason,
                explanation=explanation,
                member_id=member_id,
                member_name=member_name,
            )
            return {
                "reply": "I saved that as a suggestion for you to review before anything runs.",
                "proposal": {
                    "id": proposal_id,
                    "agent": agent,
                    "capability": capability,
                    "tier": tier,
                },
            }

        return None

    async def _add_safety_proposal(
        self,
        *,
        text: str,
        user_id: str,
        agent: str,
        capability: str,
        inputs: dict[str, Any],
        reason: str,
        explanation: dict[str, Any],
        member_id: int | None = None,
        member_name: str | None = None,
    ) -> int:
        action = {
            "agent": agent,
            "capability": capability,
            "inputs": inputs,
            "prompt": text,
            "user_id": user_id,
            "router_reason": reason,
            "safety": explanation,
        }
        if member_id is not None:
            action["member_id"] = member_id
        if member_name:
            action["member_name"] = member_name
        rationale = (
            "Autonomous execution is classified as suggest, so this action is waiting "
            f"for user confirmation. Action: {json.dumps(action, ensure_ascii=False, default=str)}"
        )
        try:
            return await self._proposal_store.add_proposal(
                kind="suggested_action",
                title=f"Review {agent}.{capability}",
                rationale=rationale,
                evidence_event_ids=[],
                confidence=0.5,
                impact_estimate=str(explanation.get("reason") or "Requires user review."),
                status="pending",
                delivery_channel="inbox",
                for_member_id=member_id,
            )
        except Exception as exc:
            logger.warning("router_add_safety_proposal_failed", error=str(exc))
            return 0

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
                # Router classification is structured-output only; thinking
                # adds 5-30s per request which delays the request flow.
                think=False,
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
