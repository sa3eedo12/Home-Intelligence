"""ReAct-style escalator: when the small router fails to route a user
request, we hand the problem to the larger 8B model along with the full
tool catalog and ability to actually query Home Assistant. The 8B can
then iterate (think → tool → observe → think) up to a bounded number of
times before either resolving the request or giving up with a structured
gap report.

Why this exists:
    The 0.6B router is fast and cheap (sub-second) but limited to picking
    from a list of capability names — it can't compose a multi-step call
    like "first list bedroom thermostats, then set the right one to 22".
    The 8B model with iteration can do that AND can resolve genuinely
    ambiguous cases ("the bedroom AC" when there are three thermostats
    in the area).

Design contract:
    - Idempotent steps: each tool call is logged with full args/result
      so the gap log can replay what happened.
    - Bounded iteration: max 4 steps prevents runaway costs (each step
      is a ~5-15s 8B call, so worst-case latency is ~60s for a clearly
      stuck request — still finite and observable).
    - Honest fallout: when escalation gives up, returns
      (None, escalation_path) so the caller writes a gap row and tells
      the user the truth. NEVER fabricates execution.
    - Lazy import of CapabilityRegistry to avoid circular imports
      (router.py already imports this module).
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.telemetry import get_logger

logger = get_logger("escalator")


# Hard upper bound on iterations. Each iteration is a full LLM call +
# tool dispatch, so 4 means worst-case ~60s on the 8B before we give up.
# Bumping this past 6 is almost never the right answer — if the agent
# can't resolve in 4 hops with the full tool catalog, the issue is
# capability composition, not iteration budget.
MAX_ITERATIONS = 4

# Discovery tools the escalator is allowed to call to learn about the
# state of the world. These are read-only and safe to invoke without
# user confirmation. The escalator may then choose to call a side-
# effecting tool with the gathered context.
DISCOVERY_TOOL_PRIORITIES = (
    "list_entities",
    "get_entity_state",
    "list_areas",
    "lights_status",
    "climate_status",
    "list_scenes",
)


ESCALATOR_SYSTEM_PROMPT = """You are a problem-solving agent for a single-user home automation system.

The fast router (a tiny model) couldn't resolve the user's request from the capability list. Your job is to figure it out by calling tools iteratively.

ON EACH STEP, respond with ONE compact JSON object — no prose, no code fences. Pick exactly one of these shapes:

1. To call a tool:
   {"action": "tool_call", "agent": "<agent>", "capability": "<capability id>", "inputs": {...}, "rationale": "<one short sentence>"}

2. When you've gathered enough information AND completed the user's request:
   {"action": "resolved", "reply": "<user-facing reply>", "rationale": "<what you did>"}

3. When you cannot make progress — no tool fits the request, every tool you tried errored, or you need user clarification:
   {"action": "give_up", "reason": "<short reason>", "suggested_tool_spec": {<optional tool spec the system should add>}}

CRITICAL CAPABILITY FORMAT:
The catalog lists capabilities as "agent.capability_id: description".
When calling a tool, pass them SEPARATELY:
  - agent: the part BEFORE the first dot (e.g., "home_automation")
  - capability: the part AFTER the first dot (e.g., "climate_status", "lights_off")
NEVER include the agent name inside the capability field.

Examples:
  Catalog line:    "home_automation.climate_status: thermostat status"
  Correct call:    {"action": "tool_call", "agent": "home_automation", "capability": "climate_status", ...}
  WRONG call:      {"action": "tool_call", "agent": "home_automation", "capability": "home_automation.climate_status", ...}

