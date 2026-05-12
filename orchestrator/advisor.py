from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

from .registry import CapabilityRegistry
from .safety import SafetyPolicy

logger = get_logger("orchestrator.advisor")

SYSTEM_PROMPT = """You are the Home Intelligence daytime advisor.
Read recent observed home events and propose at most 3 low-interruption inbox cards.
Return ONLY compact JSON in this shape:
{
  "proposals": [
    {
      "title": "short inbox-card title",
      "rationale": "why this may help now",
      "agent": "exact agent id",
      "capability": "exact capability id",
      "inputs": {"argument": "value"},
      "evidence_event_ids": [1, 2],
      "confidence": 0.0,
      "cost_estimate": "none|tiny|small|medium",
      "impact_estimate": "short impact statement"
    }
  ]
}
Rules:
- Return 0-3 proposals.
- Prefer no proposal over a weak or repetitive suggestion.
- Never suggest locks, alarms, payments, purchases, deletes, resets, or irreversible actions.
- Every proposal should cite event ids when available.
"""

HERE = Path(__file__).resolve().parent


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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _local_dt(now: datetime, tz_name: str) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = UTC
    return now.astimezone(zone)


def _iter_entities(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        by_area = value.get("by_area")
        if isinstance(by_area, dict):
            for entities in by_area.values():
                yield from _iter_entities(entities)
        for key in ("items", "entities"):
            if isinstance(value.get(key), list):
                yield from _iter_entities(value[key])
        if "state" in value:
            yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield from _iter_entities(item)


class Advisor:
    def __init__(
        self,
        pool: Any | None,
        redis: Redis | None,
        llm: OllamaClient,
        registry: CapabilityRegistry,
        safety: SafetyPolicy,
        default_model: str,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.pool = pool
        self.redis = redis
        self.llm = llm
        self.registry = registry
        self.safety = safety
        self.default_model = default_model
        self.store = ReflectionStore(pool)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    async def run_once(self, window_hours: int = 6) -> dict[str, Any]:
        hours = self._bounded_hours(window_hours)
        events = await self.store.list_recent_events(window_hours=hours)
        observer_events = [
            event for event in events if str(event.get("agent") or "").startswith("observer.")
        ]
        events_for_prompt = (observer_events or events)[:100]
        dismissed = await self.store.list_proposals(status="dismissed", limit=20)
        context = await self._context()

        interrupt_reasons = [
            key
            for key in ("quiet_hours_active", "is_dinner_window", "is_tv_on", "calendar_busy")
            if context.get(key)
        ]
        if interrupt_reasons:
            body = {
                "type": "advisor",
                "status": "skipped",
                "reason": interrupt_reasons[0],
                "context": context,
                "proposals": [],
            }
            brief_id = await self._record_advisor_brief(
                f"Advisor skipped: {interrupt_reasons[0]}", body
            )
            return {
                "ok": True,
                "status": "skipped",
                "reason": interrupt_reasons[0],
                "context": context,
                "brief_id": brief_id,
                "proposals": [],
                "saved": 0,
                "dispatched": 0,
                "dropped": 0,
            }

        proposals = await self._generate_proposals(events_for_prompt, dismissed, context)
        if not proposals:
            return {
                "ok": True,
                "status": "ok",
                "context": context,
                "events_considered": len(events_for_prompt),
                "proposals": [],
                "saved": 0,
                "dispatched": 0,
                "dropped": 0,
            }

        accepted: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        dispatched = 0
        for proposal in sorted(
            proposals, key=lambda item: float(item.get("confidence") or 0.0), reverse=True
        ):
            if len(accepted) >= 3:
                dropped.append({"title": proposal.get("title"), "reason": "over_cap"})
                continue
            explanation = self.safety.explain(
                str(proposal["agent"]),
                str(proposal["capability"]),
                proposal.get("inputs") if isinstance(proposal.get("inputs"), dict) else {},
            )
            tier = explanation["tier"]
            if tier == "never":
                logger.info(
                    "advisor_dropped_never_action",
                    agent=proposal["agent"],
                    capability=proposal["capability"],
                    reason=explanation.get("reason"),
                )
                dropped.append({"title": proposal.get("title"), "reason": "never"})
                continue
            if tier == "auto":
                try:
                    dispatch_result = await self.registry.dispatch(
                        str(proposal["agent"]),
                        str(proposal["capability"]),
                        proposal.get("inputs") or {},
                    )
                except Exception as exc:
                    logger.warning("advisor_auto_dispatch_failed", error=str(exc))
                    dropped.append({"title": proposal.get("title"), "reason": "dispatch_failed"})
                    continue
                dispatched += 1
                proposal_id = await self._save_proposal(
                    kind="auto_action",
                    proposal=proposal,
                    explanation=explanation,
                    status="auto_confirmed",
                )
                accepted.append(
                    {
                        **proposal,
                        "id": proposal_id,
                        "tier": tier,
                        "status": "auto_confirmed",
                        "dispatch_result": dispatch_result,
                    }
                )
                continue

            proposal_id = await self._save_proposal(
                kind="suggested_action",
                proposal=proposal,
                explanation=explanation,
                status="pending",
            )
            accepted.append({**proposal, "id": proposal_id, "tier": tier, "status": "pending"})

        return {
            "ok": True,
            "status": "ok",
            "context": context,
            "events_considered": len(events_for_prompt),
            "proposals": accepted,
            "saved": len(accepted),
            "dispatched": dispatched,
            "dropped": len(dropped),
            "dropped_items": dropped,
        }

    def _bounded_hours(self, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 6
        return max(1, min(parsed, 24))

    async def _generate_proposals(
        self,
        events: list[dict[str, Any]],
        dismissed: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        dismissed_titles = {str(row.get("title") or "").strip().lower() for row in dismissed}
        dismissed_titles.discard("")
        capabilities = []
        try:
            capabilities = self.registry.list_capabilities()
        except Exception as exc:
            logger.warning("advisor_capability_snapshot_failed", error=str(exc))
        prompt = {
            "recent_observer_events": events,
            "dismissed_proposals": dismissed[:20],
            "context": context,
            "capabilities": capabilities[:120],
        }
        response = await self.llm.chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _json_compact(prompt)},
            ],
            model=self.default_model,
            temperature=0.2,
            response_format="json",
        )
        content = str(
            (response.get("message") or {}).get("content") or response.get("response") or "{}"
        )
        try:
            parsed = _extract_json(content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("advisor_bad_llm_json", error=str(exc))
            return []
        raw = parsed.get("proposals", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(raw, list):
            return []
        normalized = [proposal for item in raw if (proposal := self._normalize_proposal(item))]
        return [
            proposal
            for proposal in normalized
            if str(proposal.get("title") or "").strip().lower() not in dismissed_titles
        ]

    def _normalize_proposal(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        agent = str(item.get("agent") or action.get("agent") or "").strip()
        capability = str(item.get("capability") or action.get("capability") or "").strip()
        inputs = (
            item.get("inputs") if isinstance(item.get("inputs"), dict) else action.get("inputs")
        )
        if not isinstance(inputs, dict):
            inputs = {}
        title = str(item.get("title") or "").strip()
        if not agent or not capability or not title:
            return None
        return {
            "title": title,
            "rationale": str(item.get("rationale") or "").strip(),
            "agent": agent,
            "capability": capability,
            "inputs": inputs,
            "evidence_event_ids": _ints(item.get("evidence_event_ids")),
            "confidence": _clamp_confidence(item.get("confidence")),
            "cost_estimate": str(item.get("cost_estimate") or "").strip() or None,
            "impact_estimate": str(item.get("impact_estimate") or "").strip() or None,
        }

    async def _save_proposal(
        self,
        *,
        kind: str,
        proposal: dict[str, Any],
        explanation: dict[str, Any],
        status: str,
    ) -> int:
        action = {
            "agent": proposal.get("agent"),
            "capability": proposal.get("capability"),
            "inputs": proposal.get("inputs") or {},
            "safety": explanation,
        }
        rationale = str(proposal.get("rationale") or "").strip()
        if rationale:
            rationale += "\n\n"
        rationale += f"Action: {_json_compact(action, limit=3000)}"
        try:
            return await self.store.add_proposal(
                kind=kind,
                title=str(proposal.get("title") or "Suggested action"),
                rationale=rationale,
                evidence_event_ids=proposal.get("evidence_event_ids") or [],
                confidence=float(proposal.get("confidence") or 0.0),
                cost_estimate=proposal.get("cost_estimate"),
                impact_estimate=proposal.get("impact_estimate") or explanation.get("reason"),
                status=status,
                delivery_channel="advisor",
            )
        except Exception as exc:
            logger.warning("advisor_add_proposal_failed", error=str(exc))
            return 0

    async def _context(self) -> dict[str, bool]:
        quiet_hours_active = await self._quiet_hours_active()
        is_dinner_window = self._is_dinner_window()
        calendar_busy = _env_truthy("ADVISOR_CALENDAR_BUSY")
        is_tv_on = False
        if not (quiet_hours_active or is_dinner_window or calendar_busy):
            is_tv_on = await self._is_tv_on()
        return {
            "quiet_hours_active": quiet_hours_active,
            "is_dinner_window": is_dinner_window,
            "is_tv_on": is_tv_on,
            "calendar_busy": calendar_busy,
        }

    async def _quiet_hours_active(self) -> bool:
        if self.redis is not None:
            try:
                override = await self.redis.get("policy:override:quiet")
                if override == "on":
                    return True
                if override == "off":
                    return False
            except Exception as exc:
                logger.warning("advisor_quiet_override_read_failed", error=str(exc))
        cfg = self._notification_policies().get("quiet_hours", {})
        if not cfg.get("enabled", False):
            return False
        local = _local_dt(self._now_fn(), str(cfg.get("tz") or os.environ.get("TZ") or "UTC"))
        start = self._minutes(str(cfg.get("start") or "22:30"))
        end = self._minutes(str(cfg.get("end") or "07:00"))
        current = local.hour * 60 + local.minute
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _notification_policies(self) -> dict[str, Any]:
        try:
            data = yaml.safe_load((HERE / "policies.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _is_dinner_window(self) -> bool:
        local = _local_dt(self._now_fn(), os.environ.get("TZ", "Asia/Dubai"))
        minutes = local.hour * 60 + local.minute
        return 18 * 60 <= minutes < 19 * 60 + 30

    async def _is_tv_on(self) -> bool:
        try:
            result = await self.registry.dispatch(
                "home_automation", "list_entities", {"domain": "media_player"}
            )
        except Exception as exc:
            logger.warning("advisor_tv_context_failed", error=str(exc))
            return False
        payload = (
            result.get("result") if isinstance(result, dict) and "result" in result else result
        )
        return any(
            str(entity.get("state") or "").lower() == "playing"
            for entity in _iter_entities(payload)
        )

    def _minutes(self, raw: str) -> int:
        try:
            hour_s, minute_s = raw.split(":", 1)
            hour = int(hour_s)
            minute = int(minute_s)
        except (TypeError, ValueError):
            return 0
        return max(0, min(hour, 23)) * 60 + max(0, min(minute, 59))

    async def _record_advisor_brief(self, summary: str, body: dict[str, Any]) -> int:
        try:
            return await self.store.record_brief(summary=summary, body=body)
        except Exception as exc:
            logger.warning("advisor_record_brief_failed", error=str(exc))
            return 0
