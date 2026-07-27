from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.reflection_store import PROPOSAL_KINDS, ReflectionStore
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

from .registry import CapabilityRegistry

logger = get_logger("orchestrator.reflector")

USER_PROFILE_TEMPLATE: dict[str, str] = {
    "wake_time": "Usual wake-up time and day-to-day variance.",
    "sleep_time": "Usual sleep time and quiet-hours preference.",
    "work_hours": "Work schedule, meetings-heavy days, and commute windows.",
    "wake_time_observed": "Observed wake-up time from Apple Health sleep data.",
    "sleep_time_observed": "Observed sleep timing and duration from Apple Health sleep data.",
    "daily_step_target": "Step goal or typical daily step baseline for nudges.",
    "last_workout_at": "Most recent workout timestamp and workout type.",
    "allergies": "Any allergies relevant to meals, shopping, and reminders.",
    "dietary_restrictions": "Dietary restrictions, avoidances, or nutrition goals.",
    "household_members": "People in the household and how they prefer to be referenced.",
    "household_size": "Number of people and pets living in the household.",
    "pets": "Pets, feeding routines, and care reminders.",
    "favorite_cuisines": "Favorite cuisines and default meal preferences.",
    "music_preferences": "Music genres, volume/time preferences, and contexts.",
    "hvac_preferences": "Comfort temperature ranges and schedule preferences.",
    "lighting_preferences": "Room lighting scenes and brightness habits.",
    "notification_preferences": "What deserves Telegram interruptions versus dashboard-only notes.",
    "chores_routine": "Recurring chores and preferred timing.",
    "shopping_preferences": "Preferred stores, brands, substitutions, and reorder thresholds.",
    "privacy_preferences": "Data the assistant should avoid storing or should forget quickly.",
}

# Conversational versions of the gap questions, used when the LLM hasn't generated
# personalized questions. The mechanical "Should I learn your X?" felt robotic;
# these phrasings invite a short reply.
GAP_QUESTION_PHRASING: dict[str, str] = {
    "wake_time": "What time do you usually wake up on weekdays?",
    "sleep_time": "What time do you usually try to be asleep by?",
    "work_hours": (
        "What does your typical work week look like — when do you usually start and stop?"
    ),
    "wake_time_observed": (
        "I don't have your sleep data from Apple Health yet. "
        "Want to set up the Mac bridge so I can learn your real wake times?"
    ),
    "sleep_time_observed": (
        "I'm not getting your Apple Health sleep data. "
        "Once it's connected I can learn your actual sleep window."
    ),
    "daily_step_target": "Do you have a daily step goal you'd like me to nudge you toward?",
    "last_workout_at": "When was your last workout, and what kind?",
    "allergies": "Any allergies I should remember when I help with meals or shopping?",
    "dietary_restrictions": "Any foods you avoid, or nutrition goals I should know about?",
    "household_members": (
        "Who else lives with you? I'd like to know their names so I can keep track."
    ),
    "household_size": "How many people (and pets) live in the household total?",
    "pets": "Do you have any pets? Names and what they eat would help me with reminders.",
    "favorite_cuisines": (
        "What kinds of food do you usually like? I can use this for meal suggestions."
    ),
    "music_preferences": (
        "Any music styles you usually want playing — and times you'd rather have it quiet?"
    ),
    "hvac_preferences": (
        "What temperature do you like the house at? Different for day vs. night?"
    ),
    "lighting_preferences": (
        "Any lighting preferences — bright in the morning, dim in the evening, scenes?"
    ),
    "notification_preferences": (
        "Want to tell me which things deserve a Telegram ping "
        "vs. just sitting on the dashboard?"
    ),
    "chores_routine": (
        "Any chores you do on a regular schedule (laundry day, vacuum day, etc.)?"
    ),
    "shopping_preferences": (
        "Which stores and brands do you prefer, and what should I auto-reorder?"
    ),
    "privacy_preferences": (
        "Anything you'd rather I don't track, store, or surface in the dashboard?"
    ),
}


def _phrase_gap_question(key: str, fallback: str | None = None) -> str:
    return GAP_QUESTION_PHRASING.get(
        key, f"Should I learn your {key.replace('_', ' ')}?" if not fallback else fallback
    )




SYSTEM_PROMPT = """You are the Home Intelligence nightly reflector.
You audit a local-first multi-agent home assistant and propose small self-improvements.
Return ONLY compact JSON as a list of proposal objects in this exact shape:
[
  {
    "kind": "code_change|habit_inference|preference_inference|routine_inference|cleanup_action",
    "title": "short actionable title",
    "rationale": "why this helps",
    "evidence_event_ids": [1, 2],
    "evidence_keys": ["wake_time"],
    "confidence": 0.0,
    "cost_estimate": "tiny|small|medium|large",
    "impact_estimate": "short impact statement",
    "profile_key": "optional user_profile key for inferences",
    "profile_value": {"optional": "value to write when auto-confirmed"}
  }
]
Rules:
- Generate at most 8 proposals.
- Every proposal MUST cite at least one specific event id in evidence_event_ids OR at least one
  knowledge-gap key in evidence_keys. Never propose uncited ideas.
- Use only these kinds: code_change, habit_inference, preference_inference, routine_inference,
  cleanup_action.
- Be conservative with user facts; use confidence >= 0.95 only for repeated, obvious habits.
- Code changes should be wishlist proposals, not direct file edits.
"""


