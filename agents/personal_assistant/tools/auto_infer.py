from __future__ import annotations

import json
import os
from typing import Any

import httpx
from home_agents_sdk import tool
from home_agents_sdk.auto_inferences_store import AutoInferencesStore
from home_agents_sdk.telemetry import get_logger

from tools import infer as infer_tool
from tools.core import _pool

logger = get_logger("personal_assistant.auto_infer")

SKIP_KINDS = {
    "appliance.cycle_completed",
    "coffee.brewed",
    "cleaning.completed",
    "presence.changed",
}
MIN_CONFIDENCE = 0.6
DEFAULT_HOURLY_CAP = 5
UNUSUAL_TERMS = (
    "unusual",
    "unexpected",
    "anomaly",
    "anomalous",
    "long quiet",
    "quiet period",
    "no motion",
    "multiple",
    "simultaneous",
    "at once",
    "likely asleep",
    "likely awake",
    "odd",
    "late",
    "early",
)


async def _auto_store() -> AutoInferencesStore:
    return AutoInferencesStore(await _pool())


def _hourly_cap() -> int:
    try:
        value = int(os.getenv("AUTO_INFER_HOURLY_CAP", str(DEFAULT_HOURLY_CAP)))
    except ValueError:
        value = DEFAULT_HOURLY_CAP
    return max(0, value)


def _compact_json(value: Any, max_len: int = 2500) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= max_len else f"{text[: max_len - 1]}…"


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _source_event_log_id(envelope: dict[str, Any]) -> int | None:
    for key in ("source_event_log_id", "event_log_id", "event_id", "id"):
        found = _coerce_positive_int(envelope.get(key))
        if found is not None:
            return found
    payload = envelope.get("payload")
    if isinstance(payload, dict):
        for key in ("source_event_log_id", "event_log_id", "event_id", "id"):
            found = _coerce_positive_int(payload.get(key))
            if found is not None:
                return found
    return None


def _multiple_active(payload: dict[str, Any]) -> bool:
    for key in ("active_appliances", "appliances_active", "active_devices", "simultaneous_devices"):
        value = payload.get(key)
        if isinstance(value, list) and len(value) >= 2:
            return True
    for key in ("active_appliance_count", "active_device_count", "simultaneous_count"):
        count = _coerce_positive_int(payload.get(key))
        if count is not None and count >= 2:
            return True
    return False


def _long_quiet(payload: dict[str, Any]) -> bool:
    quiet_seconds = _coerce_positive_int(payload.get("quiet_seconds"))
    if quiet_seconds is not None and quiet_seconds >= 60 * 60:
        return True
    for key in ("quiet_minutes", "minutes_since_motion", "minutes_since_activity"):
        minutes = _coerce_positive_int(payload.get(key))
        if minutes is not None and minutes >= 60:
            return True
    return False


def _worth_inferring(envelope: dict[str, Any]) -> tuple[bool, str]:
    kind = str(envelope.get("kind") or "").strip()
    if kind in SKIP_KINDS:
        return False, f"skip_kind:{kind}"
    summary = str(envelope.get("summary") or "").strip()
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    if not kind and not summary:
        return False, "missing_event_context"
    if _multiple_active(payload):
        return True, "multiple_active_signals"
    if _long_quiet(payload):
        return True, "long_quiet_signal"
    compact = f"{kind} {summary} {_compact_json(payload, max_len=1000)}".casefold()
    if any(term in compact for term in UNUSUAL_TERMS):
        return True, "unusual_event_language"
    if kind.startswith("sleep.") or kind.endswith(".unusual") or ".unusual_" in kind:
        return True, "observer_pattern_signal"
    return True, "unhandled_observer_event"


def _context_for_infer(envelope: dict[str, Any], reason: str) -> str:
    kind = str(envelope.get("kind") or "unknown")
    agent = str(envelope.get("agent") or "observer")
    summary = str(envelope.get("summary") or "")
    return (
        "Auto-inference candidate from an observer event. Be conservative and only "
        "propose one memory if this event strongly supports it.\n"
        f"Source agent: {agent}\n"
        f"Source kind: {kind}\n"
        f"Observer summary: {summary}\n"
        f"Why it was considered: {reason}\n"
        f"Full observer envelope JSON: {_compact_json(envelope)}"
    )


def _source_label(envelope: dict[str, Any], reason: str) -> str:
    kind = str(envelope.get("kind") or "observer")
    agent = str(envelope.get("agent") or "observer")
    label = f"{agent.replace('observer.', '')}/{kind}"
    return f"{label} ({reason.replace('_', ' ')})"


def _summary_for(inference: str, envelope: dict[str, Any], reason: str) -> str:
    compact = inference.strip().rstrip(".?")
    if compact.lower().startswith("you "):
        compact = compact[4:]
    source = _source_label(envelope, reason)
    return f"🤔 Did you just {compact}? (auto-inferred from {source} signals)"


def _keyboard_for(auto_inference_id: int) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "✅ Yes, log it", "callback": f"infer:{auto_inference_id}:confirmed"},
            {"text": "No, ignore", "callback": f"infer:{auto_inference_id}:rejected"},
        ],
        [{"text": "Skip", "callback": f"infer:{auto_inference_id}:skipped"}],
    ]


