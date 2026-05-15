"""Pre-bedtime wind-down nudge.

Where ``personal_assistant.late_bedtime_check`` is a POST-bedtime check
("you're past your bedtime — want help winding down?"), this is a
PRE-bedtime nudge: 90 and 30 minutes BEFORE the user's sleep_time, look
at house state (TVs on, lights on, devices active) and gently surface a
suggestion if there's anything worth winding down.

Why both? The post-bedtime check fires when the user is already running
late and risks losing sleep. The pre-bedtime nudge fires while there's
still time to act gracefully — "5 lights still on, 2 TVs playing, want
me to start dimming things in 30 minutes?"

Designed to be called from the scheduler every 15 minutes. Internally:

  1. Loads member sleep_time/wake_time windows.
  2. For each member, checks if NOW is within ``minutes_before`` of
     their sleep_time.
  3. Once-per-day-per-tier dedup so the user doesn't get the same
     nudge every 15 min during the window.
  4. Gathers current state from the lights observer + recent
     entertainment.left_on events.
  5. Emits ONE proactive_suggestion proposal and one Telegram nudge
     iff there's something to suggest (otherwise stays silent).

Tunables (env-overridable):
  PRE_BEDTIME_TIERS=90,30        # min-before-sleep tiers to scan at
  PRE_BEDTIME_MIN_LIGHTS=3       # threshold for "lots of lights on"
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger

from .member_windows import (
    MemberWindow,
    _local_now,
    _on_today,
    load_member_windows,
    mark_fired_today,
    today_local_key,
)

logger = get_logger("orchestrator.pre_bedtime")

DEFAULT_TIERS = (90, 30)
DEFAULT_MIN_LIGHTS_THRESHOLD = 3


def _tiers() -> tuple[int, ...]:
    raw = os.getenv("PRE_BEDTIME_TIERS")
    if not raw:
        return DEFAULT_TIERS
    try:
        return tuple(sorted({int(x.strip()) for x in raw.split(",") if x.strip()}, reverse=True))
    except ValueError:
        return DEFAULT_TIERS


def _min_lights_threshold() -> int:
    raw = os.getenv("PRE_BEDTIME_MIN_LIGHTS", str(DEFAULT_MIN_LIGHTS_THRESHOLD))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MIN_LIGHTS_THRESHOLD


def _minutes_until(target: Any, *, now: datetime | None = None) -> int:
    """How many local-time minutes until the next occurrence of ``target``.

    target is a ``time`` object; the next occurrence is today if it
    hasn't passed yet, else tomorrow.
    """
    local_now = _local_now(now)
    target_today = _on_today(target, local_now)
    if target_today < local_now:
        target_today = target_today + timedelta(days=1)
    delta = target_today - local_now
    return int(delta.total_seconds() // 60)


def _matched_tier(window: MemberWindow, *, now: datetime | None = None) -> int | None:
    """Return the largest tier (in minutes) that ``now`` falls into.

    A 15-min scheduler granularity means we want to fire ONCE within each
    tier window — so the tier "starts" when ``minutes_until_sleep <= tier``
    and "ends" when ``minutes_until_sleep`` drops below the next-smaller
    tier (or 0). Returns the tier we're currently 'in', or None if none.
    """
    minutes = _minutes_until(window.sleep_time, now=now)
    if minutes < 0:
        return None
    tiers = _tiers()
    for i, tier in enumerate(tiers):
        # We're INSIDE this tier if minutes_until_sleep is between
        # the next-smaller tier (or 0) and this tier.
        next_smaller = tiers[i + 1] if i + 1 < len(tiers) else 0
        if next_smaller < minutes <= tier:
            return tier
    return None


def _compose_message(
    *,
    minutes_left: int,
    light_count: int,
    tv_count: int,
    light_examples: list[str],
) -> str | None:
    """Compose the gentle nudge text. Returns None if there's nothing
    worth surfacing."""
    if light_count == 0 and tv_count == 0:
        return None
    lights_phrase = ""
    if light_count >= _min_lights_threshold():
        sample = ", ".join(light_examples[:3])
        lights_phrase = (
            f"{light_count} lights are still on"
            + (f" ({sample})" if sample else "")
        )
    elif light_count > 0:
        lights_phrase = f"{light_count} light{'s' if light_count > 1 else ''} on"
    tvs_phrase = ""
    if tv_count == 1:
        tvs_phrase = "1 TV playing"
    elif tv_count > 1:
        tvs_phrase = f"{tv_count} TVs playing"
    bits = [b for b in (lights_phrase, tvs_phrase) if b]
    if not bits:
        return None
    state = " and ".join(bits)
    if minutes_left <= 30:
        return f"🌙 Bedtime in ~{minutes_left} min. {state}. Want me to wind things down?"
    return f"🌙 ~{minutes_left} min until bedtime. {state}. Want a heads-up at 30?"


def _keyboard(member_name: str | None) -> list[list[dict[str, str]]]:
    name = member_name or "you"
    return [
        [
            {"text": "Yes, wind down", "callback": f"prebed:{name}:wind_down"},
            {"text": "Not tonight", "callback": f"prebed:{name}:_skip"},
        ]
    ]


async def _recent_tv_count(pool: Any | None, *, hours: int = 1) -> int:
    """How many distinct media_player entities the TV observer flagged
    as 'left on' in the last ``hours`` hours. Cheap proxy for "TV is
    currently active" without polling Home Assistant."""
    if pool is None:
        return 0
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT (payload->>'entity_id'))::int
                  FROM event_log
                 WHERE capability = 'entertainment.left_on'
                   AND ts >= now() - ($1::int * interval '1 hour')
                """,
                max(1, hours),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pre_bedtime_tv_count_failed", error=str(exc))
        return 0
    return int(value or 0)


async def scan_pre_bedtime(
    *,
    reflection_store: ReflectionStore,
    redis: Any,
    pool: Any | None = None,
    lights_observer: Any | None = None,
    people_home_fetch: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one pre-bedtime scan. Returns a structured-log-friendly dict.

    ``redis`` is used for once-per-day-per-tier dedup so a 15-min scheduler
    cadence doesn't spam multiple nudges within the same tier window.

    ``people_home_fetch`` (optional async callable) returns the current
    list of people home. When supplied AND it returns an empty list, the
    scan no-ops with reason='nobody_home' — nudging someone to wind down
    the lights when they're not there is just noise. The dedup flag is
    NOT set in this case so the scanner re-evaluates as soon as anyone
    returns.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    windows = await load_member_windows(pool)
    if not windows:
        return {"emitted": 0, "skipped": "no_member_windows"}

    if people_home_fetch is not None:
        try:
            people_home = await people_home_fetch()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pre_bedtime_people_home_failed", error=str(exc))
            people_home = None
        if people_home is not None and not people_home:
            logger.info("pre_bedtime_skipped", reason="nobody_home")
            return {"emitted": 0, "skipped": "nobody_home"}

    snapshot: dict[str, Any] = {"count": 0, "lights": []}
    if lights_observer is not None and hasattr(lights_observer, "snapshot"):
        try:
            snapshot = lights_observer.snapshot() or snapshot
        except Exception as exc:  # noqa: BLE001
            logger.warning("pre_bedtime_lights_snapshot_failed", error=str(exc))
            snapshot = {"count": 0, "lights": []}

    tv_count = await _recent_tv_count(pool, hours=1)

    out: list[dict[str, Any]] = []
    for window in windows:
        tier = _matched_tier(window, now=now)
        if tier is None:
            continue
        prefix = f"prebed:{window.member_id or window.name or 'household'}:t{tier}"
        if await redis.exists(today_local_key(prefix, now=now)):
            logger.info(
                "pre_bedtime_skipped",
                reason="dedup",
                tier=tier,
                member=window.name,
            )
            continue

        light_count = int(snapshot.get("count", 0))
        light_examples = [
            str(item.get("friendly_name") or item.get("entity_id"))
            for item in (snapshot.get("lights") or [])
        ]
        minutes_left = _minutes_until(window.sleep_time, now=now)
        text = _compose_message(
            minutes_left=minutes_left,
            light_count=light_count,
            tv_count=tv_count,
            light_examples=light_examples,
        )
        if text is None:
            logger.info(
                "pre_bedtime_skipped",
                reason="nothing_to_suggest",
                tier=tier,
                member=window.name,
                light_count=light_count,
                tv_count=tv_count,
            )
            # Still mark today's tier as 'fired' so we don't re-check
            # this tier every 15 min — once we've decided "nothing to
            # surface" at the 90-min mark, we don't need to recheck
            # until the 30-min tier opens.
            await mark_fired_today(redis, prefix, now=now)
            continue

        try:
            proposal_id = await reflection_store.add_proposal(
                kind="proactive_suggestion",
                title=text,
                rationale=(
                    f"Pre-bedtime tier={tier}min before sleep_time={window.sleep_time}; "
                    f"lights_on={light_count}; tvs_active_last_hour={tv_count}; "
                    f"member={window.name}"
                ),
                confidence=0.7 if tier <= 30 else 0.55,
                impact_estimate="gentle nudge",
            )
        except Exception as exc:
            logger.warning("pre_bedtime_add_proposal_failed", error=str(exc))
            continue

        # Send to Telegram via the same notify.outbound stream the rest
        # of the system uses; severity=notice so quiet hours can still
        # gate it (which it shouldn't, since we're PRE-bedtime).
        try:
            import json as _json

            await redis.xadd(
                "notify.outbound",
                {
                    "payload": _json.dumps(
                        {
                            "text": text,
                            "topic": "sleep.pre_bedtime",
                            "severity": "notice",
                            "agent": "orchestrator",
                            "capability": "pre_bedtime_scan",
                            "keyboard": _keyboard(window.name),
                        }
                    )
                },
            )
        except Exception as exc:
            logger.warning("pre_bedtime_notify_failed", error=str(exc))

        await mark_fired_today(redis, prefix, now=now)
        logger.info(
            "pre_bedtime_emitted",
            tier=tier,
            member=window.name,
            light_count=light_count,
            tv_count=tv_count,
            proposal_id=proposal_id,
        )
        out.append(
            {
                "proposal_id": proposal_id,
                "tier": tier,
                "minutes_left": minutes_left,
                "member": window.name,
                "title": text,
            }
        )

    return {"emitted": len(out), "scanned_members": len(windows), "proposals": out}