def _json_compact(value: Any, limit: int = 9000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _extract_json(value: str) -> Any:
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    object_start = text.find("{")
    array_start = text.find("[")
    starts = [idx for idx in (object_start, array_start) if idx >= 0]
    if starts:
        start = min(starts)
        end_char = "}" if text[start] == "{" else "]"
        end = text.rfind(end_char)
        if end >= start:
            text = text[start : end + 1]
    return json.loads(text)


def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clamp_confidence(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(value, 1.0))


def _ints(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:48] or "habit"


# Words/phrases that signal a code_change proposal is grounded in a
# real, measurable problem rather than speculative ("might be a
# problem"). The reflector LLM is too eager to suggest jitter / caching
# / retry caps for normal traffic; gate code_change behind one of these.
_CONCRETE_PROBLEM_TERMS = (
    "error",
    "exception",
    "traceback",
    "stack trace",
    "crashed",
    "crash",
    "panic",
    "failed",
    "failure",
    "timeout",
    "timed out",
    "5xx",
    "500 ",
    "503",
    "504",
    "regression",
    "spike",
    "leak",
    "deadlock",
    "stuck",
    "hung",
    "data loss",
    "duplicate ",
    "missing ",
    "incorrect",
)
# Speculation markers — when the rationale leans on these without any
# concrete-problem term, it's the LLM guessing at improvements.
_SPECULATIVE_TERMS = (
    "potential",
    "may indicate",
    "might indicate",
    "could be",
    "could indicate",
    "suggests a potential",
    "thundering herd",
    "if this pattern",
    "if these result",
    "if other agents",
    "to prevent",
    "to optimize",
    "to avoid",
)
MIN_CODE_CHANGE_EVIDENCE = 5
# Per-kind back-off: if the user has dismissed >= N proposals of a given
# kind in the last M days WITHOUT accepting any, the reflector pauses
# emitting more of that kind. Closes the observe -> infer -> CORRECT
# loop for the proposal layer (mirrors auto_infer's correction memory).
PROPOSAL_BACKOFF_DISMISSALS = 5
PROPOSAL_BACKOFF_DAYS = 14


def _has_concrete_code_change_evidence(
    rationale: str, evidence_event_ids: list[int]
) -> bool:
    """Return True if a code_change proposal is grounded enough to keep.

    Two gates:
      1. At least MIN_CODE_CHANGE_EVIDENCE distinct event citations.
         Speculative 'might be a problem' suggestions typically cite
         3 or fewer events.
      2. The rationale must contain a concrete-problem term (error,
         timeout, regression...) OR have NO speculation hedging.
    """
    if len(evidence_event_ids) < MIN_CODE_CHANGE_EVIDENCE:
        return False
    rationale_lower = rationale.lower()
    has_concrete = any(term in rationale_lower for term in _CONCRETE_PROBLEM_TERMS)
    has_speculation = any(term in rationale_lower for term in _SPECULATIVE_TERMS)
    if has_concrete:
        return True
    # No concrete keyword AND no speculation hedging is rare but allowed
    # — the LLM might just describe the problem in plain language.
    return not has_speculation


class NightlyReflector:
    def __init__(
        self,
        pool: Any | None,
        redis: Redis | None,
        llm: OllamaClient,
        registry: CapabilityRegistry,
        reasoner_model: str,
        fallback_model: str,
        gap_store: Any | None = None,
    ) -> None:
        self.pool = pool
        self.redis = redis
        self.llm = llm
        self.registry = registry
        # qwen36-moe-32k is the recommended reasoner: beat qwen3:14b on our
        # planner (88% vs 81%) and log_classifier (86% vs 77%) fixtures and
        # runs faster on the Strix Halo iGPU (9.1 vs 8.5 tok/s). Verified
        # Vulkan-compatible after Ollama v0.30.5 + OLLAMA_IGPU_ENABLE=1.
        # Held at 32K context deliberately: 128K cost 26 GB resident vs 23 GB
        # here, and prompt processing at that depth exceeded what agent
        # callers will wait for. Falls back to REASONER_MODEL env for
        # callers that override.
        self.reasoner_model = reasoner_model or os.environ.get("REASONER_MODEL", "qwen36-moe-32k")
        self.fallback_model = fallback_model or os.environ.get("DEFAULT_MODEL", "qwen3-8b-16k")
        self.store = ReflectionStore(pool)
        # gap_store is optional — when present, _mine_capability_gaps
        # runs and produces dedicated code_change proposals for missing
        # tools / refactors the day-to-day usage actually needed.
        # Lazy import to avoid the hard dependency on home_agents_sdk
        # in test fixtures that don't need gap mining.
        if gap_store is None and pool is not None:
            from home_agents_sdk.gap_store import GapStore
            gap_store = GapStore(pool)
        self.gap_store = gap_store
        self.health_store: Any | None = None
        # Cache of available Ollama model tags. Populated lazily on first
        # use; populated from /api/tags so we can skip an LLM call entirely
        # when the configured reasoner model isn't pulled.
        self._available_models: set[str] | None = None
        # Live status surfaced through GET /admin/reflection/status so the
        # Morning Brief page can show a "running…" banner while the
        # nightly job (or a manual run) is mid-flight.
        self._status: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "phase": None,
            "last_finished_at": None,
            "last_brief_id": None,
            "last_error": None,
            "last_duration_seconds": None,
        }

    @property
    def status(self) -> dict[str, Any]:
        snapshot = dict(self._status)
        if snapshot["running"] and snapshot.get("started_at"):
            try:
                started = datetime.fromisoformat(str(snapshot["started_at"]))
                snapshot["elapsed_seconds"] = (
                    datetime.now(UTC) - started
                ).total_seconds()
            except ValueError:
                snapshot["elapsed_seconds"] = None
        else:
            snapshot["elapsed_seconds"] = None
        return snapshot

    async def run_once(self) -> dict[str, Any]:
        if self._status.get("running"):
            logger.info(
                "reflection_already_running",
                started_at=self._status.get("started_at"),
            )
            return {"ok": False, "error": "reflection_already_running", "status": self.status}

        start = datetime.now(UTC)
        self._status.update(
            {
                "running": True,
                "started_at": start.isoformat(),
                "phase": "starting",
                "last_error": None,
            }
        )
        errors: list[dict[str, str]] = []
        try:
            self._status["phase"] = "gather_evidence"
            evidence = await self._phase("gather_evidence", self._gather_evidence, errors, {})
            self._status["phase"] = "self_audit"
            audit = await self._phase("self_audit", self._self_audit, errors, {}, evidence)
            self._status["phase"] = "knowledge_gaps"
            gaps = await self._phase("knowledge_gaps", self._knowledge_gaps, errors, [])
            self._status["phase"] = "pattern_mining"
            patterns = await self._phase(
                "pattern_mining", self._pattern_mining, errors, [], evidence
            )
            self._status["phase"] = "mine_capability_gaps"
            gap_proposals = await self._phase(
                "mine_capability_gaps", self._mine_capability_gaps, errors, []
            )
            self._status["phase"] = "refine_proposals"
            refined_proposals = await self._phase(
                "refine_proposals", self._refine_proposals, errors, []
            )
            self._status["phase"] = "health_summary"
            health_summary = await self._phase("health_summary", self._health_summary, errors, {})
            self._status["phase"] = "correlations"
            correlations = await self._phase("correlations", self._correlations, errors, [])
            self._status["phase"] = "generate_proposals"
            proposals = await self._phase(
                "generate_proposals",
                self._generate_proposals,
                errors,
                [],
                evidence,
                audit,
                gaps,
                patterns,
                health_summary,
            )
            self._status["phase"] = "apply_auto_confirm_rules"
            applied = await self._phase(
                "apply_auto_confirm_rules",
                self._apply_auto_confirm_rules,
                errors,
                [],
                proposals,
            )
            self._status["phase"] = "synthesize_nightly_brief"
            # ONE 35B call per night that reads everything that just
            # ran and produces a headline + tomorrow's attention item.
            # The 35B is reserved for this single-shot synthesis
            # because it deadlocks under sustained back-to-back calls
            # (Vulkan/RADV: GPU sits at 0% busy). A single call is
            # safe — and the quality lift over the 8B is worth it
            # when the output becomes the morning brief headline.
            #
            # _phase doesn't support kwargs, so we wrap the call
            # ourselves to get the same error-containment behavior.
            try:
                synthesis = await self._synthesize_nightly_brief(
                    refined_proposals=refined_proposals,
                    gap_proposals=gap_proposals,
                    proposals=proposals,
                    health_summary=health_summary,
                    patterns=patterns,
                    correlations=correlations,
                    knowledge_gaps=gaps,
                )
            except Exception as exc:
                error_repr = f"{type(exc).__name__}: {exc!s}".strip().rstrip(":")
                logger.warning(
                    "reflection_phase_failed",
                    phase="synthesize_nightly_brief",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    error_repr=error_repr,
                )
                errors.append({"phase": "synthesize_nightly_brief", "error": error_repr})
                synthesis = {}
            self._status["phase"] = "save_brief"
            body = self._build_brief_body(
                evidence,
                audit,
                gaps,
                patterns,
                health_summary,
                applied,
                errors,
                correlations=correlations,
                gap_proposals=gap_proposals,
                refined_proposals=refined_proposals,
                nightly_synthesis=synthesis,
            )
            brief_id = await self._phase("save_brief", self._save_brief, errors, 0, body)
            body["brief_id"] = brief_id
            body["ok"] = True
            self._status["last_brief_id"] = brief_id
            return body
        except Exception as exc:
            logger.warning("reflection_run_failed", error=str(exc))
            self._status["last_error"] = str(exc)
            raise
        finally:
            finished = datetime.now(UTC)
            self._status.update(
                {
                    "running": False,
                    "phase": None,
                    "last_finished_at": finished.isoformat(),
                    "last_duration_seconds": (finished - start).total_seconds(),
                }
            )

    async def _phase(
        self,
        name: str,
        fn: Any,
        errors: list[dict[str, str]],
        fallback: Any,
        *args: Any,
    ) -> Any:
        try:
            return await fn(*args)
        except Exception as exc:
            # Always log type+repr so empty-str exceptions (httpx.ReadTimeout etc.)
            # don't silently disappear from the log.
            error_repr = f"{type(exc).__name__}: {exc!s}".strip().rstrip(":")
            logger.warning(
                "reflection_phase_failed",
                phase=name,
                error=str(exc),
                error_type=type(exc).__name__,
                error_repr=error_repr,
            )
            errors.append({"phase": name, "error": error_repr})
            return fallback

    async def _gather_evidence(self) -> dict[str, Any]:
        events = await self.store.list_recent_events(window_hours=24)
        activity = await self._read_stream("events.activity", count=200)
        dismissals = await self._read_dismissals()
        return {"events": events, "activity": activity, "dismissals": dismissals}

    async def _self_audit(self, evidence: dict[str, Any]) -> dict[str, Any]:
        totals: dict[str, int] = defaultdict(int)
        errors: dict[str, int] = defaultdict(int)
        no_capability_misses: list[dict[str, Any]] = []
        for event in evidence.get("activity", []):
            agent = str(event.get("agent") or "unknown")
            capability = str(event.get("capability") or "unknown")
            key = f"{agent}.{capability}"
            totals[key] += 1
            status = str(event.get("status") or "")
            error_text = str(event.get("error") or event.get("reply") or "").lower()
            if status == "error":
                errors[key] += 1
            if "no capability" in error_text or "don't have a capability" in error_text:
                no_capability_misses.append(event)
        for event in evidence.get("events", []):
            haystack = _json_compact(event, limit=1500).lower()
            if "no capability" in haystack or "don't have a capability" in haystack:
                no_capability_misses.append(event)
        capability_error_rates = [
            {
                "capability": key,
                "errors": errors[key],
                "total": total,
                "error_rate": round(errors[key] / total, 3) if total else 0.0,
            }
            for key, total in totals.items()
        ]
        capability_error_rates.sort(key=lambda row: row["error_rate"], reverse=True)
        dismissed = [
            row
            for row in await self.store.list_proposals(status="dismissed", limit=50)
            if str(row.get("kind") or "").endswith("inference")
        ]
        return {
            "capability_error_rates": capability_error_rates,
            "no_capability_miss_count": len(no_capability_misses),
            "no_capability_misses": no_capability_misses[:20],
            "dismissed_inferences": dismissed,
        }

    async def _knowledge_gaps(self) -> list[dict[str, str]]:
        profile = await self.store.list_profile()
        present = {str(row.get("key")) for row in profile}
        return [
            {"key": key, "description": description}
            for key, description in USER_PROFILE_TEMPLATE.items()
            if key not in present
        ]

    async def _pattern_mining(self, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        by_hour: dict[int, list[int]] = defaultdict(list)
        summaries: dict[int, list[str]] = defaultdict(list)
        for event in evidence.get("events", []):
            ts = _parse_ts(event.get("ts"))
            if ts is None:
                continue
            try:
                event_id = int(event.get("id"))
            except (TypeError, ValueError):
                continue
            by_hour[ts.hour].append(event_id)
            summary = str(event.get("summary") or "").strip()
            if summary:
                summaries[ts.hour].append(summary)
        patterns = [
            {
                "hour": hour,
                "count": len(event_ids),
                "event_ids": event_ids,
                "examples": summaries.get(hour, [])[:3],
            }
            for hour, event_ids in by_hour.items()
            if len(event_ids) > 3
        ]
        patterns.sort(key=lambda row: row["count"], reverse=True)
        return patterns

    # ──────────────────────────────────────────────────────────────────
    # Capability-gap mining: read every unresolved capability_gap row,
    # cluster by domain (regex on user_text), and produce a structured
    # code_change proposal per cluster. This is the self-improvement
    # loop: failures recorded during the day become tool proposals
    # overnight.
    # ──────────────────────────────────────────────────────────────────

    # Domain regex for clustering. Order matters — first match wins.
    # All patterns handle singular/plural via `s?` because user requests
    # are written in natural English ("open the blinds", "play songs").
    _GAP_DOMAIN_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
        ("climate", re.compile(r"\b(thermostats?|temperatures?|ac|heat(er|ing)?s?|hvac|cool(er|ing)?s?|warm(er)?s?)\b", re.IGNORECASE)),
        ("cover", re.compile(r"\b(blinds?|curtains?|shades?|garages?|covers?|shutters?)\b", re.IGNORECASE)),
        ("media_player", re.compile(r"\b(musics?|songs?|tvs?|movies?|spotify|youtube|netflix|volumes?|mute|pause|skip)\b", re.IGNORECASE)),
        ("fan", re.compile(r"\bfans?\b", re.IGNORECASE)),
        ("lock", re.compile(r"\b(locks?|unlocks?|deadbolts?|door\s+(?:lock|secure))\b", re.IGNORECASE)),
        ("vacuum", re.compile(r"\b(vacuum(?:s|ed|ing)?|roomba|robots?|clean(?:s|ed|ing)?)\b", re.IGNORECASE)),
        ("notification", re.compile(r"\b(reminds?|notif(?:y|ies|ication)|alerts?|tell\s+me|let\s+me\s+know)\b", re.IGNORECASE)),
        ("status_query", re.compile(r"\b(status|what'?s|is\s+the|are\s+the|how\s+many)\b", re.IGNORECASE)),
    ]

    def _classify_gap_domain(self, user_text: str) -> str:
        for label, pat in self._GAP_DOMAIN_PATTERNS:
            if pat.search(user_text):
                return label
        return "other"

    async def _mine_capability_gaps(self) -> list[dict[str, Any]]:
        """Read unresolved capability_gaps, cluster by domain, and ask
        the reasoner to draft a code_change proposal per cluster.

        Returns the list of cluster summaries (each with linked
        proposal_id when successfully filed). This stays as a
        first-class field in the brief body so the user can see what
        the system noticed about its own limits."""
        if self.gap_store is None:
            return []

        try:
            gaps = await self.gap_store.list_unresolved(limit=200)
        except Exception as exc:
            logger.warning("mine_capability_gaps_list_failed", error=str(exc))
            return []

        if not gaps:
            return []

        # Cluster
        clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for gap in gaps:
            domain = self._classify_gap_domain(str(gap.get("user_text") or ""))
            clusters[domain].append(gap)

        # Cap at the 5 largest clusters to keep prompt cost predictable.
        # A single cluster with 20 examples is more useful than 10
        # clusters with 2 each — the LLM needs repetition to spot
        # the real pattern.
        ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]

        results: list[dict[str, Any]] = []
        for domain, cluster_gaps in ranked:
            try:
                proposal = await self._draft_gap_proposal(domain, cluster_gaps)
            except Exception as exc:
                logger.warning(
                    "mine_capability_gaps_draft_failed",
                    domain=domain,
                    error=str(exc),
                )
                results.append({
                    "domain": domain,
                    "gap_count": len(cluster_gaps),
                    "proposal_id": None,
                    "error": str(exc),
                })
                continue

            if proposal is None:
                results.append({
                    "domain": domain,
                    "gap_count": len(cluster_gaps),
                    "proposal_id": None,
                    "skipped_reason": "draft_returned_none",
                })
                continue

            # Insert the proposal via the existing store, then mark
            # every clustered gap as resolved pointing to it.
            proposal_id = await self._save_gap_proposal(domain, cluster_gaps, proposal)
            for gap in cluster_gaps:
                try:
                    await self.gap_store.mark_resolved(
                        gap["id"],
                        proposal_id=proposal_id,
                        note=f"clustered into {domain} proposal",
                    )
                except Exception as exc:
                    logger.warning(
                        "mine_capability_gaps_mark_resolved_failed",
                        gap_id=gap.get("id"),
                        error=str(exc),
                    )
            results.append({
                "domain": domain,
                "gap_count": len(cluster_gaps),
                "proposal_id": proposal_id,
                "title": proposal.get("title"),
            })

        return results

    async def _draft_gap_proposal(
        self,
        domain: str,
        cluster_gaps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Send a focused prompt to the reasoner for one cluster of
        gaps. Returns a proposal dict ready for the reflection_store,
        or None if the LLM refused / returned nonsense."""
        # Compact evidence: just the user_text and failure_reason from
        # each gap, capped so we stay under the model's effective
        # context budget.
        evidence_lines = []
        for gap in cluster_gaps[:30]:  # 30 examples is plenty
            evidence_lines.append(
                f"  - [{gap.get('failure_reason')}] {(gap.get('user_text') or '')[:200]}"
            )
        evidence_block = "\n".join(evidence_lines)

        system = (
            "You analyse capability gaps in a local home AI assistant — "
            "user requests the system could not route to a real tool. "
            "Given a CLUSTER of related gaps, propose ONE structured code "
            "change (a new tool, refactor of an existing tool, or routing "
            "fix) that would resolve the pattern.\n\n"
            "Reply with ONE JSON object, no prose, no code fences:\n"
            '{\n'
            '  "title": "<5-12 word title starting with a verb>",\n'
            '  "rationale": "<2-4 sentences citing the specific evidence>",\n'
            '  "proposed_change_kind": "new_tool|tool_refactor|routing_fix",\n'
            '  "proposed_tool_spec": {\n'
            '    "tool_id": "<verb_noun snake_case>",\n'
            '    "description": "<one line>",\n'
            '    "inputs": {"<param>": "<type>"}\n'
            '  },\n'
            '  "confidence": <0.0-1.0>,\n'
            '  "impact_estimate": "<one sentence on user benefit>"\n'
            "}\n"
            "Rules:\n"
            "- Title must be actionable: 'Add climate_set_temperature tool' not 'Climate issues'\n"
            "- Rationale MUST quote at least one specific user_text from the cluster\n"
            "- Be honest: if the cluster looks like noise (1 example, vague text), "
            "return {\"title\": \"\", \"confidence\": 0.0} and we'll skip it\n"
        )
        user = (
            f"DOMAIN: {domain}\n"
            f"GAP COUNT: {len(cluster_gaps)}\n\n"
            f"EVIDENCE (user requests that failed):\n{evidence_block}\n\n"
            "Propose the code change now."
        )

        try:
            response = await self.llm.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # Gap-clustering uses the reasoner (qwen36-moe-32k) with
                # thinking enabled. Same family as the router/default,
                # ~9 GB resident, real reasoning upgrade over the 8B
                # without the MoE/Vulkan deadlock risk that took
                # qwen36-moe-32k out of rotation on Strix Halo.
                model=self.reasoner_model,
                response_format="json",
                think=True,
                timeout=300.0,
            )
        except Exception as exc:
            logger.warning(
                "gap_proposal_llm_failed",
                domain=domain,
                error=f"{type(exc).__name__}: {exc!s}",
            )
            return None

        content = (response.get("message") or {}).get("content") or ""
        try:
            parsed = _extract_json(content)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "gap_proposal_bad_json",
                domain=domain,
                error=str(exc),
                content_preview=content[:200],
            )
            return None

        if not isinstance(parsed, dict):
            return None
        title = str(parsed.get("title") or "").strip()
        confidence = _clamp_confidence(parsed.get("confidence", 0.0))
        if not title or confidence < 0.4:
            logger.info(
                "gap_proposal_low_confidence_or_empty",
                domain=domain,
                title=title,
                confidence=confidence,
            )
            return None
        return parsed

    async def _save_gap_proposal(
        self,
        domain: str,
        cluster_gaps: list[dict[str, Any]],
        proposal: dict[str, Any],
    ) -> int | None:
        """Persist a gap-derived proposal as a code_change in
        reflection_store. Returns the proposal id."""
        # Build a rationale that combines the LLM's reasoning with hard
        # evidence (gap ids) so reviewers can audit.
        gap_ids = [int(g["id"]) for g in cluster_gaps if g.get("id") is not None]
        llm_rationale = str(proposal.get("rationale") or "").strip()
        full_rationale = (
            f"{llm_rationale}\n\n"
            f"Evidence: {len(cluster_gaps)} capability_gap rows in domain "
            f"'{domain}'. Gap ids: {gap_ids[:20]}"
        )
        if isinstance(proposal.get("proposed_tool_spec"), dict):
            spec_text = json.dumps(proposal["proposed_tool_spec"], ensure_ascii=False)
            full_rationale += f"\n\nProposed tool spec:\n{spec_text}"

        try:
            proposal_id = await self.store.add_proposal(
                kind="code_change",
                title=str(proposal.get("title")),
                rationale=full_rationale,
                evidence_event_ids=[],  # gaps aren't events, so leave empty
                confidence=_clamp_confidence(proposal.get("confidence", 0.5)),
                cost_estimate="small",
                impact_estimate=str(proposal.get("impact_estimate") or "Reduces fabricated replies"),
            )
        except Exception as exc:
            logger.warning(
                "gap_proposal_save_failed",
                domain=domain,
                error=str(exc),
            )
            return None
        return proposal_id

    # ──────────────────────────────────────────────────────────────────
    # Nightly proposal refinement: the 35B has hours of idle time
    # during the night. Use it to take rough proposals filed by the
    # day-time router/escalator (with limited 8B context) and produce
    # much better versions — sharper titles, focused evidence, draft
    # tool specs grounded in the actual HA entity catalog.
    # ──────────────────────────────────────────────────────────────────

    async def _refine_proposals(self) -> list[dict[str, Any]]:
        """Find pending code_change proposals that haven't been refined
        and send each through the 35B for deep reprocessing.

        Strategy:
        - List unrefined pending code_change proposals (last 7 days)
        - For each, gather HA context (entity catalog)
        - Send to reasoner with a "improve this proposal" prompt
        - Apply the refinement via store.refine_proposal

        Returns a list of {proposal_id, original_title, new_title,
        changed} summaries for the morning brief."""
        try:
            candidates = await self.store.list_unrefined_code_change_proposals(
                max_age_days=7, limit=20
            )
        except Exception as exc:
            logger.warning("refine_proposals_list_failed", error=str(exc))
            return []

        if not candidates:
            return []

        # Gather HA entity catalog ONCE per nightly run — this is the
        # context the 8B router doesn't have. Truncate for prompt
        # safety (we don't need every entity, just enough to ground
        # naming patterns like "han_" → vehicle).
        entity_catalog = await self._compact_entity_catalog()

        results: list[dict[str, Any]] = []
        for proposal in candidates:
            proposal_id = proposal.get("id")
            try:
                refined = await self._refine_single_proposal(proposal, entity_catalog)
            except Exception as exc:
                logger.warning(
                    "refine_single_proposal_failed",
                    proposal_id=proposal_id,
                    error=str(exc),
                )
                refined = None

            if refined is None:
                results.append({
                    "proposal_id": proposal_id,
                    "original_title": proposal.get("title"),
                    "changed": False,
                    "skipped_reason": "no_refinement_produced",
                })
                continue

            ok = await self.store.refine_proposal(
                proposal_id,
                new_title=refined.get("title"),
                new_rationale=refined.get("rationale"),
                new_confidence=refined.get("confidence"),
                refinement_notes=refined.get("notes"),
            )
            results.append({
                "proposal_id": proposal_id,
                "original_title": proposal.get("title"),
                "new_title": refined.get("title"),
                "changed": ok,
            })
            if ok:
                logger.info(
                    "proposal_refined",
                    proposal_id=proposal_id,
                    original_title=proposal.get("title"),
                    new_title=refined.get("title"),
                )
        return results

    async def _compact_entity_catalog(self) -> str:
        """Build a compact text summary of the HA entity catalog for
        the reasoner. Groups by area + domain. Used as grounding
        context so the 35B can recognize naming patterns ('han_*' is
        a vehicle's sensors) and propose tools that reference real
        entities, not invented ones.

        Capped tightly at ~80 entities total because larger catalogs
        push the 35B prompt past comfortable processing time
        (refinement should be <2 min, not 10+ min)."""
        from .registry import CapabilityRegistry  # noqa: F401 — used reflectively
        try:
            result = await self.registry.dispatch(
                "home_automation",
                "list_entities",
                {"include_unavailable": False},
            )
        except Exception as exc:
            logger.warning("refine_compact_catalog_failed", error=str(exc))
            return "(entity catalog unavailable)"

        payload = result
        if isinstance(result, dict) and "result" in result:
            payload = result["result"]
        by_area = (payload or {}).get("by_area") or {}
        # Compact: one line per entity, max 80 total. Within each area
        # we sort entity_ids alphabetically so the same area appears
        # in deterministic order across runs.
        lines = []
        total = 0
        for area, ents in sorted(by_area.items()):
            if total >= 80:
                lines.append(f"  (truncated: {sum(len(v) for v in by_area.values()) - total} more entities not shown)")
                break
            sorted_ents = sorted(ents, key=lambda e: e.get("entity_id") or "")
            lines.append(f"# {area}:")
            for e in sorted_ents:
                if total >= 80:
                    break
                eid = e.get("entity_id") or ""
                name = e.get("name") or ""
                lines.append(f"  {eid}  ({name})" if name else f"  {eid}")
                total += 1
        return "\n".join(lines)

    async def _refine_single_proposal(
        self,
        proposal: dict[str, Any],
        entity_catalog: str,
    ) -> dict[str, Any] | None:
        """Send one proposal to the reasoner for refinement. Returns
        a dict with title/rationale/confidence/notes, or None if the
        model refused or returned nonsense."""
        original_title = proposal.get("title") or ""
        original_rationale = proposal.get("rationale") or ""
        original_confidence = float(proposal.get("confidence") or 0.0)

        system = (
            "You are the nightly refinement engine for capability_gap "
            "proposals in a local home AI assistant. A daytime tool "
            "filed a rough proposal based on limited context. Your "
            "job: produce a CLEANER, MORE FOCUSED version with the "
            "full HA entity catalog at your disposal.\n\n"
            "Common improvements to make:\n"
            "- TITLE: replace generic ('Add sensor-status tool') with "
            "  specific ('Add ev_status tool for BYD HAN').\n"
            "- EVIDENCE: when the original lists every battery sensor "
            "  in the house but the user clearly meant the EV, narrow "
            "  to the relevant subset using the catalog to identify "
            "  which entities belong to the device.\n"
            "- TOOL SPEC: provide concrete inputs/outputs and "
            "  reference the actual entity_ids the tool would query.\n"
            "- DEDUPLICATION: if multiple unrefined proposals are "
            "  about the same gap (you'll see them in sequence), say "
            "  so in the notes so reviewers can consolidate.\n\n"
            "Reply with ONE JSON object — no prose, no code fences:\n"
            '{\n'
            '  "title": "<5-12 word title, starts with verb>",\n'
            '  "rationale": "<2-6 paragraph reasoning grounded in real entity_ids>",\n'
            '  "confidence": <0.0-1.0, raise if grounded, lower if speculative>,\n'
            '  "notes": "<one sentence summarizing what you changed>"\n'
            "}\n\n"
            "If the original proposal is already good (specific title, "
            "focused evidence), return your refinement unchanged but "
            "set notes='no improvement needed'. Don't manufacture "
            "changes."
        )

        user = (
            f"ORIGINAL PROPOSAL #{proposal.get('id')}:\n"
            f"Title: {original_title}\n"
            f"Confidence: {original_confidence}\n"
            f"Cost: {proposal.get('cost_estimate')}\n"
            f"Impact: {proposal.get('impact_estimate')}\n"
            f"Rationale:\n{original_rationale}\n\n"
            f"HA ENTITY CATALOG (for grounding):\n{entity_catalog}\n\n"
            "Now produce the refined JSON."
        )

        try:
            response = await self.llm.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # Refinement runs the reasoner (qwen36-moe-32k) with thinking
                # enabled — the bigger model gives tighter rationale +
                # better entity-narrowing on Saeed's Strix Halo (~9 GB
                # resident is well within memory budget now that the
                # 35B is gone).
                model=self.reasoner_model,
                response_format="json",
                think=True,
                timeout=300.0,
            )
        except Exception as exc:
            logger.warning(
                "refine_proposal_llm_failed",
                proposal_id=proposal.get("id"),
                error=f"{type(exc).__name__}: {exc!s}",
            )
            return None

        content = (response.get("message") or {}).get("content") or ""
        try:
            parsed = _extract_json(content)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "refine_proposal_bad_json",
                proposal_id=proposal.get("id"),
                error=str(exc),
                content_preview=content[:200],
            )
            return None
        if not isinstance(parsed, dict):
            return None

        # Validate fields
        title = str(parsed.get("title") or "").strip() or original_title
        rationale = str(parsed.get("rationale") or "").strip() or original_rationale
        try:
            confidence = max(0.0, min(float(parsed.get("confidence", original_confidence)), 1.0))
        except (TypeError, ValueError):
            confidence = original_confidence
        notes = str(parsed.get("notes") or "").strip()[:500]

        # If the refinement is identical to the original AND notes say
        # so, that's fine — still mark refined so we don't reprocess.
        # But if the title/rationale shrank dramatically, refuse the
        # refinement (likely a hallucination/truncation).
        if len(rationale) < max(50, len(original_rationale) // 4):
            logger.info(
                "refine_proposal_rejected_too_short",
                proposal_id=proposal.get("id"),
                original_len=len(original_rationale),
                refined_len=len(rationale),
            )
            return None

        return {
            "title": title,
            "rationale": rationale,
            "confidence": confidence,
            "notes": notes or "refined by 8B",
        }

    async def _synthesize_nightly_brief(
        self,
        *,
        refined_proposals: list[dict[str, Any]],
        gap_proposals: list[dict[str, Any]],
        proposals: list[dict[str, Any]],
        health_summary: dict[str, Any],
        patterns: list[dict[str, Any]],
        correlations: list[dict[str, Any]],
        knowledge_gaps: list[dict[str, str]],
    ) -> dict[str, Any]:
        """ONE 35B call per nightly run, after every other phase has run.

        The 35B (qwen36-moe-32k) deadlocks under sustained back-to-back
        calls (Vulkan/RADV: GPU sits at 0% busy while requests hang at
        the network layer). But a single-shot call works fine — the
        deadlock is specific to sustained load.

        So we use it where it matters most: synthesizing everything
        that just happened into ONE coherent paragraph that becomes
        the headline of the morning brief. Quality over quantity:
        one big think, not 50 small ones.

        Returns a dict with `headline` (1-2 sentence summary) and
        `attention` (what most deserves the user's eyes tomorrow).
        Returns an empty dict if the call fails — the brief still
        renders fine without the synthesis."""

        def _trim(items: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
            """Take top N items, stripping verbose fields. The 35B has
            generous context but the synthesis prompt has a budget."""
            out: list[dict[str, Any]] = []
            for item in items[:n]:
                if not isinstance(item, dict):
                    continue
                out.append({
                    k: v for k, v in item.items()
                    if k in {
                        "id", "kind", "title", "confidence",
                        "rationale", "notes", "changed",
                        "key", "description",
                    } and v is not None
                })
            return out

        # If literally nothing happened, skip the call — no need to
        # burn a 35B invocation on emptiness.
        total_signals = (
            len(refined_proposals) + len(gap_proposals) + len(proposals)
            + len(patterns) + len(correlations) + len(knowledge_gaps)
        )
        if total_signals == 0:
            logger.info("reflection_synthesis_skipped_no_signals")
            return {}

        system = (
            "You are the nightly synthesis engine for a local home AI "
            "assistant. Every other reflection phase has just finished. "
            "Your job: read ALL the night's outputs and produce ONE "
            "paragraph the user should read first thing in the morning.\n\n"
            "Focus on:\n"
            "- What patterns emerged that cut across multiple phases\n"
            "- What the system genuinely LEARNED tonight (not just listed)\n"
            "- What ONE thing most deserves the user's attention tomorrow\n"
            "- Connect refined proposals to gap_proposals when they're "
            "  about the same underlying capability gap\n\n"
            "Reply with ONE JSON object — no prose, no code fences:\n"
            '{\n'
            '  "headline": "<1-2 sentence summary, plain English>",\n'
            '  "attention": "<the single most important thing for tomorrow>",\n'
            '  "patterns": "<cross-phase insights worth noting, or empty>"\n'
            "}\n\n"
            "Be specific, not generic. Reference actual proposal titles "
            "and entity names. If nothing notable happened, say so "
            "honestly — don't manufacture importance."
        )

        payload = {
            "refined_proposals": _trim(refined_proposals, 20),
            "capability_gap_proposals": _trim(gap_proposals, 10),
            "other_proposals": _trim(proposals, 20),
            "knowledge_gaps": _trim(knowledge_gaps, 10),
            "hourly_patterns": patterns[:10],
            "correlations": correlations[:10],
            "health_summary": health_summary,
        }
        user = (
            "Tonight's reflection outputs:\n"
            f"{_json_compact(payload)}\n\n"
            "Now produce the synthesis JSON."
        )

        # Synthesis now uses the reasoner (qwen36-moe-32k). The historical
        # 35B-on-Strix-Halo deadlock that forced us to the 8B is gone:
        # the 14B fits in ~9 GB resident leaving plenty of headroom
        # alongside the 8B + 0.6B + bge-m3 (total ~30 GB / 64 GB).
        # Slightly better headline quality than 8B with no reliability
        # tax.
        synthesis_model = self.reasoner_model

        # Still attempt the unload — cheap, leaves a cleaner GPU state
        # for the synthesis call, and the helper is defensive against
        # the keep target being already-loaded. Best-effort: failures
        # don't block the chat call.
        try:
            await self._unload_other_ollama_models(keep=synthesis_model)
            await asyncio.sleep(1)
        except Exception as exc:
            logger.warning(
                "reflection_synthesis_unload_failed",
                error=f"{type(exc).__name__}: {exc!s}",
            )

        try:
            # Single-shot synthesis. Thinking enabled so a smaller
            # model produces a better-organized brief; timeout is
            # generous because the nightly window has hours.
            response = await self.llm.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=synthesis_model,
                response_format="json",
                think=True,
                timeout=600.0,
            )
        except Exception as exc:
            logger.warning(
                "reflection_synthesis_llm_failed",
                error=f"{type(exc).__name__}: {exc!s}",
                model=synthesis_model,
            )
            return {}

        content = (response.get("message") or {}).get("content") or ""
        try:
            parsed = _extract_json(content)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "reflection_synthesis_bad_json",
                error=str(exc),
                content_preview=content[:200],
            )
            return {}
        if not isinstance(parsed, dict):
            return {}

        headline = str(parsed.get("headline") or "").strip()
        attention = str(parsed.get("attention") or "").strip()
        patterns_note = str(parsed.get("patterns") or "").strip()

        # Reject obviously empty synthesis (model returned valid JSON
        # but no actual content).
        if not headline and not attention:
            logger.info("reflection_synthesis_empty")
            return {}

        return {
            "headline": headline,
            "attention": attention,
            "patterns": patterns_note,
            "model": synthesis_model,
        }

    async def _unload_other_ollama_models(self, *, keep: str | None = None) -> None:
        """Force-unload every loaded Ollama model except ``keep``.

        Used right before the 35B synthesis call to free GPU memory
        and dodge the Vulkan/RADV deadlock. On this hardware, asking
        Ollama to load the 35B while smaller models (8B from the
        refinement phase) are still resident causes the GPU to hang
        at 0% busy while the request times out. Force-unloading via
        keep_alive=0 leaves the GPU clean so the 35B loads fresh.

        Failures are logged but don't raise — better to attempt the
        synthesis call against a not-fully-clean GPU than to skip
        synthesis entirely because the unload probe failed.
        """
        ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{ollama_url}/api/ps")
                resp.raise_for_status()
                data = resp.json() or {}
                loaded = data.get("models") or []
                names = [
                    str(m.get("name") or m.get("model") or "")
                    for m in loaded
                    if isinstance(m, dict)
                ]
                names = [n for n in names if n and n != keep]
                if not names:
                    logger.info("ollama_unload_nothing_to_do", keep=keep)
                    return
                logger.info("ollama_unload_starting", models=names, keep=keep)
                for name in names:
                    try:
                        # keep_alive=0 + empty prompt → unload, no
                        # generation. Ollama returns near-instantly.
                        await client.post(
                            f"{ollama_url}/api/generate",
                            json={
                                "model": name,
                                "prompt": "",
                                "keep_alive": 0,
                                "stream": False,
                            },
                            timeout=30,
                        )
                        logger.info("ollama_model_unloaded", model=name)
                    except Exception as exc:
                        logger.warning(
                            "ollama_unload_failed",
                            model=name,
                            error=f"{type(exc).__name__}: {exc!s}",
                        )
        except Exception as exc:
            logger.warning(
                "ollama_unload_probe_failed",
                error=f"{type(exc).__name__}: {exc!s}",
            )

    async def _health_summary(self) -> dict[str, Any]:
        store = getattr(self, "health_store", None)
        if store is None:
            return {}
        try:
            sleep_rows, step_rows = await asyncio.gather(
                store.aggregate_daily("sleep_asleep", days=7),
                store.aggregate_daily("steps", days=7),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reflection_health_summary_failed", error=str(exc))
            return {}
        return {
            "sleep_asleep_7d": sleep_rows,
            "steps_7d": step_rows,
        }

    async def _correlations(self) -> list[dict[str, Any]]:
        """Run the cross-source correlation engine. Each insight joins data
        across HealthKit + presence + appliances to produce a "I noticed:"
        line for the morning brief. Pool may be absent in tests — skip
        silently."""
        pool = getattr(self, "pool", None)
        if pool is None:
            return []
        try:
            from .correlations import correlate_recent
            return await correlate_recent(pool, lookback_days=14)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection_correlations_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

    async def _generate_proposals(
        self,
        evidence: dict[str, Any],
        audit: dict[str, Any],
        gaps: list[dict[str, str]],
        patterns: list[dict[str, Any]],
        health_summary: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        capabilities = []
        try:
            capabilities = self.registry.list_capabilities()
        except Exception as exc:
            logger.warning("reflection_capability_snapshot_failed", error=str(exc))
        prompt = {
            "recent_events": evidence.get("events", [])[:60],
            "activity_stream": evidence.get("activity", [])[:60],
            "dismissals": evidence.get("dismissals", [])[:30],
            "self_audit": audit,
            "knowledge_gaps": gaps,
            "hourly_patterns": patterns,
            "health_summary": health_summary or {},
            "capabilities": capabilities[:80],
        }
        response = await self._chat_with_fallback(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _json_compact(prompt)},
            ]
        )
        content = str(
            (response.get("message") or {}).get("content") or response.get("response") or "{}"
        )
        parsed = _extract_json(content)
        raw_proposals = parsed.get("proposals", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(raw_proposals, list):
            return []
        return [proposal for item in raw_proposals if (proposal := self._normalize_proposal(item))]

    async def _reasoner_available(self, model: str) -> bool:
        """Check (cached) whether ``model`` is pulled into the local Ollama.

        Skips an entire LLM call if the model isn't available, saving the
        full OLLAMA_TIMEOUT_SECONDS per reflection run when a user has set
        REASONER_MODEL to a model they haven't pulled. Cached for the
        process lifetime; restart the orchestrator after pulling new models.
        """
        if self._available_models is not None:
            return model in self._available_models
        ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{ollama_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("reflection_model_probe_failed", error=str(exc))
            # Be optimistic: assume the model exists rather than skip the call.
            self._available_models = set()
            return True
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            self._available_models = set()
            return True
        names: set[str] = set()
        for m in models:
            if isinstance(m, dict) and m.get("name"):
                names.add(str(m["name"]))
        self._available_models = names
        return model in names

    async def _chat_with_fallback(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        reasoner_model = os.environ.get("REASONER_MODEL", self.reasoner_model)
        fallback_model = os.environ.get("DEFAULT_MODEL", self.fallback_model)

        # Pre-check: skip the reasoner model entirely if Ollama doesn't have
        # it pulled. Without this, an HA without the big model wastes our
        # entire OLLAMA_TIMEOUT_SECONDS (default 180s) on EACH reflection.
        # Cached on the instance after the first probe.
        if not await self._reasoner_available(reasoner_model):
            logger.info(
                "reflection_reasoner_skipped_not_pulled",
                model=reasoner_model,
                fallback=fallback_model,
            )
            return await self.llm.chat(
                messages=messages,
                model=fallback_model,
                temperature=0.1,
                response_format="json",
                # Nightly path → enable thinking so the smaller model
                # produces tighter proposals (fewer duplicates, better
                # dedup-vs-existing reasoning).
                think=True,
            )

        # If reasoner == fallback, no point retrying — short-circuit so we
        # don't waste 2x OLLAMA_TIMEOUT_SECONDS on the SAME failing model.
        if reasoner_model == fallback_model:
            return await self.llm.chat(
                messages=messages,
                model=reasoner_model,
                temperature=0.1,
                response_format="json",
                think=True,
            )

        try:
            return await self.llm.chat(
                messages=messages,
                model=reasoner_model,
                temperature=0.1,
                response_format="json",
                think=True,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "reflection_reasoner_unavailable",
                model=reasoner_model,
                fallback=fallback_model,
                error=f"{type(exc).__name__}: {exc!s}",
            )
            return await self.llm.chat(
                messages=messages,
                model=fallback_model,
                temperature=0.1,
                response_format="json",
                think=True,
            )
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code != 404:
                raise
            logger.warning(
                "reflection_reasoner_404",
                model=reasoner_model,
                fallback=fallback_model,
                error=str(exc),
            )
            return await self.llm.chat(
                messages=messages,
                model=fallback_model,
                temperature=0.1,
                response_format="json",
                think=True,
            )

    def _normalize_proposal(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind") or "").strip()
        title = str(item.get("title") or "").strip()
        if kind not in PROPOSAL_KINDS or not title:
            return None
        evidence_event_ids = _ints(item.get("evidence_event_ids"))
        evidence_keys = _strings(item.get("evidence_keys"))
        if not evidence_event_ids and not evidence_keys:
            logger.warning("reflection_proposal_missing_evidence", title=title)
            return None
        rationale = str(item.get("rationale") or "").strip()
        if kind == "code_change" and not _has_concrete_code_change_evidence(
            rationale, evidence_event_ids
        ):
            # Most code_change proposals from the LLM are speculative
            # ("add jitter", "cap retries", "add caching") with no actual
            # bug behind them — they cite ~3 events of normal traffic and
            # invent a 'might be a problem' rationale. Require at least 5
            # cited events AND a measurable-problem keyword in the
            # rationale, so we only persist code_change suggestions that
            # actually point to evidence of a real problem.
            logger.info(
                "reflection_proposal_filtered_speculative",
                title=title,
                evidence_count=len(evidence_event_ids),
                kind=kind,
            )
            return None
        return {
            "kind": kind,
            "title": title,
            "rationale": rationale,
            "evidence_event_ids": evidence_event_ids,
            "evidence_keys": evidence_keys,
            "confidence": _clamp_confidence(item.get("confidence")),
            "cost_estimate": str(item.get("cost_estimate") or "").strip() or None,
            "impact_estimate": str(item.get("impact_estimate") or "").strip() or None,
            "profile_key": str(item.get("profile_key") or "").strip() or None,
            "profile_value": item.get("profile_value"),
        }

    async def _apply_auto_confirm_rules(
        self, proposals: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        # Per-kind dismissal counts in the last 14 days. If the user has
        # rejected >= BACKOFF_DISMISSALS proposals of a given kind, we
        # stop emitting more of that kind for now (they'll resume once
        # rolling-window dismissals drop below the threshold). Same
        # 'closes the loop' pattern as user-correction memory in
        # auto_infer.
        signal_cache: dict[str, dict[str, int]] = {}
        for proposal in proposals:
            kind = str(proposal.get("kind") or "")
            evidence_count = len(proposal.get("evidence_event_ids") or []) + len(
                proposal.get("evidence_keys") or []
            )
            if kind not in signal_cache:
                try:
                    signal_cache[kind] = await self.store.proposal_dismissal_signal(
                        kind=kind, days=PROPOSAL_BACKOFF_DAYS
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reflection_proposal_signal_failed", error=str(exc))
                    signal_cache[kind] = {"dismissed": 0, "accepted": 0, "auto_confirmed": 0}
            signal = signal_cache[kind]
            if signal.get("dismissed", 0) >= PROPOSAL_BACKOFF_DISMISSALS and signal.get(
                "accepted", 0
            ) + signal.get("auto_confirmed", 0) == 0:
                logger.info(
                    "reflection_proposal_backoff",
                    kind=kind,
                    title=proposal.get("title"),
                    signal=signal,
                )
                continue
            # Auto-confirm safe inferences with reasonable backing. Tightened
            # in 2026-05: was habit_inference only, threshold 0.95 + 5
            # evidence — too strict (nothing ever qualified). Then 0.85 + 3
            # — but the LLM tends to land at 0.80 which leaves real
            # inferences on the floor. Now: 0.75 + 3, which auto-promotes
            # the typical "User likely watches TV around 20:30" kind of
            # signal while still gating obvious guesses (< 0.75).
            auto_confirm = (
                kind in {"habit_inference", "preference_inference", "routine_inference"}
                and float(proposal.get("confidence") or 0.0) >= 0.75
                and evidence_count >= 3
            )
            status = "auto_confirmed" if auto_confirm else "pending"
            proposal_id = await self.store.add_proposal(
                kind=str(proposal["kind"]),
                title=str(proposal["title"]),
                rationale=str(proposal.get("rationale") or ""),
                evidence_event_ids=proposal.get("evidence_event_ids") or [],
                confidence=float(proposal.get("confidence") or 0.0),
                cost_estimate=proposal.get("cost_estimate"),
                impact_estimate=proposal.get("impact_estimate"),
                status=status,
            )
            enriched = {**proposal, "id": proposal_id, "status": status}
            if auto_confirm:
                await self._write_auto_confirmed_profile(enriched)
            applied.append(enriched)
        return applied

    async def _write_auto_confirmed_profile(self, proposal: dict[str, Any]) -> None:
        """Promote an auto-confirmed proposal into user_profile + the typed
        knowledge tables. Delegates to the shared helper so /admin/proposals/
        {id}/accept and the bulk-accept paths get the SAME promotion behavior
        as auto-confirm — without this duplication, manual accepts silently
        skipped the typed-table write and /dashboard/about-you stayed empty."""
        from home_agents_sdk.proposal_promotion import promote_proposal_to_knowledge

        await promote_proposal_to_knowledge(
            proposal=proposal,
            reflection_store=self.store,
            knowledge_graph=getattr(self, "knowledge_graph", None),
        )

    def _build_brief_body(
        self,
        evidence: dict[str, Any],
        audit: dict[str, Any],
        gaps: list[dict[str, str]],
        patterns: list[dict[str, Any]],
        health_summary: dict[str, Any],
        proposals: list[dict[str, Any]],
        errors: list[dict[str, str]],
        *,
        correlations: list[dict[str, Any]] | None = None,
        gap_proposals: list[dict[str, Any]] | None = None,
        refined_proposals: list[dict[str, Any]] | None = None,
        nightly_synthesis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        yesterday = [
            {"id": row.get("id"), "ts": row.get("ts"), "summary": row.get("summary")}
            for row in evidence.get("events", [])[:12]
        ]
        questions = [
            proposal
            for proposal in proposals
            if proposal.get("status") == "pending"
            and proposal.get("kind")
            in {"habit_inference", "preference_inference", "routine_inference"}
        ]
        if not questions:
            questions = [
                {
                    "title": _phrase_gap_question(gap["key"]),
                    "kind": "knowledge_gap",
                    "evidence_keys": [gap["key"]],
                    "rationale": gap["description"],
                }
                for gap in gaps[:5]
            ]
        suggestions = [
            proposal
            for proposal in proposals
            if proposal.get("kind") in {"cleanup_action", "routine_inference"}
        ]
        code_wishlist = [
            proposal for proposal in proposals if proposal.get("kind") == "code_change"
        ]
        synthesis = dict(nightly_synthesis or {})
        # If synthesis produced a headline, promote it as the brief
        # summary — that's the whole point of running the 35B once.
        # Falls back to the heuristic _headline() when synthesis was
        # skipped/failed (e.g. no signals tonight or LLM error).
        summary = synthesis.get("headline") or self._headline(proposals, gaps, errors)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": summary,
            "yesterday": yesterday,
            "questions_for_you": questions,
            "suggestions_for_me": suggestions,
            "code_wishlist": code_wishlist,
            "capability_gap_proposals": list(gap_proposals or []),
            "refined_proposals": list(refined_proposals or []),
            "nightly_synthesis": synthesis,
            "proposals": proposals,
            "evidence": evidence,
            "self_audit": audit,
            "knowledge_gaps": gaps,
            "patterns": patterns,
            "health_summary": health_summary,
            "correlations": list(correlations or []),
            "errors": errors,
        }

    def _headline(
        self,
        proposals: list[dict[str, Any]],
        gaps: list[dict[str, str]],
        errors: list[dict[str, str]],
    ) -> str:
        # If we DO have proposals, lead with them — even if some phases failed.
        if proposals:
            auto = sum(1 for proposal in proposals if proposal.get("status") == "auto_confirmed")
            tail = " (some phases had errors — see diagnostics)" if errors else ""
            return (
                f"Reflection found {len(proposals)} improvement ideas, "
                f"with {auto} auto-confirmed.{tail}"
            )
        # No proposals + no errors → calm headline
        if not errors and not gaps:
            return "Reflection found no urgent gaps overnight."
        if not errors and gaps:
            return f"Reflection found {len(gaps)} profile gaps to ask about over time."
        # We have errors AND no proposals — surface what DID work instead of
        # the bland "partial data" headline.
        bits: list[str] = []
        if gaps:
            bits.append(f"{len(gaps)} profile gap(s) to learn")
        # The user's data — what we DO know — is more interesting than 'failure'
        failed_phases = [str(e.get("phase") or "?") for e in errors]
        if bits:
            return (
                "Reflection partially completed: "
                + ", ".join(bits)
                + f". Failed phases: {', '.join(failed_phases)}."
            )
        return (
            "Reflection couldn't generate proposals (failed: "
            + ", ".join(failed_phases)
            + "). Check diagnostics — usually means the LLM was slow."
        )

    async def _save_brief(self, body: dict[str, Any]) -> int:
        return await self.store.record_brief(summary=str(body.get("summary") or ""), body=body)

    async def _read_stream(self, stream: str, count: int) -> list[dict[str, Any]]:
        if self.redis is None:
            return []
        try:
            rows = await self.redis.xrevrange(stream, count=count)
        except Exception as exc:
            logger.warning("reflection_stream_read_failed", stream=stream, error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for message_id, fields in rows:
            raw_payload = fields.get("payload") if isinstance(fields, dict) else None
            payload = self._decode_payload(raw_payload)
            payload["stream_id"] = str(message_id)
            out.append(payload)
        return out

    async def _read_dismissals(self) -> list[dict[str, Any]]:
        if self.redis is None:
            return []
        dismissals: list[dict[str, Any]] = []
        for key in ("reflection:dismissals", "inbox:dismissals"):
            try:
                rows = await self.redis.lrange(key, 0, 100)
            except Exception as exc:
                logger.warning("reflection_dismissal_read_failed", key=key, error=str(exc))
                continue
            dismissals.extend(self._decode_payload(row) for row in rows)
        try:
            rows = await self.redis.lrange("policy:recent", 0, 100)
        except Exception:
            rows = []
        for row in rows:
            decoded = self._decode_payload(row)
            if decoded.get("decision") in {"dismiss", "suppress"}:
                dismissals.append(decoded)
        return dismissals[:100]

    def _decode_payload(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except ValueError:
                return {"raw": raw}
            return decoded if isinstance(decoded, dict) else {"value": decoded}
        if raw is None:
            return {}
        return {"raw": str(raw)}
