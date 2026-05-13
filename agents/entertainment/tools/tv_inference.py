from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import asyncpg
from home_agents_sdk import tool
from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tv_left_on_store import TvLeftOnStore

logger = get_logger("entertainment.tv_inference")

_POOL: asyncpg.Pool | None = None
TV_ACTIONS = {"turn_off", "snooze", "always_off_at_bedtime", "skip"}


async def _pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        url = os.getenv("DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents")
        _POOL = await asyncpg.create_pool(url, min_size=1, max_size=5)
    return _POOL


def _parse_iso(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _coerce_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _coerce_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _suggested_action(reason: str | None) -> str:
    return "always_off_at_bedtime" if reason == "past_bedtime" else "turn_off"


def _reason_text(reason: str | None) -> str:
    if reason == "nobody_home":
        return "nobody appears to be home"
    if reason == "past_bedtime":
        return "it's past the usual bedtime"
    return "it looks idle"


def _summary_for(
    *,
    friendly_name: str,
    on_hours: float | None,
    reason: str | None,
    suggested_action: str,
) -> str:
    hours_text = f" for {on_hours:.1f}h" if on_hours is not None else ""
    bits = [f"📺 {friendly_name} has been on{hours_text}; {_reason_text(reason)}."]
    if suggested_action == "always_off_at_bedtime":
        bits.append("Turn it off now, snooze, or make bedtime auto-off the default?")
    else:
        bits.append("Turn it off now or snooze?")
    return " ".join(bits)


def _keyboard_for(tv_left_on_id: int) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "Turn off now", "callback": f"tv:{tv_left_on_id}:turn_off"},
            {"text": "Snooze 30 min", "callback": f"tv:{tv_left_on_id}:snooze"},
        ],
        [
            {
                "text": "Always off at bedtime",
                "callback": f"tv:{tv_left_on_id}:always_off_at_bedtime",
            },
            {"text": "Skip", "callback": f"tv:{tv_left_on_id}:skip"},
        ],
    ]


@tool("suggest_tv_action", side_effects=True)
async def suggest_tv_action(
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    data = dict(payload or {})
    data.update(kwargs)
    entity_id = str(data.get("entity_id") or "").strip()
    if not entity_id:
        return {"ok": False, "error": "entity_id is required"}

    friendly_name = str(data.get("friendly_name") or entity_id)
    on_since = _parse_iso(data.get("on_since"))
    detected_at = _parse_iso(data.get("detected_at")) or datetime.now(UTC)
    on_hours = _coerce_float(data.get("on_hours"))
    reason_raw = data.get("reason")
    reason = str(reason_raw) if reason_raw not in (None, "") else None
    event_log_id = _coerce_int(data.get("event_log_id"))
    suggested_action = _suggested_action(reason)

    store = TvLeftOnStore(await _pool())
    tv_left_on_id = await store.insert(
        event_log_id=event_log_id,
        entity_id=entity_id,
        friendly_name=friendly_name,
        on_since=on_since,
        detected_at=detected_at,
        on_hours=on_hours,
        reason=reason,
        suggested_action=suggested_action,
    )
    summary = _summary_for(
        friendly_name=friendly_name,
        on_hours=on_hours,
        reason=reason,
        suggested_action=suggested_action,
    )
    keyboard = _keyboard_for(tv_left_on_id) if tv_left_on_id is not None else []
    return {
        "ok": True,
        "summary": summary,
        "suggested_action": suggested_action,
        "keyboard": keyboard,
        "tv_left_on_id": tv_left_on_id,
    }


@tool("confirm_tv_action", side_effects=True)
async def confirm_tv_action(
    tv_left_on_id: int,
    action: str,
    chat_id: int | None = None,
) -> dict[str, Any]:
    tv_id = _coerce_int(tv_left_on_id)
    if tv_id is None or tv_id <= 0:
        return {"ok": False, "error": "tv_left_on_id must be a positive integer"}
    clean_action = str(action or "").strip()
    if clean_action == "_skip":
        clean_action = "skip"
    if clean_action not in TV_ACTIONS:
        return {"ok": False, "error": f"unsupported action: {action}"}

    store = TvLeftOnStore(await _pool())
    record = await store.confirm(tv_id, action=clean_action, chat_id=_coerce_int(chat_id))
    if record is None:
        return {"ok": False, "error": "tv_left_on not found"}

    result: dict[str, Any] = {"ok": True, "record": _jsonable(record)}
    entity_id = str(record.get("entity_id") or "")
    friendly_name = record.get("friendly_name")
    if clean_action == "turn_off" and entity_id:
        result["turn_off"] = await _turn_off_entity(entity_id)
    elif clean_action == "always_off_at_bedtime" and entity_id:
        setting = await store.enable_auto_off_at_bedtime(
            entity_id=entity_id,
            friendly_name=str(friendly_name or entity_id),
        )
        result["auto_off_at_bedtime"] = _jsonable(setting) if setting is not None else None
    return result


@tool("recent_tv_left_on")
async def recent_tv_left_on(limit: int = 20, only_unconfirmed: bool = False) -> dict[str, Any]:
    store = TvLeftOnStore(await _pool())
    items = await store.recent(limit=limit, only_unconfirmed=only_unconfirmed)
    return {"ok": True, "items": _jsonable(items), "count": len(items)}


async def _turn_off_entity(entity_id: str) -> dict[str, Any]:
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "homeassistant"
    payload = {
        "capability": "call_service",
        "payload": {"domain": domain, "service": "turn_off", "data": {"entity_id": entity_id}},
    }
    url = f"{_home_automation_url().rstrip('/')}/invoke"
    try:
        return await asyncio.to_thread(_post_json, url, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tv_turn_off_dispatch_failed", entity_id=entity_id, error=str(exc))
        return {"ok": False, "error": str(exc)}


def _home_automation_url() -> str:
    explicit = os.getenv("HOME_AUTOMATION_URL")
    if explicit:
        return explicit
    for part in os.getenv("AGENT_URLS", "").split(","):
        if "=" not in part:
            continue
        name, url = part.split("=", 1)
        if name.strip() == "home_automation" and url.strip():
            return url.strip()
    return "http://home_automation:8000"


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"home_automation returned {exc.code}: {body}") from exc
    decoded = json.loads(raw) if raw else {}
    return decoded if isinstance(decoded, dict) else {"result": decoded}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