def _normalize_action(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    agent = value.get("agent")
    capability = value.get("capability")
    payload = value.get("payload")
    if (
        not isinstance(agent, str)
        or not isinstance(capability, str)
        or not isinstance(payload, dict)
    ):
        return None
    return {"agent": agent, "capability": capability, "payload": payload}


def _safe_record_event_action(action: dict[str, Any] | None) -> bool:
    if not action:
        return False
    if action.get("agent") != "knowledge_notes" or action.get("capability") != "record_event":
        return False
    payload = action.get("payload")
    if not isinstance(payload, dict):
        return False
    required = ("agent", "capability", "summary")
    return all(isinstance(payload.get(key), str) and payload.get(key) for key in required)


def _agent_url(agent: str) -> str:
    env_name = f"{agent.upper()}_URL"
    if os.getenv(env_name):
        return str(os.environ[env_name])
    for item in os.getenv("AGENT_URLS", "").split(","):
        name, sep, url = item.partition("=")
        if sep and name.strip() == agent and url.strip():
            return url.strip()
    return f"http://{agent}:8000"


async def _dispatch_proposed_action(action: dict[str, Any]) -> dict[str, Any]:
    url = _agent_url(str(action["agent"]))
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{url.rstrip('/')}/invoke",
            json={"capability": action["capability"], "payload": action["payload"]},
        )
        response.raise_for_status()
        result = response.json()
    return result if isinstance(result, dict) else {"ok": False, "error": "bad_dispatch_result"}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _action_succeeded(result: dict[str, Any]) -> bool:
    if not result.get("ok"):
        return False
    inner = result.get("result")
    return not (isinstance(inner, dict) and inner.get("ok") is False)


@tool("auto_infer_observer_event", side_effects=True)
async def auto_infer_observer_event(**envelope: Any) -> dict[str, Any]:
    should_run, reason = _worth_inferring(envelope)
    if not should_run:
        return {"ok": True, "skipped": True, "reason": reason}

    try:
        store = await _auto_store()
        recent_count = await store.recent_count_in_window(hours=1)
    except Exception as exc:
        logger.warning("auto_infer_store_unavailable", error=str(exc))
        return {"ok": True, "skipped": True, "reason": "store_unavailable"}

    cap = _hourly_cap()
    if cap == 0 or recent_count >= cap:
        return {"ok": True, "skipped": True, "reason": "rate_limit"}

    try:
        inference_result = await infer_tool.infer(_context_for_infer(envelope, reason))
    except Exception as exc:
        logger.warning("auto_infer_infer_failed", error=str(exc))
        return {"ok": True, "skipped": True, "reason": "infer_failed"}

    try:
        confidence = max(0.0, min(float(inference_result.get("confidence") or 0.0), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    inference = str(inference_result.get("inference") or "").strip()
    proposed_action = _normalize_action(inference_result.get("proposed_action"))
    if (
        confidence < MIN_CONFIDENCE
        or not inference
        or not _safe_record_event_action(proposed_action)
    ):
        return {
            "ok": True,
            "skipped": True,
            "reason": "confidence_gate",
            "confidence": confidence,
        }

    source_kind = str(envelope.get("kind") or "observer.unknown")
    reasoning = (
        f"auto-inference classifier reason={reason}; "
        f"source_summary={str(envelope.get('summary') or '')[:300]}"
    )
    auto_inference_id = await store.insert(
        source_event_log_id=_source_event_log_id(envelope),
        source_kind=source_kind,
        inference=inference,
        confidence=confidence,
        reasoning=reasoning,
        proposed_action=proposed_action,
    )
    if auto_inference_id is None:
        return {"ok": True, "skipped": True, "reason": "store_insert_failed"}

    return {
        "ok": True,
        "summary": _summary_for(inference, envelope, reason),
        "keyboard": _keyboard_for(auto_inference_id),
        "auto_inference_id": auto_inference_id,
        "inference": inference,
        "confidence": confidence,
    }


@tool("confirm_auto_inference", side_effects=True)
async def confirm_auto_inference(
    auto_inference_id: int,
    status: str,
    chat_id: int | None = None,
) -> dict[str, Any]:
    auto_inference_id = _coerce_positive_int(auto_inference_id) or 0
    if auto_inference_id <= 0:
        return {"ok": False, "error": "auto_inference_id must be a positive integer"}
    if status not in {"confirmed", "rejected", "skipped"}:
        return {"ok": False, "error": "status must be confirmed, rejected, or skipped"}

    try:
        store = await _auto_store()
    except Exception as exc:
        logger.warning("auto_infer_store_unavailable", error=str(exc))
        return {"ok": False, "error": "auto_inference_store_unavailable"}

    if status != "confirmed":
        record = await store.reject(auto_inference_id, status=status, chat_id=chat_id)
        if record is None:
            return {"ok": False, "error": "auto_inference not found or already handled"}
        return {"ok": True, "record": _jsonable(record)}

    record = await store.get(auto_inference_id)
    if record is None or record.get("status") != "proposed":
        return {"ok": False, "error": "auto_inference not found or already handled"}
    proposed_action = _normalize_action(record.get("proposed_action"))
    if not _safe_record_event_action(proposed_action):
        return {"ok": False, "error": "unsafe_or_missing_proposed_action"}

    try:
        action_result = await _dispatch_proposed_action(proposed_action)
    except Exception as exc:
        logger.warning("auto_infer_dispatch_failed", error=str(exc))
        action_result = {"ok": False, "error": str(exc)}

    if not _action_succeeded(action_result):
        return {
            "ok": False,
            "error": "proposed_action_failed",
            "action_result": _jsonable(action_result),
        }

    updated = await store.confirm(
        auto_inference_id,
        chat_id=chat_id,
        action_result=action_result,
    )
    if updated is None:
        return {"ok": False, "error": "auto_inference not found or already handled"}
    return {
        "ok": True,
        "record": _jsonable(updated),
        "action_result": _jsonable(action_result),
    }
