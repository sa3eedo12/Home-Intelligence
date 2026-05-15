"""GET /admin/intelligence/summary — what the system has actually learned.

Built to answer the user's complaint: "the system doesn't feel intelligent or
learning". The dashboard now has a single page (/dashboard/what-i-know) that
surfaces, in one glance, everything the system actually knows about the user
and their home — the learned habits, devices, members, recent inferences,
mistakes corrected, and open questions.

This module is separate from admin.py to keep the heavy SQL out of the main
admin surface and to make it easy to tune the per-section queries.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from fastapi import Request
from home_agents_sdk.auto_inferences_store import AutoInferencesStore
from home_agents_sdk.cleaning_runs_store import CleaningRunsStore
from home_agents_sdk.cycle_loads_store import CycleLoadsStore
from home_agents_sdk.presence_returns_store import PresenceReturnsStore
from home_agents_sdk.sleep_summaries_store import SleepSummariesStore
from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tv_left_on_store import TvLeftOnStore

logger = get_logger("orchestrator.intelligence")


async def _safe(coro, fallback):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - section failures shouldn't break the page
        logger.warning("intelligence_section_failed", error=str(exc))
        return fallback


def _confirmed(rows: list[dict[str, Any]], field: str) -> tuple[int, int]:
    """Return (confirmed_count, total) given a list of rows where ``field`` is
    populated only when the user has confirmed the inference."""
    total = len(rows)
    confirmed = sum(1 for r in rows if r.get(field) not in (None, ""))
    return confirmed, total


async def gather_intelligence_summary(request: Request) -> dict[str, Any]:
    """Return a single big dict with every "what I know" section."""
    pool = getattr(request.app.state, "pool", None)
    knowledge_graph = getattr(request.app.state, "knowledge_graph", None)
    health_store = getattr(request.app.state, "health_store", None)

    cycle_store = CycleLoadsStore(pool)
    cleaning_store = CleaningRunsStore(pool)
    sleep_store = SleepSummariesStore(pool)
    presence_store = PresenceReturnsStore(pool)
    tv_store = TvLeftOnStore(pool)
    auto_store = AutoInferencesStore(pool)

    members_coro = (
        _safe(knowledge_graph.list_members(include_pets=True), [])
        if knowledge_graph
        else _async_value([])
    )
    things_coro = (
        _safe(knowledge_graph.list_things(), []) if knowledge_graph else _async_value([])
    )
    habits_coro = (
        _safe(knowledge_graph.list_habits(), []) if knowledge_graph else _async_value([])
    )

    # Run all the cheap, parallelizable lookups concurrently.
    (
        members,
        things,
        habits,
        cycle_recent,
        cleaning_recent,
        sleep_recent,
        presence_recent,
        tv_recent,
        auto_recent,
        observation_counts_by_kind,
        recent_observations,
    ) = await asyncio.gather(
        members_coro,
        things_coro,
        habits_coro,
        _safe(cycle_store.recent(limit=30), []),
        _safe(cleaning_store.recent(limit=20), []),
        _safe(sleep_store.recent(limit=14), []),
        _safe(presence_store.recent(limit=30), []),
        _safe(tv_store.recent(limit=20), []),
        _safe(auto_store.recent(limit=30), []),
        _observation_counts(pool),
        _recent_observations(pool, limit=10),
    )

    # Things grouped by type for the "devices we know about" tile.
    things_by_type: dict[str, list[dict[str, Any]]] = {}
    for thing in things or []:
        things_by_type.setdefault(str(thing.get("type") or "other"), []).append(thing)

    # Habits split into confirmed vs unconfirmed.
    confirmed_habits = [h for h in habits or [] if _habit_confirmed(h)]
    unconfirmed_habits = [h for h in habits or [] if not _habit_confirmed(h)]

    # Per-inference confirmation stats — gives the user a sense of how much
    # they've actually trained the system.
    cycle_conf, cycle_total = _confirmed(cycle_recent, "confirmed_label")
    cleaning_conf, cleaning_total = _confirmed(cleaning_recent, "confirmed_status")
    sleep_conf, sleep_total = _confirmed(sleep_recent, "confirmed_quality")
    presence_conf, presence_total = _confirmed(presence_recent, "confirmed_context")
    tv_conf, tv_total = _confirmed(tv_recent, "confirmed_action")

    auto_status_counts = Counter((r.get("status") or "proposed") for r in auto_recent)

    # HealthKit signal — shows whether sleep/steps/weight are flowing.
    health_signal: dict[str, Any] = {"available": False}
    if health_store is not None:
        try:
            for metric in ("sleep_minutes", "steps", "weight_kg", "heart_rate"):
                rows = await health_store.list_recent(metric=metric, hours=72)
                if rows:
                    health_signal = {
                        "available": True,
                        "last_metric": metric,
                        "last_value": rows[-1].get("value"),
                        "last_ts": str(rows[-1].get("ts")),
                    }
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("intelligence_health_probe_failed", error=str(exc))

    return {
        "ok": True,
        "household": {
            "members": _serialize_members(members),
        },
        "devices": {
            "by_type": {
                t: [_serialize_thing(x) for x in xs]
                for t, xs in sorted(things_by_type.items())
            },
            "total": len(things or []),
        },
        "habits": {
            "confirmed": [_serialize_habit(h) for h in confirmed_habits],
            "unconfirmed": [_serialize_habit(h) for h in unconfirmed_habits],
            "total": len(habits or []),
        },
        "inferences": {
            "appliance_cycles": {
                "recent": _serialize_rows(cycle_recent[:10]),
                "confirmed": cycle_conf,
                "total": cycle_total,
            },
            "vacuum_cleanings": {
                "recent": _serialize_rows(cleaning_recent[:10]),
                "confirmed": cleaning_conf,
                "total": cleaning_total,
            },
            "sleep_summaries": {
                "recent": _serialize_rows(sleep_recent[:7]),
                "confirmed": sleep_conf,
                "total": sleep_total,
            },
            "presence_returns": {
                "recent": _serialize_rows(presence_recent[:10]),
                "confirmed": presence_conf,
                "total": presence_total,
            },
            "tv_left_on": {
                "recent": _serialize_rows(tv_recent[:10]),
                "confirmed": tv_conf,
                "total": tv_total,
            },
            "auto_inferences": {
                "recent": _serialize_rows(auto_recent[:10]),
                "by_status": dict(auto_status_counts),
            },
        },
        "observations": {
            "counts_24h_by_kind": observation_counts_by_kind,
            "recent": recent_observations,
        },
        "health": health_signal,
        "open_questions": _open_questions(
            unconfirmed_habits=unconfirmed_habits,
            cycle_unconfirmed=cycle_total - cycle_conf,
            cleaning_unconfirmed=cleaning_total - cleaning_conf,
            sleep_unconfirmed=sleep_total - sleep_conf,
            presence_unconfirmed=presence_total - presence_conf,
            tv_unconfirmed=tv_total - tv_conf,
            auto_status_counts=auto_status_counts,
        ),
    }


async def _async_value(v):
    return v


async def _observation_counts(pool) -> dict[str, int]:
    if pool is None:
        return {}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT capability, count(*) AS n
                FROM event_log
                WHERE agent LIKE 'observer.%'
                  AND ts > now() - interval '24 hours'
                GROUP BY capability
                ORDER BY n DESC
                """
            )
        return {r["capability"]: int(r["n"]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("observation_counts_failed", error=str(exc))
        return {}


async def _recent_observations(pool, *, limit: int = 10) -> list[dict[str, Any]]:
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, agent, capability, summary
                FROM event_log
                WHERE agent LIKE 'observer.%'
                ORDER BY ts DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "id": int(r["id"]),
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "agent": r["agent"],
                "capability": r["capability"],
                "summary": r["summary"],
            }
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("recent_observations_failed", error=str(exc))
        return []


def _habit_confirmed(habit: dict[str, Any]) -> bool:
    attrs = habit.get("attributes") or {}
    if isinstance(attrs, str):
        # registry returns jsonb as string sometimes; tolerate it
        return False
    return bool(attrs.get("confirmed_at") or habit.get("confirmed_at"))


def _serialize_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in members or []:
        out.append({
            "id": m.get("id"),
            "name": m.get("name"),
            "role": m.get("role"),
            "sleep_time": str(m.get("sleep_time") or ""),
            "wake_time": str(m.get("wake_time") or ""),
            "allergies": list(m.get("allergies") or []),
            "dietary_restrictions": list(m.get("dietary_restrictions") or []),
            "telegram_chat_id": m.get("telegram_chat_id"),
        })
    return out


def _serialize_thing(thing: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": thing.get("id"),
        "friendly_name": thing.get("friendly_name") or thing.get("name"),
        "type": thing.get("type"),
        "entity_id": (thing.get("attributes") or {}).get("entity_id"),
        "confidence": thing.get("confidence"),
    }


def _serialize_habit(habit: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": habit.get("id"),
        "subject": habit.get("subject"),
        "pattern": habit.get("pattern"),
        "frequency": habit.get("frequency"),
        "confidence": habit.get("confidence"),
        "last_observed_at": str(habit.get("last_observed_at") or ""),
        "source": habit.get("source"),
    }


def _format_habit_example(habit: dict[str, Any]) -> str:
    """Render a habit row as a one-line, human-readable example.

    The ``pattern`` column is jsonb (e.g. ``{"value": {"start": "22:00", "end":
    "07:00"}, "rationale": "..."}``) — earlier code concatenated the dict
    directly into a string with ``+ " — " +``, which raises TypeError and
    500s the entire /dashboard/what-i-know page. Now we extract the most
    descriptive bits we can find.
    """
    subject = str(habit.get("subject") or "").strip()
    pattern = habit.get("pattern")
    parts: list[str] = []
    if subject:
        parts.append(subject)
    if isinstance(pattern, dict):
        # Prefer 'value' (the structured signal) over 'rationale' (LLM prose).
        value = pattern.get("value")
        if isinstance(value, dict):
            # Time-window patterns are by far the most common shape.
            start = value.get("start")
            end = value.get("end")
            if start and end:
                parts.append(f"{start} → {end}")
            else:
                # Fall back to a compact key=value rendering of value
                rendered = ", ".join(f"{k}={v}" for k, v in value.items() if k != "confidence")
                if rendered:
                    parts.append(rendered)
        elif value not in (None, ""):
            parts.append(str(value))
        else:
            rationale = pattern.get("rationale") or pattern.get("summary")
            if rationale:
                parts.append(str(rationale)[:120])
    elif pattern not in (None, ""):
        parts.append(str(pattern)[:120])
    return " — ".join(parts) if parts else "(habit details unavailable)"


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows or []:
        clean = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                clean[k] = v.isoformat()
            elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                clean[k] = v
            else:
                clean[k] = str(v)
        out.append(clean)
    return out


def _open_questions(
    *,
    unconfirmed_habits: list[dict[str, Any]],
    cycle_unconfirmed: int,
    cleaning_unconfirmed: int,
    sleep_unconfirmed: int,
    presence_unconfirmed: int,
    tv_unconfirmed: int,
    auto_status_counts: Counter,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    if unconfirmed_habits:
        sample = unconfirmed_habits[0]
        questions.append({
            "topic": "habit",
            "summary": f"{len(unconfirmed_habits)} inferred habits awaiting your confirmation",
            "example": _format_habit_example(sample),
            "action": "Open About You → Habits to confirm or correct.",
        })
    if cycle_unconfirmed:
        questions.append({
            "topic": "appliance",
            "summary": (
                f"{cycle_unconfirmed} washer/dryer cycle(s) waiting on "
                f"a load-type confirmation"
            ),
            "action": "Reply to the most recent 🧺 Telegram prompt.",
        })
    if cleaning_unconfirmed:
        questions.append({
            "topic": "cleaning",
            "summary": f"{cleaning_unconfirmed} vacuum run(s) pending coverage confirmation",
            "action": "Tap the 🧹 Telegram message buttons.",
        })
    if sleep_unconfirmed:
        questions.append({
            "topic": "sleep",
            "summary": f"{sleep_unconfirmed} morning sleep summary/-ies awaiting quality rating",
            "action": "Tap a 🌙 Telegram quick reply.",
        })
    if presence_unconfirmed:
        questions.append({
            "topic": "presence",
            "summary": f"{presence_unconfirmed} arrival(s) waiting on a context tag",
            "action": "Tap a 👋 Telegram quick reply.",
        })
    if tv_unconfirmed:
        questions.append({
            "topic": "tv",
            "summary": f"{tv_unconfirmed} TV-left-on prompt(s) waiting for a decision",
            "action": "Tap a 📺 Telegram quick reply.",
        })
    proposed_auto = auto_status_counts.get("proposed", 0)
    if proposed_auto:
        questions.append({
            "topic": "auto_infer",
            "summary": f"{proposed_auto} auto-inference(s) awaiting your yes/no",
            "action": "Tap a 🤔 Telegram quick reply.",
        })
    return questions
