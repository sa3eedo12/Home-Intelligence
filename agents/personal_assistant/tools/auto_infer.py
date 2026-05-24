from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # device.state_changed events get persisted to event_log directly by
    # the device_activity_recorder observer. They don't need a separate
    # LLM inference round — adding one was an expensive no-op that
    # contributed to the post-reboot CPU fire when thousands of
    # unavailable→on transitions stacked up.
    "device.state_changed",
}
DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_HOURLY_CAP = 5
# Hard cap on how long the LLM inference call may run. The observer
# pipeline is reactive — if one event's inference blocks for 17+ seconds
# (real values from production logs that prompted proposal #75), every
# subsequent reactive trigger queues behind it. 30s is a generous ceiling
# for the 0.6B/8B routing the auto_infer path uses; anything beyond is
# almost certainly a model swap or Ollama hang we should not wait for.
# Override with AUTO_INFER_LLM_TIMEOUT_SECONDS env var.
DEFAULT_INFER_TIMEOUT_SECONDS = 30.0
# Cross-entity / cross-time dedup window for rule-based inferences.
# A "TV left on past bedtime" event firing from 4 different HA entities
# (media_player + 2 switches + 1 sensor for one physical TV) should
# collapse to a single auto_inference. 6h is generous enough to absorb
# observer-cooldown bursts but short enough that "left TV on" tomorrow
# evening is still allowed to fire.
DEDUP_LOOKBACK_HOURS = 6
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


