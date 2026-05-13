"""Cycle-load inference for appliance cycles.

When the washer_observer detects ``appliance.cycle_completed``, it dispatches
to :func:`infer_cycle_load` here. We persist a guessed-label row and return a
user-facing summary + an inline keyboard the reactive trigger embeds in the
Telegram notification. The user replies via Telegram or the dashboard, which
routes back to :func:`confirm_cycle_load` to update the row.

Inference today is pure heuristics over program text, duration buckets, time
of day, and the user's recent confirmation history. It's intentionally
deterministic so the user can predict what we'll guess. A future revision can
swap in an LLM via Ollama for richer free-text guesses (e.g. "Looks like
gym clothes given the workout you logged 30 min ago"), but the storage shape
and confirmation flow stay identical.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import asyncpg
from home_agents_sdk import tool
from home_agents_sdk.cycle_loads_store import CycleLoadsStore

_POOL: asyncpg.Pool | None = None

# Ordered list of canonical labels we offer as quick-reply buttons.
CANDIDATE_LABELS: tuple[str, ...] = (
    "colors",
    "whites",
    "delicates",
    "towels",
    "bedding",
    "workout",
    "darks",
    "quick",
)

# Keywords that often appear in HA cycle-program names. The first match wins.
PROGRAM_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("delicate", "delicates"),
    ("hand wash", "delicates"),
    ("wool", "delicates"),
    ("silk", "delicates"),
    ("sport", "workout"),
    ("active", "workout"),
    ("towel", "towels"),
    ("bedding", "bedding"),
    ("sheet", "bedding"),
    ("duvet", "bedding"),
    ("dark", "darks"),
    ("black", "darks"),
    ("white", "whites"),
    ("bright", "whites"),
    ("color", "colors"),
    ("cotton", "colors"),
    ("mixed", "colors"),
    ("quick", "quick"),
    ("rapid", "quick"),
    ("speed", "quick"),
    ("daily", "colors"),
)


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


def _bucket_for_duration(seconds: int | None) -> tuple[str | None, str]:
    """Map a cycle duration to a likely label + a human-readable note."""
    if seconds is None or seconds <= 0:
        return None, "no duration"
    minutes = seconds // 60
    if minutes < 25:
        return "quick", f"short {minutes}-min cycle"
    if minutes < 45:
        return "delicates", f"~{minutes} min suggests a gentle cycle"
    if minutes < 75:
        return "colors", f"~{minutes} min is a standard load"
    if minutes < 120:
        return "towels", f"~{minutes} min is a heavy load"
    return "bedding", f"long {minutes}-min cycle suggests bedding/duvet"


def _guess_from_program(program: str | None) -> tuple[str | None, str]:
    if not program:
        return None, ""
    text = program.casefold()
    for keyword, label in PROGRAM_KEYWORDS:
        if keyword in text:
            return label, f"program '{program}' matched '{keyword}'"
    return None, ""


def _habitual_label(history: list[str]) -> tuple[str | None, str]:
    if not history:
        return None, ""
    counts = Counter(history)
    label, count = counts.most_common(1)[0]
    if count >= 2:
        return label, f"most common recent load was '{label}' ({count} of last {len(history)})"
    return None, ""


def _infer(
    *,
    duration_seconds: int | None,
    program: str | None,
    history: list[str],
) -> tuple[str, float, str]:
    """Combine signals into a single best guess + confidence + reasoning blob."""
    program_label, program_reason = _guess_from_program(program)
    duration_label, duration_reason = _bucket_for_duration(duration_seconds)
    habit_label, habit_reason = _habitual_label(history)

    reasons: list[str] = []
    label: str | None = None
    confidence = 0.0

    # Strongest signal: the user wrote the program name on the washer panel.
    if program_label:
        label = program_label
        confidence = 0.75
        reasons.append(program_reason)
        if duration_label == program_label:
            confidence = 0.9
            reasons.append(f"duration agrees ({duration_reason})")
    elif duration_label:
        label = duration_label
        confidence = 0.55
        reasons.append(duration_reason)

    # Habitual override: only bump confidence if it AGREES with the duration/program guess.
    if habit_label and label == habit_label:
        confidence = min(1.0, confidence + 0.1)
        reasons.append(habit_reason)
    elif label is None and habit_label:
        label = habit_label
        confidence = 0.4
        reasons.append(habit_reason)

    if label is None:
        label = "colors"  # safest default in most households
        confidence = 0.2
        reasons.append("no signals matched — defaulting to colors")

    return label, round(confidence, 2), "; ".join(reasons)


def _keyboard_for(cycle_load_id: int, guessed: str) -> list[list[dict[str, str]]]:
    """Build a 2-row inline keyboard with the guess first, then the candidates."""
    seen = {guessed}
    ordered: list[str] = [guessed]
    for candidate in CANDIDATE_LABELS:
        if candidate not in seen:
            ordered.append(candidate)
            seen.add(candidate)
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for label in ordered[:6]:  # cap to 6 buttons total (Telegram is fine with up to 100 but UX)
        button_text = f"✅ {label}" if label == guessed else label
        row.append({"text": button_text, "callback": f"cycle:{cycle_load_id}:{label}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Skip", "callback": f"cycle:{cycle_load_id}:_skip"}])
    return rows


@tool("infer_cycle_load", side_effects=True)
async def infer_cycle_load(payload: dict[str, Any]) -> dict[str, Any]:
    """Guess what kind of laundry just finished + persist for confirmation.

    Inputs (observer event payload, all optional):
      appliance, entity_id, started_at, ended_at, duration_seconds, program,
      brand, attributes_at_finish, event_log_id

    Output: ``{ok, summary, label, confidence, reasoning, cycle_load_id, keyboard}``
    """
    appliance = str(payload.get("appliance") or "washer")
    entity_id = payload.get("entity_id")
    program_raw = payload.get("program")
    program = str(program_raw) if program_raw not in (None, "") else None
    brand_raw = payload.get("brand")
    brand = str(brand_raw) if brand_raw not in (None, "") else None
    duration_seconds = _coerce_int(payload.get("duration_seconds"))
    started_at = _parse_iso(payload.get("started_at"))
    ended_at = _parse_iso(payload.get("ended_at")) or datetime.now(UTC)
    attributes_at_finish = payload.get("attributes_at_finish")
    if not isinstance(attributes_at_finish, dict):
        attributes_at_finish = {}
    event_log_id = _coerce_int(payload.get("event_log_id"))

    store = CycleLoadsStore(await _pool())
    history = await store.confirmed_label_history(appliance=appliance, limit=15)
    label, confidence, reasoning = _infer(
        duration_seconds=duration_seconds,
        program=program,
        history=history,
    )
    cycle_load_id = await store.insert_guess(
        appliance=appliance,
        entity_id=entity_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        program=program,
        brand=brand,
        attributes_at_finish=attributes_at_finish,
        guessed_label=label,
        guessed_confidence=confidence,
        guessed_reasoning=reasoning,
        event_log_id=event_log_id,
    )
    summary = _summary_for(appliance, label, confidence, reasoning, program)
    keyboard = _keyboard_for(cycle_load_id, label) if cycle_load_id is not None else []
    return {
        "ok": True,
        "summary": summary,
        "label": label,
        "confidence": confidence,
        "reasoning": reasoning,
        "cycle_load_id": cycle_load_id,
        "keyboard": keyboard,
    }


def _summary_for(
    appliance: str, label: str, confidence: float, reasoning: str, program: str | None
) -> str:
    appliance_word = "🧺 Washer" if appliance == "washer" else f"🧺 {appliance.title()}"
    confidence_word = "looks like" if confidence < 0.5 else "best guess:"
    bits = [f"{appliance_word} cycle done."]
    if program:
        bits.append(f"Program: {program}.")
    bits.append(f"{confidence_word} **{label}** ({int(round(confidence * 100))}%).")
    bits.append("Confirm or correct?")
    return " ".join(bits)


@tool("confirm_cycle_load", side_effects=True)
async def confirm_cycle_load(
    cycle_load_id: int, label: str, chat_id: int | None = None
) -> dict[str, Any]:
    """Record the user's correction. Called by Telegram callback handler.

    Returns a ``learning`` field with a short conversational note about
    the resulting pattern (e.g. "Got it — of your last 5 cycles, 4 were
    colors. I'll lean toward colors next time."). The Telegram callback
    handler can surface this back to the user instead of a bland "Saved".
    """
    if not isinstance(cycle_load_id, int) or cycle_load_id <= 0:
        return {"ok": False, "error": "cycle_load_id must be a positive integer"}
    if not isinstance(label, str) or not label.strip():
        return {"ok": False, "error": "label must be a non-empty string"}
    label_clean = label.strip().casefold()
    store = CycleLoadsStore(await _pool())
    record = await store.confirm(cycle_load_id, confirmed_label=label_clean, chat_id=chat_id)
    if record is None:
        return {"ok": False, "error": "cycle_load not found"}

    appliance = record.get("appliance") or "washer"
    history = await store.confirmed_label_history(appliance=appliance, limit=10)
    learning = _learning_message(label_clean, history, was_correction=label_clean != (
        record.get("guessed_label") or ""
    ).casefold())
    return {"ok": True, "record": _jsonable(record), "learning": learning}


def _learning_message(label: str, history: list[str], *, was_correction: bool) -> str:
    """Return a short note about what we learned from this confirmation."""
    same_label_count = sum(1 for h in history if h == label)
    if was_correction:
        if same_label_count >= 3:
            return (
                f"Noted — of your last {len(history)} cycles, {same_label_count} were {label}. "
                f"I'll lean {label} next time."
            )
        return f"Got it. Updated to {label}; I'll factor that in next time."
    if same_label_count >= 3:
        return (
            f"Saved as {label}. {same_label_count} of your last {len(history)} cycles "
            f"were {label} — that pattern is getting strong."
        )
    if same_label_count >= 1:
        return f"Saved as {label}. I've seen {same_label_count} other {label} load(s) recently."
    return f"Saved as {label} — first time you've confirmed this load type."


@tool("recent_cycle_loads")
async def recent_cycle_loads(
    appliance: str | None = None, limit: int = 20, only_confirmed: bool = False
) -> dict[str, Any]:
    store = CycleLoadsStore(await _pool())
    items = await store.recent(appliance=appliance, limit=limit, only_confirmed=only_confirmed)
    return {"ok": True, "items": _jsonable(items), "count": len(items)}


def _jsonable(value: Any) -> Any:
    """Roundtrip through json so datetimes/UUIDs serialize cleanly."""
    return json.loads(json.dumps(value, default=str))
