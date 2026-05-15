"""Promote an accepted/auto_confirmed proposal into the typed knowledge tables.

Single source of truth for the contract:
  proposals row (kind=habit_inference|preference_inference|routine_inference)
                          ↓
            user_profile row (free-form key/value)
                          ↓
        habits / preferences / routines table (typed,
        what the About You dashboard actually reads)

Previously this lived only inside NightlyReflector._write_auto_confirmed_profile,
so the manual /admin/proposals/{id}/accept endpoint silently bypassed it
and the typed tables stayed empty no matter how many proposals the user
accepted on the dashboard.
"""
from __future__ import annotations

import re
from typing import Any

from .telemetry import get_logger

logger = get_logger("home_agents_sdk.proposal_promotion")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:48] or "habit"


PROMOTION_KINDS = {"habit_inference", "preference_inference", "routine_inference"}


async def promote_proposal_to_knowledge(
    *,
    proposal: dict[str, Any],
    reflection_store: Any,
    knowledge_graph: Any | None,
) -> dict[str, Any]:
    """Write the proposal's contents into user_profile + the typed table.

    Safe to call on any kind: returns early with skipped=True for
    proposals whose kind doesn't promote (e.g. code_change, cleanup_action,
    suggested_action). Errors are logged at warning and swallowed; never
    raises so the caller's accept/auto-confirm flow stays robust.
    """
    kind = str(proposal.get("kind") or "")
    title = str(proposal.get("title") or "")
    if kind not in PROMOTION_KINDS:
        return {"ok": True, "skipped": True, "reason": "kind_not_promotable", "kind": kind}

    # 1. user_profile row (free-form key/value index that the reflector
    #    surfaces back to itself in nightly context).
    profile_key = proposal.get("profile_key") or f"habits.{_slug(title or 'habit')}"
    profile_value = proposal.get("profile_value")
    if profile_value is None:
        profile_value = {
            "title": title,
            "rationale": proposal.get("rationale"),
            "evidence_event_ids": proposal.get("evidence_event_ids") or [],
        }
    confidence = float(proposal.get("confidence") or 0.0)
    source = f"proposal:{proposal.get('id') or 'manual_accept'}"

    promoted: dict[str, Any] = {"profile_key": str(profile_key), "knowledge_table": None}

    try:
        await reflection_store.upsert_profile(
            key=str(profile_key),
            value=profile_value,
            confidence=confidence,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "promote_proposal_profile_write_failed",
            kind=kind,
            title=title,
            error=str(exc),
        )

    # 2. Typed knowledge table — what the dashboard actually reads.
    if knowledge_graph is None:
        return {"ok": True, "promoted": promoted, "skipped": "no_knowledge_graph"}

    try:
        if kind == "habit_inference":
            await knowledge_graph.put_habit(
                subject=str(profile_key) if not title else title,
                pattern={
                    "rationale": proposal.get("rationale"),
                    "value": proposal.get("profile_value"),
                    "evidence_event_ids": proposal.get("evidence_event_ids") or [],
                },
                frequency=str(proposal.get("frequency") or ""),
                confidence=confidence,
                source=source,
            )
            promoted["knowledge_table"] = "habits"
        elif kind == "preference_inference":
            pref_value = proposal.get("profile_value") or {
                "title": title,
                "rationale": proposal.get("rationale"),
            }
            await knowledge_graph.put_preference(
                key=str(profile_key),
                value=pref_value,
                confidence=confidence,
                source=source,
            )
            promoted["knowledge_table"] = "preferences"
        elif kind == "routine_inference":
            steps = proposal.get("profile_value") or {
                "rationale": proposal.get("rationale"),
            }
            steps_list = steps if isinstance(steps, list) else [steps]
            await knowledge_graph.put_routine(
                name=title or str(profile_key),
                steps=steps_list,
                schedule=proposal.get("schedule") or None,
                source=source,
            )
            promoted["knowledge_table"] = "routines"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "promote_proposal_kg_write_failed",
            kind=kind,
            title=title,
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "promoted": promoted, "error": str(exc)}

    logger.info(
        "promote_proposal_ok",
        kind=kind,
        title=title,
        knowledge_table=promoted["knowledge_table"],
        profile_key=promoted["profile_key"],
    )
    return {"ok": True, "promoted": promoted}