Rules:
- Use ONLY capability ids from the provided catalog. Never invent. Never prepend the agent name.
- Prefer read-only discovery tools (list_entities, climate_status, lights_status, get_entity_state, list_areas) before any side-effecting tool.
- After each tool result you see "OBSERVATION:" — use that to refine your next step.
- If a side-effecting tool succeeds, immediately respond with "resolved".
- If you've tried 2 tools and made no progress, lean toward "give_up" with a useful suggested_tool_spec rather than thrashing.
- NEVER fabricate a result. NEVER claim you did something you didn't do.
"""


class RegistryLike(Protocol):
    """Minimal interface from CapabilityRegistry the escalator needs."""

    def get_capability(self, agent: str, capability: str) -> dict[str, Any] | None: ...

    def list_capabilities(self) -> list[dict[str, Any]]: ...

    async def dispatch(self, agent: str, capability: str, inputs: dict) -> Any: ...


def _format_catalog(caps: list[dict[str, Any]]) -> str:
    """Compact one-line-per-capability inventory for the escalator
    prompt. Slightly more verbose than the router's version because the
    8B can use the extra context to compose multi-step calls."""
    if not caps:
        return "(no capabilities registered)"
    lines = []
    for cap in caps:
        agent = cap.get("agent", "?")
        cap_id = cap.get("id", "?")
        desc = cap.get("description") or ""
        # Strip newlines so each capability fits on one line — keeps the
        # prompt compact even with 20+ tools.
        desc = re.sub(r"\s+", " ", desc).strip()
        inputs = cap.get("inputs") or {}
        marker = "*" if cap.get("side_effects") else " "
        line = f"  {marker} {agent}.{cap_id}: {desc}"
        if inputs:
            line += f"  | inputs: {json.dumps(inputs, ensure_ascii=False)}"
        lines.append(line)
    return "\n".join(lines)


_RESPONSE_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _parse_step(content: str) -> dict[str, Any] | None:
    """Extract a JSON action object from the LLM response. Tolerant of
    code fences and surrounding prose because the 8B occasionally
    ignores the 'no prose' instruction."""
    if not content:
        return None
    cleaned = content.strip()
    # Try whole-string parse first
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    # Greedy {...} match for inline JSON
    match = _RESPONSE_JSON.search(cleaned)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _summarise_observation(result: Any, max_chars: int = 1200) -> str:
    """Trim a tool result to something the LLM context can hold without
    blowing up. Keeps the structure but truncates long string/array
    fields."""
    try:
        text = json.dumps(result, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


class Escalator:
    def __init__(
        self,
        llm: OllamaClient,
        registry: RegistryLike,
        *,
        model: str,
        max_iterations: int = MAX_ITERATIONS,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._model = model
        self._max_iterations = max(1, min(int(max_iterations), 8))

    async def resolve(
        self,
        text: str,
        *,
        prior_attempt: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Try to fulfill the user's request via iterative tool use.

        Returns:
            (resolution_or_None, escalation_path)

            resolution_or_None: when the agent finishes with action="resolved",
                returns {"reply": str, "rationale": str, "tools_used": [...]}.
                Returns None when the agent gives up OR hits max iterations
                OR every tool errored.

            escalation_path: list of step records suitable for storing in
                capability_gaps.escalation_path. Always populated, even on
                successful resolution (useful for auditing the path).
        """
        catalog = self._registry.list_capabilities()
        catalog_text = _format_catalog(catalog)

        prior_block = ""
        if prior_attempt:
            prior_block = (
                f"\nPRIOR ROUTER ATTEMPT (failed):\n"
                f"{json.dumps(prior_attempt, ensure_ascii=False, default=str)}\n"
            )

        history: list[dict[str, str]] = [
            {"role": "system", "content": ESCALATOR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"USER REQUEST: {text}\n\n"
                    f"CAPABILITY CATALOG (* = side-effecting):\n{catalog_text}\n"
                    f"{prior_block}\n"
                    "Respond with the next step JSON now."
                ),
            },
        ]

        escalation_path: list[dict[str, Any]] = []
        tools_used: list[dict[str, Any]] = []
        any_tool_errored = False
        any_tool_ok = False
        last_observation: str | None = None

        for iteration in range(1, self._max_iterations + 1):
            try:
                resp = await self._llm.chat(
                    messages=history,
                    model=self._model,
                    response_format="json",
                    think=False,
                    temperature=0.2,
                )
            except Exception as exc:
                logger.warning(
                    "escalator_llm_failed", iteration=iteration, error=str(exc)
                )
                escalation_path.append({
                    "iter": iteration,
                    "stage": "llm_call",
                    "outcome": "exception",
                    "error": str(exc),
                })
                # Don't append an "exhausted" summary — the exception
                # step is the terminal record for this case. Test
                # expectation: path[-1]["outcome"] == "exception".
                return None, escalation_path

            content = (resp.get("message") or {}).get("content") or ""
            step = _parse_step(content)
            if step is None:
                escalation_path.append({
                    "iter": iteration,
                    "stage": "parse",
                    "outcome": "bad_json",
                    "content_preview": content[:200],
                })
                logger.info(
                    "escalator_bad_json",
                    iteration=iteration,
                    content_preview=content[:200],
                )
                # Give the model one nudge before giving up
                history.append({"role": "assistant", "content": content})
                history.append({
                    "role": "user",
                    "content": (
                        "Your previous response wasn't valid JSON. Reply with "
                        "ONE JSON object: tool_call, resolved, or give_up."
                    ),
                })
                continue

            action = step.get("action")
            if action == "resolved":
                reply = str(step.get("reply") or "").strip()
                if not reply:
                    # No reply provided — treat as give-up
                    escalation_path.append({
                        "iter": iteration,
                        "stage": "resolved",
                        "outcome": "empty_reply",
                    })
                    return None, escalation_path
                # Anti-fabrication guard: if the escalator declares
                # "resolved" without having called ANY tool, that's
                # the 8b inventing a result. Same class of bug as the
                # chat tool fabrication we fixed — never trust an
                # unsubstantiated action-completion claim. Reading the
                # reply text would also fail because the model can
                # write convincing fake confirmations. So we just
                # treat zero-tool resolutions as give_up.
                if not tools_used:
                    logger.warning(
                        "escalator_resolved_without_tool_call_treating_as_giveup",
                        reply_preview=reply[:200],
                    )
                    escalation_path.append({
                        "iter": iteration,
                        "stage": "resolved",
                        "outcome": "no_tool_used_suspect_fabrication",
                        "reply_preview": reply[:200],
                    })
                    return None, escalation_path
                escalation_path.append({
                    "iter": iteration,
                    "stage": "resolved",
                    "outcome": "ok",
                    "rationale": step.get("rationale"),
                })
                return (
                    {
                        "reply": reply,
                        "rationale": step.get("rationale"),
                        "tools_used": tools_used,
                    },
                    escalation_path,
                )

            if action == "give_up":
                escalation_path.append({
                    "iter": iteration,
                    "stage": "give_up",
                    "reason": step.get("reason"),
                    "suggested_tool_spec": step.get("suggested_tool_spec"),
                })
                logger.info(
                    "escalator_gave_up",
                    iteration=iteration,
                    reason=step.get("reason"),
                )
                return None, escalation_path

            if action != "tool_call":
                escalation_path.append({
                    "iter": iteration,
                    "stage": "parse",
                    "outcome": "unknown_action",
                    "action": action,
                })
                continue

            agent = step.get("agent")
            capability = step.get("capability")
            inputs = step.get("inputs") or {}
            if not isinstance(inputs, dict):
                inputs = {}

            # Defensive normalisation: the 8b sometimes includes the
            # agent name in the capability field (despite the prompt
            # forbidding it). Strip a leading "<agent>." if present so
            # the call still works instead of bouncing through
            # invalid_capability and burning an iteration.
            if isinstance(capability, str) and agent and isinstance(agent, str):
                prefix = f"{agent}."
                if capability.startswith(prefix):
                    capability = capability[len(prefix):]

            if not agent or not capability:
                escalation_path.append({
                    "iter": iteration,
                    "stage": "tool_call",
                    "outcome": "missing_agent_or_capability",
                    "step": step,
                })
                history.append({"role": "assistant", "content": content})
                history.append({
                    "role": "user",
                    "content": (
                        "OBSERVATION: missing agent or capability. "
                        "Pick from the catalog above."
                    ),
                })
                continue

            if self._registry.get_capability(agent, capability) is None:
                escalation_path.append({
                    "iter": iteration,
                    "stage": "tool_call",
                    "outcome": "invalid_capability",
                    "agent": agent,
                    "capability": capability,
                })
                history.append({"role": "assistant", "content": content})
                history.append({
                    "role": "user",
                    "content": (
                        f"OBSERVATION: capability {agent}.{capability} does "
                        "not exist. Choose ONLY from the catalog above. If "
                        "no suitable capability exists, respond with give_up."
                    ),
                })
                continue

            try:
                result = await self._registry.dispatch(agent, capability, inputs)
                tool_outcome = "ok"
                any_tool_ok = True
            except Exception as exc:
                result = {
                    "error": "dispatch_exception",
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
                tool_outcome = "exception"
                any_tool_errored = True

            tools_used.append({
                "agent": agent,
                "capability": capability,
                "inputs": inputs,
                "outcome": tool_outcome,
            })
            obs = _summarise_observation(result)
            last_observation = obs
            escalation_path.append({
                "iter": iteration,
                "stage": "tool_call",
                "outcome": tool_outcome,
                "agent": agent,
                "capability": capability,
                "inputs": inputs,
                "result_summary": obs[:300],
            })

            history.append({"role": "assistant", "content": content})
            history.append({
                "role": "user",
                "content": f"OBSERVATION: {obs}\n\nNext step?",
            })

        # Loop exhausted without resolution
        if any_tool_errored and not any_tool_ok:
            outcome_kind = "all_errored"
        elif not tools_used:
            outcome_kind = "no_tool_proposed"
        else:
            outcome_kind = "max_iterations"
        escalation_path.append({
            "iter": self._max_iterations + 1,
            "stage": "exhausted",
            "outcome": outcome_kind,
            "last_observation_preview": (last_observation or "")[:300],
        })
        logger.info(
            "escalator_exhausted",
            outcome=outcome_kind,
            tools_used=len(tools_used),
        )
        return None, escalation_path


def map_exhausted_outcome_to_failure_reason(escalation_path: list[dict[str, Any]]) -> str:
    """Translate the last step of escalation_path into a known
    failure_reason for GapStore. Lives here (not in GapStore) because
    the mapping is escalator-specific."""
    if not escalation_path:
        return "escalator_no_tool_proposed"
    last = escalation_path[-1]
    if last.get("stage") == "exhausted":
        kind = last.get("outcome")
        if kind == "all_errored":
            return "escalator_all_tools_errored"
        if kind == "no_tool_proposed":
            return "escalator_no_tool_proposed"
        if kind == "max_iterations":
            return "escalator_max_iterations"
    if last.get("stage") == "give_up":
        return "escalator_no_tool_proposed"
    return "escalator_max_iterations"