def _infer_timeout_seconds() -> float:
    raw = os.getenv("AUTO_INFER_LLM_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_INFER_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_INFER_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_INFER_TIMEOUT_SECONDS


def _min_confidence() -> float:
    """Floor for accepting an inference. Below this it's logged, not saved.

    Was hardcoded to 0.6 before but the LLM prompt biases the model toward
    sub-0.6 outputs ("Set confidence below 0.6 unless..."), so almost
    everything was silently gated out. 0.5 default + env override gives
    operators a way to tune without redeploying.
    """
    raw = os.getenv("AUTO_INFER_MIN_CONFIDENCE", str(DEFAULT_MIN_CONFIDENCE))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIDENCE
    return max(0.0, min(value, 1.0))


# ── User-correction memory ───────────────────────────────────────────────
#
# The auto_inferences table records the outcome of every proposed
# inference: confirmed / rejected / skipped / proposed / expired. We treat
# rejected+skipped as user corrections — when the user keeps saying "no"
# to the same source_kind, the system should back off. When the user
# keeps saying "yes", the floor should loosen so we surface them more.
#
# This is the start of "the system actually learns from feedback" rather
# than "every event is a clean slate."

DEFAULT_BACKOFF_REJECTIONS = 3        # ≥ this many in window → mute
DEFAULT_BACKOFF_DAYS = 7              # rolling window for both directions
DEFAULT_REINFORCE_CONFIRMS = 3        # ≥ this many → loosen floor
DEFAULT_REINFORCE_BONUS = 0.1         # how much to lower floor when reinforced


def _backoff_rejections() -> int:
    raw = os.getenv("AUTO_INFER_BACKOFF_REJECTIONS", str(DEFAULT_BACKOFF_REJECTIONS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_BACKOFF_REJECTIONS


def _backoff_days() -> int:
    raw = os.getenv("AUTO_INFER_BACKOFF_DAYS", str(DEFAULT_BACKOFF_DAYS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_BACKOFF_DAYS


def _reinforce_confirms() -> int:
    raw = os.getenv("AUTO_INFER_REINFORCE_CONFIRMS", str(DEFAULT_REINFORCE_CONFIRMS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_REINFORCE_CONFIRMS


def _adjusted_min_confidence(corrections: dict[str, int]) -> float:
    """Apply correction memory to the global confidence floor.

    Heavy confirmations of a kind → loosen floor. Heavy rejections are
    handled separately (a hard mute, not a floor adjustment) because by
    the time we got here we've already DECIDED to propose; the user
    saying "no" 3 times means we shouldn't even propose, not that we
    should propose with stricter gates.
    """
    base = _min_confidence()
    if corrections.get("confirmed", 0) >= _reinforce_confirms():
        return max(0.0, base - DEFAULT_REINFORCE_BONUS)
    return base


def _should_back_off(corrections: dict[str, int]) -> bool:
    """True if user has corrected enough times that we should stop proposing."""
    rejected = corrections.get("rejected", 0) + corrections.get("skipped", 0)
    return rejected >= _backoff_rejections()


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


def _to_local_iso(ts: Any) -> str:
    """Convert any UTC-bearing timestamp string to the user's local ISO string.

    Returns the input verbatim if it can't be parsed. The LLM sees these
    timestamps verbatim — when they're UTC the model dutifully repeats UTC
    in its inference text ('lightbulb turned on at 06:53:14' when local
    was 10:53). Pre-converting fixes that without changing the model.
    """
    if not isinstance(ts, str) or not ts:
        return ts if isinstance(ts, str) else ""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    try:
        return parsed.astimezone(ZoneInfo(_local_tz_name())).isoformat()
    except ZoneInfoNotFoundError:
        return ts


def _envelope_with_local_ts(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of envelope with every timestamp string
    converted to local time. Used before serialising into the LLM
    context so the model never sees raw UTC and repeats it."""
    out = dict(envelope)
    if "ts" in out:
        out["ts"] = _to_local_iso(out["ts"])
    payload = out.get("payload")
    if isinstance(payload, dict):
        slim = dict(payload)
        for key in ("ts", "since", "on_since", "started_at", "ended_at"):
            if key in slim:
                slim[key] = _to_local_iso(slim[key])
        out["payload"] = slim
    return out


def _context_for_infer(envelope: dict[str, Any], reason: str) -> str:
    kind = str(envelope.get("kind") or "unknown")
    agent = str(envelope.get("agent") or "observer")
    summary = str(envelope.get("summary") or "")
    local_envelope = _envelope_with_local_ts(envelope)
    tz_name = _local_tz_name()
    return (
        "Auto-inference candidate from an observer event. Be conservative and only "
        "propose one memory if this event strongly supports it.\n"
        f"User timezone: {tz_name} — all timestamps below are LOCAL time, not UTC.\n"
        f"Source agent: {agent}\n"
        f"Source kind: {kind}\n"
        f"Observer summary: {summary}\n"
        f"Why it was considered: {reason}\n"
        f"Full observer envelope JSON: {_compact_json(local_envelope)}"
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


# ── Rule-based inferences ────────────────────────────────────────────────
#
# The LLM path is unreliable for known observer kinds: the model's prompt
# biases it toward sub-threshold confidences and even the responses we DO
# get are vague paraphrases ("maybe someone went to bed"). For events whose
# meaning is *deterministic* — TV left on for 6h means TV was left on, full
# stop — skip the LLM entirely and emit a high-confidence inference straight
# from the envelope. The LLM stays as the fallback for novel kinds.
#
# Each producer takes the envelope and returns
# ``(inference, confidence, rule_id, dedup_key)`` if it can interpret the
# event, or ``None`` to defer to the LLM.
#
# dedup_key is a stable signature INDEPENDENT of HA entity-id quirks: the
# same physical TV often surfaces as multiple entities (media_player,
# switch.power, sensor.sound_detection) and each fires a separate
# observer event. Without a per-device dedup key the auto_infer would
# persist 3-4 near-identical rows for one real "TV left on" situation.

RuleProducer = Callable[
    [dict[str, Any]],
    "tuple[str, float, str, str] | None",
]


def _local_tz_name() -> str:
    return os.environ.get("TZ") or os.environ.get("USER_TZ") or "Asia/Dubai"


def _hh_mm(ts: Any) -> str:
    """Render a UTC ISO timestamp as HH:MM in the USER's local timezone.

    REGRESSION: previously returned UTC time. User saw 'The lightbulb
    was turned on at 06:53:14' when actual local time was 10:53 (UTC+4).
    Now converts to the configured TZ (defaults to Asia/Dubai) before
    formatting. Same fix should be applied wherever a raw HA / event_log
    ts string is rendered into user-facing text.
    """
    if not isinstance(ts, str):
        return ""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    try:
        local = parsed.astimezone(ZoneInfo(_local_tz_name()))
    except ZoneInfoNotFoundError:
        local = parsed
    return local.strftime("%H:%M")


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entertainment_left_on_rule(
    envelope: dict[str, Any],
) -> tuple[str, float, str, str] | None:
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    name = (
        payload.get("friendly_name")
        or payload.get("entity_id")
        or "the TV"
    )
    on_hours = _coerce_float(payload.get("on_hours"))
    reason = str(payload.get("reason") or "").strip()
    suffix = ""
    if reason == "past_bedtime":
        suffix = " past your usual bedtime"
    elif reason == "nobody_home":
        suffix = " while nobody was home"
    if on_hours is not None and on_hours >= 1.0:
        text = f"left {name} on for {on_hours:.1f}h{suffix}"
    else:
        text = f"left {name} on{suffix}"
    # Dedup key: collapse all entities of any single TV into one signature.
    # 'past_bedtime' from any of 4 entity_ids → same key → only one
    # auto_inference persisted per evening.
    dedup_key = f"entertainment.left_on:{reason or 'unknown'}"
    return text, 0.85, "rule:entertainment.left_on", dedup_key


def _sleep_likely_asleep_rule(
    envelope: dict[str, Any],
) -> tuple[str, float, str, str] | None:
    when = _hh_mm(envelope.get("ts")) or "tonight"
    return (
        f"went to bed around {when}",
        0.7,
        "rule:sleep.likely_asleep",
        f"sleep.likely_asleep:{when}",
    )


def _sleep_likely_awake_rule(
    envelope: dict[str, Any],
) -> tuple[str, float, str, str] | None:
    when = _hh_mm(envelope.get("ts")) or "this morning"
    return (
        f"woke up around {when}",
        0.7,
        "rule:sleep.likely_awake",
        f"sleep.likely_awake:{when}",
    )


def _anomaly_detected_rule(
    envelope: dict[str, Any],
) -> tuple[str, float, str, str] | None:
    summary = str(envelope.get("summary") or "").strip().rstrip(".")
    if not summary:
        return None
    # Dedup key: anomaly_type from payload, fall back to first 50 chars of
    # summary so the same anomaly firing repeatedly only proposes once.
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    anomaly_type = str(payload.get("anomaly_type") or summary[:50])
    return (
        f"saw something unusual: {summary}",
        0.65,
        "rule:anomaly.detected",
        f"anomaly.detected:{anomaly_type}",
    )


RULE_BASED_INFERENCES: dict[str, RuleProducer] = {
    "entertainment.left_on": _entertainment_left_on_rule,
    "sleep.likely_asleep": _sleep_likely_asleep_rule,
    "sleep.likely_awake": _sleep_likely_awake_rule,
    "anomaly.detected": _anomaly_detected_rule,
}


def _rule_based_inference(
    envelope: dict[str, Any],
) -> tuple[str, float, str, str] | None:
    """Return the deterministic inference for ``envelope.kind``, if any.

    Returns a 4-tuple (inference, confidence, rule_id, dedup_key) where
    dedup_key is a stable signature for cross-entity dedup.
    """
    kind = str(envelope.get("kind") or "").strip()
    producer = RULE_BASED_INFERENCES.get(kind)
    if producer is None:
        return None
    try:
        return producer(envelope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto_infer_rule_failed", kind=kind, error=str(exc))
        return None


def _build_record_event_action(
    inference: str, envelope: dict[str, Any], rule_id: str
) -> dict[str, Any]:
    """Build a `_safe_record_event_action`-compliant action for an inference.

    The schema mirrors what the LLM is supposed to emit — keeping the same
    downstream contract (`knowledge_notes.record_event`) means rule-based
    and LLM-derived inferences flow through the same confirmation +
    persistence path.
    """
    return {
        "agent": "knowledge_notes",
        "capability": "record_event",
        "payload": {
            "agent": "personal_assistant",
            "capability": "inferred_event",
            "summary": inference,
            "payload": {
                "source": rule_id,
                "source_kind": str(envelope.get("kind") or "observer.unknown"),
                "source_summary": str(envelope.get("summary") or "")[:300],
            },
        },
    }


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


# Source kinds where the inference is a *passive observation* (the
# system just logging that something happened) rather than a user
# choice. For these we auto-confirm + auto-dispatch the record_event
# action so the inferences actually flow into knowledge_notes (and
# downstream the proactive scanner can use them). Without this, the
# Telegram callback is the only way auto-inferences ever exit
# 'proposed' state, and the proactive loop starves.
_AUTO_CONFIRM_SOURCE_KINDS = {
    "anomaly.detected",
    "entertainment.left_on",
    # NOTE: device.state_changed used to be here but is now in
    # SKIP_KINDS — the recorder persists rows directly and the LLM
    # inference round was an expensive no-op.
    "appliance.state_changed",
    "presence.left_on",
}
_AUTO_CONFIRM_MIN_CONFIDENCE = 0.60


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
    source_kind = str(envelope.get("kind") or "observer.unknown")
    should_run, reason = _worth_inferring(envelope)
    if not should_run:
        logger.info(
            "auto_infer_skipped", source_kind=source_kind, reason=reason, stage="worth_check"
        )
        return {"ok": True, "skipped": True, "reason": reason}

    try:
        store = await _auto_store()
        recent_count = await store.recent_count_in_window(hours=1)
    except Exception as exc:
        logger.warning("auto_infer_store_unavailable", error=str(exc))
        return {"ok": True, "skipped": True, "reason": "store_unavailable"}

    cap = _hourly_cap()
    if cap == 0 or recent_count >= cap:
        logger.info(
            "auto_infer_skipped",
            source_kind=source_kind,
            reason="rate_limit",
            recent_count=recent_count,
            cap=cap,
        )
        return {"ok": True, "skipped": True, "reason": "rate_limit"}

    # User-correction memory: if you've rejected/skipped this same source_kind
    # repeatedly in the last 7 days, mute it entirely. This is the start of
    # closing the observe -> infer -> CORRECT -> learn loop. Confirmations
    # in the same window unlock a slightly looser confidence floor.
    try:
        corrections = await store.correction_counts(
            source_kind=source_kind, days=_backoff_days()
        )
    except Exception as exc:
        logger.warning("auto_infer_correction_lookup_failed", error=str(exc))
        corrections = {"confirmed": 0, "rejected": 0, "skipped": 0}

    if _should_back_off(corrections):
        logger.info(
            "auto_infer_skipped",
            source_kind=source_kind,
            reason="user_corrections",
            corrections=corrections,
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": "user_corrections",
            "corrections": corrections,
        }

    # Try the deterministic rule path first; fall back to the LLM only for
    # observer kinds we don't have a hand-coded interpretation for. This is
    # what closes the auto_inferences=0 gap — the LLM's prompt is biased
    # toward refusing every event, but for known kinds we don't need it at all.
    rule = _rule_based_inference(envelope)
    dedup_key: str | None = None
    if rule is not None:
        inference, confidence, rule_id, dedup_key = rule
        proposed_action = _build_record_event_action(inference, envelope, rule_id)
        inference_source = rule_id
    else:
        try:
            inference_result = await asyncio.wait_for(
                infer_tool.infer(_context_for_infer(envelope, reason)),
                timeout=_infer_timeout_seconds(),
            )
        except asyncio.TimeoutError:
            # Hard cap — see proposal #75. Logging at warning so latency
            # spikes are still visible in the dashboard without crashing
            # the reactive pipeline. Returns a clean "skipped" so the
            # caller doesn't retry.
            logger.warning(
                "auto_infer_llm_timeout",
                source_kind=source_kind,
                timeout_seconds=_infer_timeout_seconds(),
            )
            return {"ok": True, "skipped": True, "reason": "llm_timeout"}
        except Exception as exc:
            logger.warning("auto_infer_infer_failed", error=str(exc))
            return {"ok": True, "skipped": True, "reason": "infer_failed"}
        try:
            confidence = max(0.0, min(float(inference_result.get("confidence") or 0.0), 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
        inference = str(inference_result.get("inference") or "").strip()
        proposed_action = _normalize_action(inference_result.get("proposed_action"))
        inference_source = "llm"

    # Cross-entity dedup: when the rule path emits a stable dedup_key,
    # check whether the same signature was already persisted in the last
    # DEDUP_HOURS hours and skip if so. Without this, one physical TV
    # exposed as 4 HA entities produces 4 near-identical inferences for
    # one real "TV left on" situation. dedup_key is None on the LLM path
    # (text varies too much to be a reliable signature there).
    if dedup_key is not None:
        try:
            existing = await store.recent_for_inference(
                source_kind=source_kind,
                inference=inference,
                hours=DEDUP_LOOKBACK_HOURS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_infer_dedup_lookup_failed", error=str(exc))
            existing = 0
        if existing > 0:
            logger.info(
                "auto_infer_skipped",
                source_kind=source_kind,
                reason="dedup_within_window",
                dedup_key=dedup_key,
                existing=existing,
            )
            return {
                "ok": True,
                "skipped": True,
                "reason": "dedup_within_window",
                "dedup_key": dedup_key,
            }

    min_conf = _adjusted_min_confidence(corrections)
    if (
        confidence < min_conf
        or not inference
        or not _safe_record_event_action(proposed_action)
    ):
        logger.info(
            "auto_infer_skipped",
            source_kind=source_kind,
            reason="confidence_gate",
            inference_source=inference_source,
            confidence=confidence,
            min_confidence=min_conf,
            has_inference=bool(inference),
            has_action=_safe_record_event_action(proposed_action),
            corrections=corrections,
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": "confidence_gate",
            "confidence": confidence,
        }

    reasoning = (
        f"auto-inference source={inference_source} reason={reason}; "
        f"corrections={corrections}; "
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
        logger.warning("auto_infer_insert_failed", source_kind=source_kind)
        return {"ok": True, "skipped": True, "reason": "store_insert_failed"}

    logger.info(
        "auto_infer_persisted",
        source_kind=source_kind,
        inference_source=inference_source,
        confidence=confidence,
        auto_inference_id=auto_inference_id,
        corrections=corrections,
    )

    # Passive observations auto-confirm so the inference layer actually
    # flows downstream instead of stalling forever on a Telegram
    # callback that may never come. We only do this for explicitly
    # observational source kinds + above the confidence floor; user
    # *choices* (e.g. "did you go to bed at 02:30?") still require a
    # confirm tap.
    auto_confirmed = False
    if (
        source_kind in _AUTO_CONFIRM_SOURCE_KINDS
        and confidence >= _AUTO_CONFIRM_MIN_CONFIDENCE
        and _safe_record_event_action(proposed_action)
    ):
        try:
            action_result = await _dispatch_proposed_action(proposed_action)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "auto_infer_auto_confirm_dispatch_failed",
                auto_inference_id=auto_inference_id,
                error=str(exc),
            )
            action_result = None
        if action_result is not None and _action_succeeded(action_result):
            try:
                await store.confirm(
                    auto_inference_id,
                    chat_id=None,
                    action_result=action_result,
                )
                auto_confirmed = True
                logger.info(
                    "auto_infer_auto_confirmed",
                    source_kind=source_kind,
                    auto_inference_id=auto_inference_id,
                    confidence=confidence,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto_infer_auto_confirm_persist_failed",
                    auto_inference_id=auto_inference_id,
                    error=str(exc),
                )

    response = {
        "ok": True,
        "summary": _summary_for(inference, envelope, reason),
        "auto_inference_id": auto_inference_id,
        "inference": inference,
        "confidence": confidence,
        "auto_confirmed": auto_confirmed,
    }
    # Only include the confirm keyboard when the inference still
    # needs a user decision. Auto-confirmed rows shouldn't show buttons
    # — the user can't "un-confirm" via Telegram anyway.
    if not auto_confirmed:
        response["keyboard"] = _keyboard_for(auto_inference_id)
    return response


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
