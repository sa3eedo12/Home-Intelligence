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


class NightlyReflector:
    def __init__(
        self,
        pool: Any | None,
        redis: Redis | None,
        llm: OllamaClient,
        registry: CapabilityRegistry,
        reasoner_model: str,
        fallback_model: str,
    ) -> None:
        self.pool = pool
        self.redis = redis
        self.llm = llm
        self.registry = registry
        # Default to qwen3:8b: ~5GB loads in seconds, generates a JSON
        # proposal in 30-90s. The bigger qwen3.6:35b-a3b (23GB) was the
        # historical default but it requires >24GB RAM/VRAM and the load
        # alone takes 60-120s, blowing past OLLAMA_TIMEOUT_SECONDS on
        # modest hardware. Users with the headroom can opt in with
        # REASONER_MODEL=qwen3.6:35b-a3b in their env.
        self.reasoner_model = reasoner_model or os.environ.get("REASONER_MODEL", "qwen3:8b")
        self.fallback_model = fallback_model or os.environ.get("DEFAULT_MODEL", "qwen3:8b")
        self.store = ReflectionStore(pool)
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
            self._status["phase"] = "health_summary"
            health_summary = await self._phase("health_summary", self._health_summary, errors, {})
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
            self._status["phase"] = "save_brief"
            body = self._build_brief_body(
                evidence,
                audit,
                gaps,
                patterns,
                health_summary,
                applied,
                errors,
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
                think=False,
            )

        # If reasoner == fallback, no point retrying — short-circuit so we
        # don't waste 2x OLLAMA_TIMEOUT_SECONDS on the SAME failing model.
        if reasoner_model == fallback_model:
            return await self.llm.chat(
                messages=messages,
                model=reasoner_model,
                temperature=0.1,
                response_format="json",
                think=False,
            )

        try:
            return await self.llm.chat(
                messages=messages,
                model=reasoner_model,
                temperature=0.1,
                response_format="json",
                think=False,
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
                think=False,
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
        return {
            "kind": kind,
            "title": title,
            "rationale": str(item.get("rationale") or "").strip(),
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
        for proposal in proposals:
            evidence_count = len(proposal.get("evidence_event_ids") or []) + len(
                proposal.get("evidence_keys") or []
            )
            # Auto-confirm safe inferences with reasonable backing. Tightened
            # in 2026-05: was habit_inference only, threshold 0.95 + 5
            # evidence — too strict (nothing ever qualified). Now: any
            # inference kind with confidence >= 0.85 and >= 3 evidence.
            # Habits/preferences/routines also get promoted to their typed
            # tables in _write_auto_confirmed_profile so the dashboard sees
            # them.
            kind = proposal.get("kind") or ""
            auto_confirm = (
                kind in {"habit_inference", "preference_inference", "routine_inference"}
                and float(proposal.get("confidence") or 0.0) >= 0.85
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
        key = proposal.get("profile_key") or (
            f"habits.{_slug(str(proposal.get('title') or 'habit'))}"
        )
        value = proposal.get("profile_value")
        if value is None:
            value = {
                "title": proposal.get("title"),
                "rationale": proposal.get("rationale"),
                "evidence_event_ids": proposal.get("evidence_event_ids") or [],
            }
        await self.store.upsert_profile(
            key=str(key),
            value=value,
            confidence=float(proposal.get("confidence") or 0.0),
            source=f"proposal:{proposal.get('id') or 'nightly_reflector'}",
        )
        # Also promote into the typed table the dashboard reads from. Without
        # this, the user_profile row exists but /dashboard/about-you shows an
        # empty Habits / Preferences / Routines tab forever.
        kg = getattr(self, "knowledge_graph", None)
        if kg is None:
            return
        kind = proposal.get("kind")
        try:
            if kind == "habit_inference":
                await kg.put_habit(
                    subject=str(proposal.get("title") or proposal.get("profile_key") or "habit"),
                    pattern={
                        "rationale": proposal.get("rationale"),
                        "value": proposal.get("profile_value"),
                        "evidence_event_ids": proposal.get("evidence_event_ids") or [],
                    },
                    frequency=str(proposal.get("frequency") or ""),
                    confidence=float(proposal.get("confidence") or 0.0),
                    source=f"proposal:{proposal.get('id') or 'nightly_reflector'}",
                )
            elif kind == "preference_inference":
                pref_key = proposal.get("profile_key") or _slug(
                    str(proposal.get("title") or "preference")
                )
                pref_value = proposal.get("profile_value") or {
                    "title": proposal.get("title"),
                    "rationale": proposal.get("rationale"),
                }
                await kg.put_preference(
                    key=str(pref_key),
                    value=pref_value,
                    confidence=float(proposal.get("confidence") or 0.0),
                    source=f"proposal:{proposal.get('id') or 'nightly_reflector'}",
                )
            elif kind == "routine_inference":
                steps = proposal.get("profile_value") or {
                    "rationale": proposal.get("rationale"),
                }
                # put_routine expects a list of steps; wrap the value if it
                # came in as a dict so we don't crash on bad LLM output.
                steps_list = steps if isinstance(steps, list) else [steps]
                await kg.put_routine(
                    name=str(proposal.get("title") or "routine"),
                    steps=steps_list,
                    schedule=proposal.get("schedule") or None,
                    source=f"proposal:{proposal.get('id') or 'nightly_reflector'}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection_promote_failed",
                kind=kind,
                title=proposal.get("title"),
                error=f"{type(exc).__name__}: {exc}",
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
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": self._headline(proposals, gaps, errors),
            "yesterday": yesterday,
            "questions_for_you": questions,
            "suggestions_for_me": suggestions,
            "code_wishlist": code_wishlist,
            "proposals": proposals,
            "evidence": evidence,
            "self_audit": audit,
            "knowledge_gaps": gaps,
            "patterns": patterns,
            "health_summary": health_summary,
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
