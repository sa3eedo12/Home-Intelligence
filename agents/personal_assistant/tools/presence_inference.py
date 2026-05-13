from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from home_agents_sdk import tool
from home_agents_sdk.presence_returns_store import PresenceReturnsStore

_POOL: asyncpg.Pool | None = None

CANDIDATE_CONTEXTS: tuple[str, ...] = (
    "work",
    "gym",
    "errands",
    "social",
    "school",
    "commute",
    "unknown",
)
KEYBOARD_CONTEXTS: tuple[str, ...] = ("work", "gym", "errands", "social")
_CONTEXT_ALIASES = {
    "office": "work",
    "job": "work",
    "workout": "gym",
    "errand": "errands",
    "shopping": "errands",
    "friends": "social",
    "friend": "social",
}


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
    if not isinstance(raw, str) or not raw.strip():
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


def _normalise_context(raw: Any) -> str | None:
    if raw is None:
        return None
    context = str(raw).strip().casefold().replace(" ", "_")
    context = _CONTEXT_ALIASES.get(context, context)
    return context if context in CANDIDATE_CONTEXTS else None


def _user_zone() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("USER_TZ", "Asia/Dubai"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_user_local(value: datetime) -> datetime:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(_user_zone())


def _history_day_of_week(value: datetime) -> int:
    local = _as_user_local(value)
    return (local.weekday() + 1) % 7


def _time_bucket(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def _format_away(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    if minutes < 60:
        return f"gone {minutes}m"
    hours, remainder = divmod(minutes, 60)
    if remainder >= 15:
        return f"gone {hours}h{remainder}m"
    return f"gone {hours}h"


def _normalise_history_item(item: Any) -> tuple[int, int, str] | None:
    if isinstance(item, dict):
        hour = _coerce_int(item.get("hour_of_day"))
        day = _coerce_int(item.get("day_of_week"))
        context = _normalise_context(item.get("confirmed_context") or item.get("context"))
    elif isinstance(item, (tuple, list)) and len(item) >= 3:
        hour = _coerce_int(item[0])
        day = _coerce_int(item[1])
        context = _normalise_context(item[2])
    else:
        return None
    if hour is None or day is None or context is None:
        return None
    return hour % 24, day % 7, context


def _habitual_context(
    history: list[tuple[int, int, str]] | list[dict[str, Any]],
    *,
    hour: int,
    day_of_week: int,
) -> tuple[str | None, int, str]:
    counts: Counter[str] = Counter()
    for item in history:
        normalized = _normalise_history_item(item)
        if normalized is None:
            continue
        hist_hour, hist_day, context = normalized
        if context == "unknown":
            continue
        if hist_day == day_of_week and abs(hist_hour - hour) <= 1:
            counts[context] += 1
    if not counts:
        return None, 0, ""
    context, count = counts.most_common(1)[0]
    if count >= 3:
        return context, count, f"{count} recent confirmations for this return time were '{context}'"
    return None, count, ""


def _infer(
    *,
    away_minutes: int | None,
    returned_at: datetime,
    history: list[tuple[int, int, str]] | list[dict[str, Any]],
) -> tuple[str, float, str]:
    local = _as_user_local(returned_at)
    hour = local.hour
    python_weekday = local.weekday()
    day_of_week = (python_weekday + 1) % 7
    is_weekend = python_weekday >= 5
    habit_context, habit_count, habit_reason = _habitual_context(
        history,
        hour=hour,
        day_of_week=day_of_week,
    )

    reasons: list[str] = []
    context = "unknown"
    confidence = 0.25

    if away_minutes is None:
        reasons.append("no away duration was available")
    elif not is_weekend and 300 <= away_minutes <= 480 and 14 <= hour <= 16:
        context = "school"
        confidence = 0.55
        reasons.append("weekday afternoon return after a school-length absence")
    elif not is_weekend and 420 <= away_minutes <= 660 and 17 <= hour <= 19:
        context = "work"
        confidence = 0.78
        reasons.append("weekday evening return after a workday-length absence")
    elif 19 <= hour <= 23 and 120 <= (away_minutes or 0) <= 300:
        context = "social"
        confidence = 0.65
        reasons.append("evening return after a few hours away")
    elif not is_weekend and 20 <= away_minutes <= 90 and 6 <= hour <= 10:
        context = "commute"
        confidence = 0.45
        reasons.append("weekday morning short trip resembles a commute")
    elif not is_weekend and 60 <= away_minutes <= 150:
        context = habit_context if habit_context in {"gym", "errands"} else "errands"
        confidence = 0.62 if habit_context == context else 0.52
        reasons.append("weekday short absence points to errands or gym")
    elif is_weekend and 60 <= away_minutes <= 180:
        context = habit_context if habit_context in {"gym", "errands"} else "gym"
        confidence = 0.62 if habit_context == context else 0.55
        reasons.append("weekend 1-3 hour absence points to gym or errands")
    # NEW: low-confidence fallbacks so we always make SOME guess and
    # surface a meaningful question. "unknown" forever is useless.
    elif away_minutes is not None and away_minutes <= 30:
        context = "errands"
        confidence = 0.35
        reasons.append(f"short {away_minutes}-min absence — guessing errands")
    elif away_minutes is not None and away_minutes >= 240:
        if not is_weekend and hour >= 16:
            context = "work"
            confidence = 0.5
            reasons.append(f"long {away_minutes // 60}h weekday return — likely work")
        else:
            context = "social"
            confidence = 0.4
            reasons.append(f"long {away_minutes // 60}h absence — guessing social")
    elif 18 <= hour <= 23:
        context = "social"
        confidence = 0.35
        reasons.append("evening return — guessing social outing")
    elif 6 <= hour <= 10:
        context = "commute"
        confidence = 0.35
        reasons.append("morning return — guessing commute/errand")
    else:
        # Truly nothing — keep unknown but lift confidence slightly
        # so the keyboard still surfaces useful options.
        confidence = 0.3
        reasons.append("no duration/time heuristic matched — ask the user")

    if habit_context and habit_count >= 3:
        if context == habit_context:
            confidence = min(0.95, confidence + 0.15)
            reasons.append(habit_reason)
        elif context in {"unknown", "gym", "errands"} or confidence <= 0.55:
            context = habit_context
            confidence = max(0.65, min(0.85, confidence + 0.2))
            reasons.append(habit_reason)
        else:
            reasons.append(
                f"habit signal suggested '{habit_context}', but time/duration fit '{context}'"
            )

    return context, round(confidence, 2), "; ".join(reasons)


def _keyboard_for(presence_return_id: int, guessed: str) -> list[list[dict[str, str]]]:
    seen = {guessed}
    ordered = [] if guessed == "unknown" else [guessed]
    for candidate in KEYBOARD_CONTEXTS:
        if candidate not in seen:
            ordered.append(candidate)
            seen.add(candidate)
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for context in ordered[:4]:
        button_text = f"✅ {context}" if context == guessed else context
        row.append({"text": button_text, "callback": f"presence:{presence_return_id}:{context}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Skip", "callback": f"presence:{presence_return_id}:_skip"}])
    return rows


def _summary_for(
    person: str | None,
    context: str,
    away_minutes: int | None,
    returned_at: datetime,
) -> str:
    local = _as_user_local(returned_at)
    day_word = "weekend" if local.weekday() >= 5 else "weekday"
    time_hint = f"{day_word} {_time_bucket(local.hour)}"
    hints = [hint for hint in (_format_away(away_minutes), time_hint) if hint]
    prefix = f"👋 Welcome home, {person}." if person else "👋 Welcome home."
    if context == "unknown":
        question = "Want to note where you were?"
    else:
        question = f"Coming back from {context}?"
    return f"{prefix} {question} ({', '.join(hints)})"


def _payload_from(payload: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if isinstance(payload, dict):
        data.update(payload)
    data.update(kwargs)
    nested = data.get("payload")
    if isinstance(nested, dict) and "state" not in data:
        data.update(nested)
    return data


def _person_from(entity_id: str | None, raw: Any) -> str | None:
    if raw not in (None, ""):
        return str(raw)
    if not entity_id:
        return None
    name = entity_id.split(".", 1)[-1].replace("_", " ").strip()
    return name.title() if name else None


@tool("infer_presence_return", side_effects=True)
async def infer_presence_return(
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    data = _payload_from(payload, kwargs)
    state = str(data.get("state") or "").casefold()
    if state != "home":
        return {"ok": True, "ignored": True, "summary": "", "keyboard": []}

    entity_id = str(data.get("entity_id") or "") or None
    person = _person_from(entity_id, data.get("person"))
    returned_at = (
        _parse_iso(data.get("returned_at")) or _parse_iso(data.get("since")) or datetime.now(UTC)
    )
    returned_at = returned_at.astimezone(UTC)
    left_at = _parse_iso(data.get("left_at"))
    away_minutes = _coerce_int(data.get("away_minutes"))
    household_member_id = _coerce_int(data.get("household_member_id"))

    store = PresenceReturnsStore(await _pool())
    if left_at is None and entity_id:
        left_at = await store.last_left_at(entity_id)
    if away_minutes is None and left_at is not None:
        away_minutes = max(0, int((returned_at - left_at.astimezone(UTC)).total_seconds() // 60))

    history = await store.confirmed_context_history(person, limit_days=30)
    context, confidence, reasoning = _infer(
        away_minutes=away_minutes,
        returned_at=returned_at,
        history=history,
    )
    presence_return_id = await store.insert_return(
        household_member_id=household_member_id,
        entity_id=entity_id,
        person=person,
        left_at=left_at,
        returned_at=returned_at,
        away_minutes=away_minutes,
        guessed_context=context,
        guessed_confidence=confidence,
        guessed_reasoning=reasoning,
    )
    keyboard = _keyboard_for(presence_return_id, context) if presence_return_id is not None else []
    return {
        "ok": True,
        "summary": _summary_for(person, context, away_minutes, returned_at),
        "context": context,
        "confidence": confidence,
        "reasoning": reasoning,
        "presence_return_id": presence_return_id,
        "keyboard": keyboard,
    }


@tool("confirm_presence_return", side_effects=True)
async def confirm_presence_return(
    presence_return_id: int,
    context: str,
    chat_id: int | None = None,
) -> dict[str, Any]:
    if not isinstance(presence_return_id, int) or presence_return_id <= 0:
        return {"ok": False, "error": "presence_return_id must be a positive integer"}
    normalized_context = _normalise_context(context)
    if normalized_context is None:
        return {"ok": False, "error": "context must be one of the known return contexts"}
    store = PresenceReturnsStore(await _pool())
    record = await store.confirm(presence_return_id, normalized_context, chat_id)
    if record is None:
        return {"ok": False, "error": "presence_return not found"}
    person = str(record.get("person") or "")
    history = await store.confirmed_context_history(person, limit_days=30) if person else []
    same = sum(1 for h in history if _confirm_match(h, normalized_context))
    was_correction = normalized_context != (record.get("guessed_context") or "")
    learning = _presence_learning(
        normalized_context, same, len(history), was_correction=was_correction
    )
    return {"ok": True, "record": _jsonable(record), "learning": learning}


def _confirm_match(history_item: Any, ctx: str) -> bool:
    if isinstance(history_item, dict):
        return (history_item.get("confirmed_context") or history_item.get("context") or "") == ctx
    if isinstance(history_item, (list, tuple)) and len(history_item) >= 3:
        return history_item[2] == ctx
    return False


def _presence_learning(ctx: str, same: int, total: int, *, was_correction: bool) -> str:
    if was_correction:
        return f"Got it — '{ctx}'. I'll lean that way for similar return times."
    if same >= 4:
        return (
            f"Saved as {ctx}. Most of your similar returns were {ctx} too "
            f"({same}/{total}) — strong pattern."
        )
    return f"Saved as {ctx}."


@tool("recent_presence_returns")
async def recent_presence_returns(person: str | None = None, limit: int = 20) -> dict[str, Any]:
    store = PresenceReturnsStore(await _pool())
    items = await store.recent(person=person, limit=limit)
    return {"ok": True, "items": _jsonable(items), "count": len(items)}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
