"""Room-level inference for completed vacuum cleaning runs.

When the vacuum observer detects ``cleaning.completed``, the reactive trigger
passes the observer payload here. We persist the run, compare reported rooms to
the user's recent typical room set, and return a Telegram-ready summary plus
quick-reply keyboard so the user can confirm or correct the coverage status.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import asyncpg
from home_agents_sdk import tool
from home_agents_sdk.cleaning_runs_store import CleaningRunsStore

_POOL: asyncpg.Pool | None = None
VALID_STATUSES: tuple[str, ...] = ("full", "partial", "unusual")
_STATUS_LABELS: dict[str, str] = {
    "full": "Full coverage",
    "partial": "Missed rooms",
    "unusual": "Unusual run",
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


def _normalise_room(raw: Any) -> str | None:
    room = " ".join(str(raw).strip().casefold().replace("_", " ").split())
    return room or None


def _rooms_from_payload(payload: dict[str, Any]) -> list[str]:
    raw: Any = None
    for key in ("rooms", "reported_rooms", "cleaned_rooms", "current_room"):
        value = payload.get(key)
        if value:
            raw = value
            break

    if raw is None:
        return []
    if isinstance(raw, str):
        values: list[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return []

    rooms: list[str] = []
    seen: set[str] = set()
    for value in values:
        room = _normalise_room(value)
        if room and room not in seen:
            rooms.append(room)
            seen.add(room)
    return rooms


def _format_rooms(rooms: list[str]) -> str:
    return ", ".join(rooms)


def _sentence_start(text: str) -> str:
    return f"{text[:1].upper()}{text[1:]}" if text else text


def _infer_status(
    reported_rooms: list[str], expected_rooms: list[str]
) -> tuple[str, list[str], str]:
    expected_set = set(expected_rooms)
    reported_set = set(reported_rooms)

    if not reported_rooms and expected_rooms:
        return "unusual", expected_rooms, "observer did not report any cleaned rooms"
    if not reported_rooms:
        return "unusual", [], "observer did not report cleaned rooms"
    if not expected_rooms:
        return "unusual", [], "no typical room pattern is known yet"

    missed = [room for room in expected_rooms if room not in reported_set]
    if not reported_set.intersection(expected_set):
        return "unusual", missed or expected_rooms, "reported rooms do not overlap the usual set"
    if missed:
        return "partial", missed, f"missed expected rooms: {_format_rooms(missed)}"
    return "full", [], "reported rooms cover the usual set"


def _summary_for(
    status: str,
    reported_rooms: list[str],
    expected_rooms: list[str],
    missed_rooms: list[str],
) -> str:
    reported_text = _format_rooms(reported_rooms) or "unknown rooms"
    if status == "full":
        covered = _format_rooms(expected_rooms or reported_rooms) or reported_text
        return f"🧹 Vacuum done — full coverage of {covered}"
    if status == "partial":
        missed = _sentence_start(_format_rooms(missed_rooms))
        return (
            f"🧹 Vacuum done in {reported_text}. {missed} missed today — "
            "should I remind you to run it again?"
        )
    if expected_rooms:
        return (
            f"🧹 Vacuum done in {reported_text}. This differs from the usual "
            f"{_format_rooms(expected_rooms)} — confirm this run?"
        )
    return (
        f"🧹 Vacuum done in {reported_text}. I don't have a usual room pattern yet — "
        "confirm this run?"
    )


def _keyboard_for(cleaning_run_id: int, guessed_status: str) -> list[list[dict[str, str]]]:
    if guessed_status == "full":
        return [[{"text": "Acknowledge", "callback": f"clean:{cleaning_run_id}:_skip"}]]

    ordered = [guessed_status, *(status for status in VALID_STATUSES if status != guessed_status)]
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for status in ordered:
        label = _STATUS_LABELS[status]
        text = f"✅ {label}" if status == guessed_status else label
        row.append({"text": text, "callback": f"clean:{cleaning_run_id}:{status}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Skip", "callback": f"clean:{cleaning_run_id}:_skip"}])
    return rows


def _merge_payload(payload: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload) if isinstance(payload, dict) else {}
    merged.update(kwargs)
    return merged


@tool("infer_cleaning_run", side_effects=True)
async def infer_cleaning_run(
    payload: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Infer room coverage for a completed vacuum run and persist it."""
    data = _merge_payload(payload, kwargs)
    reported_rooms = _rooms_from_payload(data)
    started_at = _parse_iso(data.get("started_at"))
    ended_at = _parse_iso(data.get("ended_at")) or datetime.now(UTC)
    duration_seconds = _coerce_int(data.get("duration_seconds"))
    event_log_id = _coerce_int(data.get("event_log_id"))
    entity_id_raw = data.get("entity_id")
    entity_id = str(entity_id_raw) if entity_id_raw not in (None, "") else None
    attributes_at_finish = data.get("attributes_at_finish") or data.get("attributes") or {}
    if not isinstance(attributes_at_finish, dict):
        attributes_at_finish = {}

    store = CleaningRunsStore(await _pool())
    expected_rooms = await store.typical_rooms(limit_history=10)
    status, missed_rooms, reasoning = _infer_status(reported_rooms, expected_rooms)
    cleaning_run_id = await store.insert_run(
        entity_id=entity_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        reported_rooms=reported_rooms,
        expected_rooms=expected_rooms,
        missed_rooms=missed_rooms,
        guessed_status=status,
        guessed_reasoning=reasoning,
        attributes_at_finish=attributes_at_finish,
        event_log_id=event_log_id,
    )
    summary = _summary_for(status, reported_rooms, expected_rooms, missed_rooms)
    keyboard = _keyboard_for(cleaning_run_id, status) if cleaning_run_id is not None else []
    return {
        "ok": True,
        "summary": summary,
        "status": status,
        "missed_rooms": missed_rooms,
        "expected_rooms": expected_rooms,
        "reported_rooms": reported_rooms,
        "reasoning": reasoning,
        "cleaning_run_id": cleaning_run_id,
        "keyboard": keyboard,
    }


@tool("confirm_cleaning_run", side_effects=True)
async def confirm_cleaning_run(
    cleaning_run_id: int, status: str, chat_id: int | None = None
) -> dict[str, Any]:
    """Record the user's correction. Called by Telegram callback handler."""
    if not isinstance(cleaning_run_id, int) or cleaning_run_id <= 0:
        return {"ok": False, "error": "cleaning_run_id must be a positive integer"}
    if status not in VALID_STATUSES:
        return {"ok": False, "error": "status must be one of full, partial, unusual"}
    store = CleaningRunsStore(await _pool())
    record = await store.confirm(cleaning_run_id, status=status, chat_id=chat_id)
    if record is None:
        return {"ok": False, "error": "cleaning_run not found"}
    return {"ok": True, "record": _jsonable(record)}


@tool("recent_cleaning_runs")
async def recent_cleaning_runs(limit: int = 20) -> dict[str, Any]:
    store = CleaningRunsStore(await _pool())
    items = await store.recent(limit=limit)
    return {"ok": True, "items": _jsonable(items), "count": len(items)}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
