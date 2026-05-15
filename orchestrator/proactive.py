"""Proactive scanner — emits gentle "you usually do X around now" suggestions.

Where the morning brief is a once-a-day after-the-fact summary and the
reactive observers are after-the-fact alerts ("TV's been on 6h"), this
scanner runs every 15 minutes and asks:

    Given the current time-of-day + day-of-week and what the user has
    historically confirmed doing around this slot, is there a gentle
    suggestion worth surfacing right now?

V1 deliberately uses only the auto_inferences table (rows the user has
already confirmed). Each scan:

  1. Buckets all confirmed inferences from the last 30 days by
     (source_kind, hour-of-day rounded to a slot).
  2. Filters to buckets where the slot lines up with "now ± slot/2".
  3. Ranks by confirmation count → recency.
  4. Emits at most ONE proposal per scan to avoid Telegram spam.
  5. Suppresses if we're inside the user's sleep window.
  6. Suppresses if we already proposed an identical suggestion in the
     last 4 hours (dedup window > scan interval × 16).

Future versions will widen to cycle_loads, sleep_summaries, and
observer-derived patterns. The scaffolding (15-min cadence + gentle
proposal kind + dedup) doesn't need to change for any of that.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from home_agents_sdk.auto_inferences_store import AutoInferencesStore
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger

logger = get_logger("orchestrator.proactive")

DEFAULT_SLOT_MINUTES = 60
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_MIN_CONFIRMATIONS = 3
DEFAULT_DEDUP_HOURS = 4
DEFAULT_MAX_PROPOSALS_PER_SCAN = 1


def _slot_minutes() -> int:
    raw = os.getenv("PROACTIVE_SLOT_MINUTES", str(DEFAULT_SLOT_MINUTES))
    try:
        return max(15, min(int(raw), 240))
    except (TypeError, ValueError):
        return DEFAULT_SLOT_MINUTES


def _lookback_days() -> int:
    raw = os.getenv("PROACTIVE_LOOKBACK_DAYS", str(DEFAULT_LOOKBACK_DAYS))
    try:
        return max(1, min(int(raw), 90))
    except (TypeError, ValueError):
        return DEFAULT_LOOKBACK_DAYS


def _min_confirmations() -> int:
    raw = os.getenv("PROACTIVE_MIN_CONFIRMATIONS", str(DEFAULT_MIN_CONFIRMATIONS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIRMATIONS


def _dedup_hours() -> int:
    raw = os.getenv("PROACTIVE_DEDUP_HOURS", str(DEFAULT_DEDUP_HOURS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_DEDUP_HOURS


def _max_per_scan() -> int:
    raw = os.getenv("PROACTIVE_MAX_PER_SCAN", str(DEFAULT_MAX_PROPOSALS_PER_SCAN))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PROPOSALS_PER_SCAN


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("TZ", "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _slot_for(dt: datetime, slot_minutes: int) -> int:
    """Return the slot index (0..1440/slot - 1) for a local datetime."""
    minutes = dt.hour * 60 + dt.minute
    return (minutes // slot_minutes) % (24 * 60 // slot_minutes)


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_in_sleep_window(now_local: time, sleep_time: time, wake_time: time) -> bool:
    """Mirror of tv_observer._time_is_in_sleep_window — kept local to avoid
    a cross-module import for one helper. Handles midnight crossings."""
    if sleep_time == wake_time:
        return False
    if sleep_time < wake_time:
        return sleep_time <= now_local < wake_time
    return now_local >= sleep_time or now_local < wake_time


async def _quiet_hour_active(pool: Any, now: datetime) -> bool:
    """True if any household member is in their sleep window right now.

    Conservative: any one member being asleep mutes proactive suggestions
    for everyone. Better to under-notify at 02:00 than wake someone up.
    """
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT sleep_time, wake_time
                  FROM household_members
                 WHERE sleep_time IS NOT NULL
                   AND role <> 'pet'
                """
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("proactive_sleep_window_query_failed", error=str(exc))
        return False
    local_now = now.astimezone(_local_tz()).time().replace(tzinfo=None)
    for row in rows:
        sleep = row["sleep_time"]
        wake = row["wake_time"] or time(7, 0)
        if not isinstance(sleep, time) or not isinstance(wake, time):
            continue
        if _is_in_sleep_window(local_now, sleep, wake):
            return True
    return False


def _bucket_inferences(
    rows: list[dict[str, Any]], slot_minutes: int, tz: ZoneInfo
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Group confirmed inferences by (source_kind, slot)."""
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        kind = str(row.get("source_kind") or "").strip()
        if not kind:
            continue
        ts_raw = row.get("confirmed_at") or row.get("created_at")
        ts = _parse_dt(ts_raw)
        if ts is None:
            continue
        slot = _slot_for(ts.astimezone(tz), slot_minutes)
        buckets.setdefault((kind, slot), []).append(row)
    return buckets


def _last_local_time_for_slot(slot: int, slot_minutes: int) -> str:
    """Render a slot index as a friendly local-time range, e.g. "18:00–19:00"."""
    start_minutes = slot * slot_minutes
    end_minutes = start_minutes + slot_minutes
    start = f"{(start_minutes // 60) % 24:02d}:{start_minutes % 60:02d}"
    end = f"{(end_minutes // 60) % 24:02d}:{end_minutes % 60:02d}"
    return f"{start}–{end}"


def _render_suggestion(kind: str, slot_label: str, examples: list[str]) -> str:
    """Compose a one-line gentle suggestion. Falls back to a generic
    template when we don't have a verb for ``kind``."""
    template = _SUGGESTION_TEMPLATES.get(kind)
    if template is None:
        # Generic: use the first inference as the suggestion verb.
        sample = examples[0] if examples else "doing this"
        return f"You usually {sample} around {slot_label}."
    return template.format(slot=slot_label)


_SUGGESTION_TEMPLATES = {
    "entertainment.left_on": (
        "You often have the TV on around {slot} — want me to nudge if it stays on past bedtime?"
    ),
    "sleep.likely_asleep": "You usually head to bed around {slot}.",
    "sleep.likely_awake": "You usually wake up around {slot}.",
    "appliance.cycle_completed": "You usually run a wash around {slot}.",
    "coffee.brewed": "You usually brew coffee around {slot}.",
}


async def _recent_proactive_kinds(
    reflection_store: ReflectionStore,
    dedup_hours: int,
    *,
    now: datetime | None = None,
) -> set[str]:
    """Return the source_kinds we've already proposed about in the dedup
    window. Reuses list_proposals; cheap for the volumes we're at."""
    try:
        rows = await reflection_store.list_proposals(limit=200)
    except Exception as exc:
        logger.warning("proactive_list_proposals_failed", error=str(exc))
        return set()
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=dedup_hours)
    seen: set[str] = set()
    for row in rows:
        if row.get("kind") != "proactive_suggestion":
            continue
        created = _parse_dt(row.get("created_at"))
        if created is None or created < cutoff:
            continue
        # We tag the source_kind into the rationale; pull it back out.
        rationale = str(row.get("rationale") or "")
        marker = "source_kind="
        if marker in rationale:
            tag = rationale.split(marker, 1)[1].split(";", 1)[0].strip()
            if tag:
                seen.add(tag)
    return seen


async def scan_for_opportunities(
    *,
    reflection_store: ReflectionStore,
    auto_store: AutoInferencesStore,
    pool: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one proactive scan. Returns a summary dict suitable for
    structured logging."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    tz = _local_tz()
    slot_minutes = _slot_minutes()
    current_slot = _slot_for(now.astimezone(tz), slot_minutes)
    slot_label = _last_local_time_for_slot(current_slot, slot_minutes)

    if await _quiet_hour_active(pool, now):
        logger.info(
            "proactive_scan_skipped",
            reason="quiet_hours",
            slot=slot_label,
        )
        return {"emitted": 0, "skipped": "quiet_hours", "slot": slot_label}

    try:
        # Pull a bounded slice of confirmed inferences. Lookback × ~5/day
        # is a generous upper bound on what we need.
        confirmed = await auto_store.recent(status="confirmed", limit=200)
    except Exception as exc:
        logger.warning("proactive_recent_failed", error=str(exc))
        return {"emitted": 0, "skipped": "store_unavailable"}

    lookback = timedelta(days=_lookback_days())
    cutoff = now - lookback
    fresh = [
        row
        for row in confirmed
        if (_parse_dt(row.get("confirmed_at") or row.get("created_at")) or now) >= cutoff
    ]

    buckets = _bucket_inferences(fresh, slot_minutes, tz)
    min_confirms = _min_confirmations()
    candidates: list[tuple[str, int, list[dict[str, Any]]]] = [
        (kind, slot, rows)
        for (kind, slot), rows in buckets.items()
        if slot == current_slot and len(rows) >= min_confirms
    ]
    if not candidates:
        logger.info(
            "proactive_scan_no_candidates",
            slot=slot_label,
            buckets=len(buckets),
        )
        return {"emitted": 0, "skipped": "no_candidates", "slot": slot_label}

    # Highest-confidence buckets first (count desc, then most-recent-first).
    candidates.sort(
        key=lambda c: (
            -len(c[2]),
            -(
                _parse_dt(c[2][0].get("confirmed_at") or c[2][0].get("created_at"))
                or now
            ).timestamp(),
        )
    )

    seen = await _recent_proactive_kinds(reflection_store, _dedup_hours(), now=now)
    emitted = 0
    out: list[dict[str, Any]] = []
    for kind, _slot, rows in candidates:
        if emitted >= _max_per_scan():
            break
        if kind in seen:
            continue
        examples = [str(r.get("inference") or "").strip() for r in rows[:3]]
        examples = [e for e in examples if e]
        title = _render_suggestion(kind, slot_label, examples)
        rationale = (
            f"Confirmed {len(rows)} times in the last {_lookback_days()} days "
            f"around the {slot_label} slot. source_kind={kind}; "
            f"recent_examples={examples[:2]}"
        )
        try:
            proposal_id = await reflection_store.add_proposal(
                kind="proactive_suggestion",
                title=title,
                rationale=rationale,
                evidence_event_ids=[
                    int(r.get("source_event_log_id"))
                    for r in rows
                    if isinstance(r.get("source_event_log_id"), int)
                ][:5],
                confidence=min(0.5 + 0.1 * len(rows), 0.95),
                impact_estimate="gentle nudge",
            )
        except Exception as exc:
            logger.warning("proactive_add_proposal_failed", kind=kind, error=str(exc))
            continue
        logger.info(
            "proactive_emitted",
            kind=kind,
            slot=slot_label,
            confirmations=len(rows),
            proposal_id=proposal_id,
        )
        emitted += 1
        out.append(
            {
                "proposal_id": proposal_id,
                "source_kind": kind,
                "slot": slot_label,
                "title": title,
            }
        )

    return {"emitted": emitted, "slot": slot_label, "proposals": out}
